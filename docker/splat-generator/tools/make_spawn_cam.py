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
import math
import sys
from pathlib import Path

import numpy as np
import torch

# a world generated from one panorama, rather than reconstructed from many
GENERATED = "@world"


def unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    return v / max(np.linalg.norm(v), 1e-9)


def hyworld_frame(scene: Path) -> tuple[np.ndarray, np.ndarray, float]:
    """(rows, centre, scale) of the frame HY-World normalised this world into.

    Rows map the normalised frame onto the exported ply: p_ply = centre +
    scale * rows.T @ p_normalised. The row order is measured, not assumed —
    scanning the yaw about up against the trained gaussians puts it at exactly
    90 degrees from the naive [right, facing, up] on every world, and the fit
    improves tenfold, from 0.43 ply units to 0.04.
    """
    meta = json.loads((scene / "gs_result" / "ply"
                       / "position_meta_info.json").read_text())
    up, fwd = unit(meta["up_direction"]), unit(meta["facing_direction"])
    rows = np.stack([-fwd, unit(np.cross(fwd, up)), up])
    return rows, np.asarray(meta["center_point"], dtype=np.float64), float(meta["scale"])


def to_building(scene: Path, panos: Path, pick: str, p0: np.ndarray) -> np.ndarray:
    """The 3x4 map carrying building metres into this world's coordinates.

    The world was generated from one panorama, and that panorama was
    photographed in Gazebo, so its depth camera recorded a metric range for
    every pixel. HY-World's own depth prediction carries a ray direction and a
    distance for the same pixels. That is a per-pixel correspondence between
    the two frames: orientation from the ray directions, scale from the ratio
    of the two ranges, origin from where the camera was standing. Nothing is
    fitted to appearance and nothing is guessed.

    Solving the orientation rather than asserting it also means this keeps
    working across the convention fix in capture.py: for panoramas captured
    before it the answer comes out with a determinant of -1, which is the
    mirror those worlds were built with.
    """
    import cv2

    pred = torch.load(scene / "render_results" / "full_depth_prediction.pt",
                      map_location="cpu", weights_only=False)
    rays = pred["rays"].numpy().astype(np.float64)
    dist = pred["distance"].numpy().astype(np.float64)
    mask = np.asarray(pred["mask"])
    H, W = dist.shape

    lon = math.pi - (np.arange(W) + 0.5) / W * 2 * math.pi
    lat = math.pi / 2 - (np.arange(H) + 0.5) / H * math.pi
    lo, la = np.meshgrid(lon, lat)
    gz = np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], -1)
    s = (slice(None, None, 8), slice(None, None, 8))
    A = np.linalg.lstsq(gz[s].reshape(-1, 3), rays[s].reshape(-1, 3), rcond=None)[0].T

    rng = cv2.resize(np.load(panos / f"{Path(pick).stem}.range.npy").astype(np.float64),
                     (W, H), interpolation=cv2.INTER_NEAREST)
    # Near surfaces only. Far ones are mostly generated rather than observed,
    # and their predicted distance carries no metric information at all: over
    # the whole frame the ratio is bimodal, over the near floor and walls its
    # quartiles sit within a few per cent of each other.
    near = mask & (rng > 0.4) & (rng < 8) & (dist > 1e-3) & (np.sin(lat)[:, None] * rng < 0.8)
    ratio = float(np.median(dist[near] / rng[near]))

    rows, centre, scale = hyworld_frame(scene)
    M = (scale * ratio) * (rows.T @ A)
    print(f"  {ratio:.4f} hyworld-units/m, {scale * ratio:.3f} ply-units/m, "
          f"handedness {np.sign(np.linalg.det(M)):+.0f}")
    return np.concatenate([M, (centre - M @ p0)[:, None]], 1)


def building_walk(scene: Path) -> tuple[np.ndarray, np.ndarray, float] | None:
    """(points, up, length_m) tracing this world's lane, in world coordinates.

    A generated world is one corridor, so the walk through it is that
    corridor's lane out of the building — the same straight line a camera
    driven through Gazebo would take, at the height the panoramas were shot
    from. Fitting a line through HY-World's planned cameras instead walked
    metres of invented space either side of a corridor only a metre or two
    long, which is what made the tours wander.
    """
    if not scene.name.endswith(GENERATED):
        return None
    edge = scene.name[:-len(GENERATED)]
    root = scene.parent.parent
    panos = root / "panos" / edge
    plans = sorted(root.glob("worlds/*/capture_plan.json"))
    if not (panos / "poses.json").is_file() or not plans:
        return None
    lane = next((e for data in json.loads(plans[0].read_text())["levels"].values()
                 for e in data["edges"] if e["id"] == edge), None)
    if lane is None:
        return None
    verts = {v["id"]: v for data in json.loads(plans[0].read_text())["levels"].values()
             for v in data["vertices"]}

    stand = json.loads((panos / "poses.json").read_text())["standpoints"]
    # the same panorama world-edge handed to HY-World: the middle of the walk
    pick = sorted(s["image"] for s in stand)[len(stand) // 2]
    xyz = {s["image"]: np.asarray(s["xyz"], dtype=np.float64) for s in stand}
    height = float(np.median([p[2] for p in xyz.values()]))
    a = np.array([verts[lane["a"]]["x"], verts[lane["a"]]["y"], height])
    b = np.array([verts[lane["b"]]["x"], verts[lane["b"]]["y"], height])
    # walked a to b, which is the order the id names and the order capture.py
    # sampled the lane in
    if np.linalg.norm(xyz[sorted(xyz)[0]] - b) < np.linalg.norm(xyz[sorted(xyz)[0]] - a):
        a, b = b, a

    T = to_building(scene, panos, pick, xyz[pick])
    line = np.linspace(a, b, 240)
    length = float(np.linalg.norm(b - a))
    print(f"  {edge}: {length:.2f} m lane at {height:.2f} m")
    return (line @ T[:, :3].T + T[:, 3], unit(T[:, :3] @ [0.0, 0.0, 1.0]), length)


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


def write_path(world: Path, line: np.ndarray, up: np.ndarray, source: str,
               length_m: float | None = None) -> np.ndarray:
    out = world.with_suffix("").as_posix() + ".path.json"
    doc = {
        "points": [[round(float(v), 5) for v in p] for p in line],
        "up": [round(float(v), 5) for v in up],
        "source": source,
    }
    # how long the walk is in the building, when that is known: it is what
    # lets the walkthrough be paced at walking speed rather than at whatever
    # speed fills a fixed number of seconds
    if length_m is not None:
        doc["length_m"] = round(float(length_m), 3)
    Path(out).write_text(json.dumps(doc))
    print(f"wrote {out} ({len(line)} points, {source})")
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
    length = None
    if args and args[0] == "--colmap":
        world = Path(args[2])
        pts, up = colmap_walk(Path(args[1]))
        line, source = fit_walk(pts, up), f"fitted through {len(pts)} standpoints"
    else:
        scene = Path(args[0])
        world = Path(args[1]) if len(args) > 1 else scene / "world.ply"
        walk = building_walk(scene)
        if walk is not None:
            line, up, length = walk
            source = "the lane, out of the building"
        else:
            # no capture to align to: a world generated from a loose panorama
            pts, up = hyworld_walk(scene)
            facing = unit(json.loads((scene / "gs_result" / "ply"
                                      / "position_meta_info.json").read_text())
                          ["facing_direction"])
            line = fit_walk(pts, up, facing)
            source = f"fitted through {len(pts)} planned cameras"
    write_cam(world, write_path(world, line, up, source, length), up)


if __name__ == "__main__":
    main()
