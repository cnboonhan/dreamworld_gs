"""Tag a generated world with the camera the viewer should spawn at.

Writes <world>.cam.json next to <world>.ply holding a 16-float column-major
world->camera matrix. The viewer picks it up automatically, so a scene opens
at eye level with the correct up-axis instead of the viewer's arbitrary
default (which makes non-+Y-up scenes look upside down until you drag).

The pose comes from the scene's own training cameras: WorldNav planned them
along the walkable floor, so they are upright by construction.

Usage:
    python make_spawn_cam.py <scene-dir> [world.ply]
"""

import json
import sys
from pathlib import Path

import numpy as np


def training_cameras(scene: Path) -> np.ndarray:
    """(N,4,4) world->camera matrices from the GS training data."""
    path = scene / "gs_data" / "cameras.json"
    if not path.exists():
        raise SystemExit(f"no {path}; run the pipeline first")
    data = json.loads(path.read_text())
    entries = data.values() if isinstance(data, dict) else data
    # the file mixes per-camera dicts with scalar metadata entries
    mats = [np.asarray(e["extrinsic"], dtype=np.float64).reshape(4, 4)
            for e in entries if isinstance(e, dict) and "extrinsic" in e]
    if not mats:
        raise SystemExit(f"no extrinsics in {path}")
    return np.stack(mats)


def colmap_cameras(model: Path) -> np.ndarray:
    """(N,4,4) world->camera matrices from a COLMAP sparse model."""
    import pycolmap

    rec = pycolmap.Reconstruction(model)
    mats = []
    for _, im in sorted(rec.images.items()):
        cfw = im.cam_from_world() if callable(im.cam_from_world) else im.cam_from_world
        m = np.eye(4)
        m[:3, :] = np.asarray(cfw.matrix(), dtype=np.float64)
        mats.append(m)
    if not mats:
        raise SystemExit(f"no registered images in {model}")
    return np.stack(mats)


def main() -> None:
    args = sys.argv[1:]
    # --colmap <sparse-model> <world.ply>: the reconstruction flow has no
    # gs_data/cameras.json, its poses live in the COLMAP model
    if args and args[0] == "--colmap":
        model, world = Path(args[1]), Path(args[2])
        w2c = colmap_cameras(model)
    else:
        scene = Path(args[0])
        world = Path(args[1]) if len(args) > 1 else scene / "world.ply"
        w2c = training_cameras(scene)
    centers = np.stack([-m[:3, :3].T @ m[:3, 3] for m in w2c])
    # most central camera: representative of the interior, and away from the
    # trajectory endpoints that often sit against a wall
    pick = int(np.argmin(np.linalg.norm(centers - np.median(centers, 0), axis=1)))

    out = world.with_suffix("").as_posix() + ".cam.json"
    Path(out).write_text(json.dumps({
        "viewMatrix": [round(float(v), 6) for v in w2c[pick].T.flatten()],
        "source": f"training camera {pick} of {len(w2c)}",
    }, indent=2))
    print(f"wrote {out} (camera {pick}/{len(w2c)}, "
          f"center {centers[pick].round(2).tolist()})")


if __name__ == "__main__":
    main()
