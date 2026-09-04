"""Tag a built world with the camera the viewer should open at.

Writes <world>.cam.json, a 16-float column-major world->camera matrix standing
in the middle of the world at eye level, looking the way it faces. The viewer
picks it up automatically, so a scene opens the right way up rather than in the
viewer's arbitrary default orientation.

Where the tour goes is a separate question, answered by edge_walks.py: a world
is generated at a waypoint, so it has one walk per corridor leaving it, not one
walk of its own.

Usage:
    python make_spawn_cam.py <scene-dir>
"""

import json
import sys
from pathlib import Path

import numpy as np

# enough that the fitted line is smooth where the camera picks a heading off it
POINTS = 240


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


def planned_walk(scene: Path) -> tuple[np.ndarray, np.ndarray]:
    """(points, up) through the cameras HY-World planned over its own navmesh.

    A 360 camera records where nothing — no poses, no ranges — so there is no
    measurement placing this world in the building. What is left is HY-World's
    own account of itself: the frame it normalised into, and the cameras it
    planned. The line through those is inside the space the model actually
    generated, which is where the splat looks right.
    """
    rows, centre, scale = hyworld_frame(scene)
    up = unit(json.loads((scene / "gs_result" / "ply"
                          / "position_meta_info.json").read_text())["up_direction"])
    cams = json.loads((scene / "gs_data" / "cameras.json").read_text())
    mats = [np.asarray(e["extrinsic"], dtype=np.float64).reshape(4, 4)
            for e in (cams.values() if isinstance(cams, dict) else cams)
            if isinstance(e, dict) and "extrinsic" in e]
    if not mats:
        raise SystemExit(f"{scene}: no training cameras to stand a camera among")
    pts = centre + scale * (np.stack([-m[:3, :3].T @ m[:3, 3] for m in mats]) @ rows)
    return fit_walk(pts, up), up


def fit_walk(pts: np.ndarray, up: np.ndarray) -> np.ndarray:
    """Points along the level line the cameras lie on.

    Levelling against up is what makes it a walk rather than a climb; the fit
    is otherwise least-squares.
    """
    height = float(np.median(pts @ up))
    flat = pts - np.outer(pts @ up, up)
    centred = flat - flat.mean(0)
    axis = unit(np.linalg.svd(centred, full_matrices=False)[2][0])
    t = centred @ axis
    if t[-1] < t[0]:                     # travel the way the cameras were laid out
        axis, t = -axis, -t
    lo, hi = np.percentile(t, [10, 90])
    mid = flat.mean(0) + up * height
    return np.linspace(mid + axis * lo, mid + axis * hi, POINTS)


def write_cam(world: Path, line: np.ndarray, up: np.ndarray) -> None:
    """The pose the viewer spawns at: midway along the line, looking down it.

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
        "source": "midway along the planned cameras",
    }, indent=2))
    print(f"wrote {out}")


def main() -> None:
    scene = Path(sys.argv[1])
    line, up = planned_walk(scene)
    write_cam(scene / "world.ply", line, up)


if __name__ == "__main__":
    main()
