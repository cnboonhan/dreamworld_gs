"""Tag a generated world with the camera the viewer should spawn at, and the
path it should tour along.

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


def colmap_cameras(model: Path) -> tuple[np.ndarray, list[str]]:
    """(N,4,4) world->camera matrices from a COLMAP sparse model, with names."""
    import pycolmap

    rec = pycolmap.Reconstruction(model)
    mats, names = [], []
    for _, im in sorted(rec.images.items()):
        cfw = im.cam_from_world() if callable(im.cam_from_world) else im.cam_from_world
        m = np.eye(4)
        m[:3, :] = np.asarray(cfw.matrix(), dtype=np.float64)
        mats.append(m)
        names.append(im.name)
    if not mats:
        raise SystemExit(f"no registered images in {model}")
    return np.stack(mats), names


def write_path(w2c: np.ndarray, world: Path, names: list[str] | None = None) -> None:
    """Sample the straight line through the standpoints, for the viewer.

    The viewer's tour then walks the camera along the same line the rendered
    walkthrough uses, which keeps it inside the space the panoramas actually
    observed — outside it, nothing constrained the geometry.

    Views are named <panorama>_<k>, so grouping by prefix gives one centre per
    standpoint. That is what render_video.py fits its line through; fitting
    through the raw per-view poses instead would tilt the line slightly and
    the tour would no longer match the video.
    """
    centres = np.stack([-m[:3, :3].T @ m[:3, 3] for m in w2c])
    if names and len(names) == len(centres):
        groups: dict[str, list[np.ndarray]] = {}
        for name, c in zip(names, centres):
            groups.setdefault(name.rsplit("_", 1)[0], []).append(c)
        pts = np.stack([np.median(np.stack(v), 0) for _, v in sorted(groups.items())])
    else:
        # generative path: no view names, drop repeats instead
        keep = [centres[0]]
        for c in centres[1:]:
            if np.linalg.norm(c - keep[-1]) > 1e-6:
                keep.append(c)
        pts = np.stack(keep)
    if len(pts) < 2:
        return

    centred = pts - pts.mean(0)
    axis = np.linalg.svd(centred, full_matrices=False)[2][0]
    t = centred @ axis
    if t[0] > t[-1]:
        axis, t = -axis, -t
    mid = pts.mean(0)
    line = np.linspace(mid + axis * t.min(), mid + axis * t.max(), 240)

    ups = np.stack([-m[:3, :3].T @ np.array([0.0, 1.0, 0.0]) for m in w2c])
    up = ups.mean(0)
    up /= max(np.linalg.norm(up), 1e-9)

    out = world.with_suffix("").as_posix() + ".path.json"
    Path(out).write_text(json.dumps({
        "points": [[round(float(v), 5) for v in p] for p in line],
        "up": [round(float(v), 5) for v in up],
        "standpoints": len(pts),
    }))
    print(f"wrote {out} ({len(line)} points along {len(pts)} standpoints)")


def main() -> None:
    args = sys.argv[1:]
    # --colmap <sparse-model> <world.ply>: the reconstruction flow has no
    # gs_data/cameras.json, its poses live in the COLMAP model
    names = None
    if args and args[0] == "--colmap":
        model, world = Path(args[1]), Path(args[2])
        w2c, names = colmap_cameras(model)
    else:
        scene = Path(args[0])
        world = Path(args[1]) if len(args) > 1 else scene / "world.ply"
        w2c = training_cameras(scene)
    centers = np.stack([-m[:3, :3].T @ m[:3, 3] for m in w2c])
    # most central camera: representative of the interior, and away from the
    # trajectory endpoints that often sit against a wall
    pick = int(np.argmin(np.linalg.norm(centers - np.median(centers, 0), axis=1)))

    write_path(w2c, world, names)

    out = world.with_suffix("").as_posix() + ".cam.json"
    Path(out).write_text(json.dumps({
        "viewMatrix": [round(float(v), 6) for v in w2c[pick].T.flatten()],
        "source": f"training camera {pick} of {len(w2c)}",
    }, indent=2))
    print(f"wrote {out} (camera {pick}/{len(w2c)}, "
          f"center {centers[pick].round(2).tolist()})")


if __name__ == "__main__":
    main()
