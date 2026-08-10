"""Tag a generated world with the camera the viewer should spawn at, and the
path it should tour along.

Writes <world>.path.json, the level line the standpoints lie on, and
<world>.cam.json, a 16-float column-major world->camera matrix midway along
it. The viewer picks both up automatically, so a scene opens at eye level the
right way up rather than in the viewer's arbitrary default orientation, and
its tour rides the same walk the rendered video does.

Both flows land here. A reconstructed world's poses come from its COLMAP
model; a generated one's come from HY-World's own record of the frame it
normalised the world into.

Usage:
    python make_spawn_cam.py <scene-dir> [world.ply]
    python make_spawn_cam.py --colmap <sparse-model> <world.ply>
"""

import json
import sys
from pathlib import Path

import numpy as np


def unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    return v / max(np.linalg.norm(v), 1e-9)


def hyworld_walk(scene: Path) -> tuple[np.ndarray, np.ndarray]:
    """(standpoints, up) for a generated world, in its exported ply's frame.

    The world is not axis-aligned — this building's corridors come out with up
    at [-0.07, -0.58, -0.82] and no two the same — so up cannot be guessed, and
    guessing it is what put the tour camera on its side walking into the
    ceiling: an up that is really a horizontal direction leaves cross(forward,
    up) nearly degenerate, which rolls the render and tilts the walk at once.

    It does not have to be guessed. HY-World records the frame it normalised
    the world into, beside the ply it exported: up, facing, centre and scale.
    The training extrinsics live in that normalised frame, so those four
    numbers map their centres back to ply coordinates exactly, and the walk
    comes out where the scene was actually observed.
    """
    meta = json.loads((scene / "gs_result" / "ply"
                       / "position_meta_info.json").read_text())
    up, fwd = unit(meta["up_direction"]), unit(meta["facing_direction"])
    centre = np.asarray(meta["center_point"], dtype=np.float64)
    scale = float(meta["scale"])

    path = scene / "gs_data" / "cameras.json"
    data = json.loads(path.read_text())
    entries = data.values() if isinstance(data, dict) else data
    # the file mixes per-camera dicts with scalar metadata entries
    mats = [np.asarray(e["extrinsic"], dtype=np.float64).reshape(4, 4)
            for e in entries if isinstance(e, dict) and "extrinsic" in e]
    if not mats:
        raise SystemExit(f"no extrinsics in {path}")
    normalised = np.stack([-m[:3, :3].T @ m[:3, 3] for m in mats])
    rows = np.stack([unit(np.cross(fwd, up)), fwd, up])
    return centre + scale * (normalised @ rows), up


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


def colmap_walk(model: Path) -> tuple[np.ndarray, np.ndarray]:
    """(standpoints, up) for a reconstructed world, from its COLMAP model.

    Views are named <panorama>_<k>, so grouping by prefix gives one centre per
    standpoint. That is what render_video.py fits its line through; fitting
    through the raw per-view poses instead would tilt the line slightly and the
    tour would no longer match the video.
    """
    w2c, names = colmap_cameras(model)
    centres = np.stack([-m[:3, :3].T @ m[:3, 3] for m in w2c])
    groups: dict[str, list[np.ndarray]] = {}
    for name, c in zip(names, centres):
        groups.setdefault(name.rsplit("_", 1)[0], []).append(c)
    pts = np.stack([np.median(np.stack(v), 0) for _, v in sorted(groups.items())])
    up = np.stack([-m[:3, :3].T @ np.array([0.0, 1.0, 0.0]) for m in w2c]).mean(0)
    return pts, unit(up)


def fit_walk(pts: np.ndarray, up: np.ndarray,
             facing: np.ndarray | None = None) -> np.ndarray:
    """Points along the level line the standpoints lie on.

    The viewer's tour and the rendered walkthrough both ride this, which keeps
    the camera inside the space the panoramas observed — outside it, nothing
    constrained the geometry. Levelling against up is what makes it a walk
    rather than a climb; the fit is otherwise least-squares.
    """
    height = float(np.median(pts @ up))
    flat = pts - np.outer(pts @ up, up)
    centred = flat - flat.mean(0)
    axis = unit(np.linalg.svd(centred, full_matrices=False)[2][0])
    t = centred @ axis
    if (axis @ facing if facing is not None else t[-1] - t[0]) < 0:
        axis, t = -axis, -t
    # the ends are excursions: HY-World sweeps its cameras out past the room to
    # see round corners, and a capture's first and last standpoints sit against
    # the vertices. Neither belongs in a walkthrough.
    lo, hi = np.percentile(t, [10, 90])
    mid = flat.mean(0) + up * height
    return np.linspace(mid + axis * lo, mid + axis * hi, 240)


def write_path(world: Path, pts: np.ndarray, up: np.ndarray,
               facing: np.ndarray | None = None) -> np.ndarray:
    line = fit_walk(pts, up, facing)
    out = world.with_suffix("").as_posix() + ".path.json"
    Path(out).write_text(json.dumps({
        "points": [[round(float(v), 5) for v in p] for p in line],
        "up": [round(float(v), 5) for v in up],
        "standpoints": len(pts),
    }))
    print(f"wrote {out} ({len(line)} points along {len(pts)} standpoints)")
    return line


def write_cam(world: Path, line: np.ndarray, up: np.ndarray) -> None:
    """The pose the viewer spawns at: midway along the walk, looking down it.

    Built here rather than copied from a training extrinsic — HY-World lays
    those out differently from COLMAP, so handing one straight to the viewer
    opened every generated world on its side.
    """
    i = len(line) // 2
    eye = line[i]
    fwd = unit(line[min(i + 10, len(line) - 1)] - eye)
    right = unit(np.cross(fwd, up))
    R = np.stack([right, np.cross(fwd, right), fwd])
    m = np.eye(4)
    m[:3, :3] = R
    m[:3, 3] = -R @ eye
    out = world.with_suffix("").as_posix() + ".cam.json"
    Path(out).write_text(json.dumps({
        "viewMatrix": [round(float(v), 6) for v in m.T.flatten()],
        "source": "midway along the walk",
    }, indent=2))
    print(f"wrote {out}")


def main() -> None:
    args = sys.argv[1:]
    # --colmap <sparse-model> <world.ply>: the reconstruction flow has no
    # gs_data/cameras.json, its poses live in the COLMAP model
    if args and args[0] == "--colmap":
        world = Path(args[2])
        pts, up = colmap_walk(Path(args[1]))
        facing = None
    else:
        scene = Path(args[0])
        world = Path(args[1]) if len(args) > 1 else scene / "world.ply"
        pts, up = hyworld_walk(scene)
        facing = unit(json.loads((scene / "gs_result" / "ply"
                                  / "position_meta_info.json").read_text())
                      ["facing_direction"])
    if len(pts) < 2:
        raise SystemExit(f"{world}: too few standpoints to fit a walk")
    write_cam(world, write_path(world, pts, up, facing), up)


if __name__ == "__main__":
    main()
