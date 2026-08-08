"""Place a corridor splat in the building's coordinate frame.

A COLMAP solve gives a reconstruction that is metric in scale but arbitrary in
origin and orientation. Two splats therefore cannot be shown together, and a
camera position in building metres means nothing to either. This computes where
one belongs and rewrites it there.

Three constraints, all recovered from the capture itself:

  the walk axis   ->  the lane's direction   (from straight_path, which orders
                                              standpoints the way you walked)
  the camera up   ->  the building's up
  the walk centre ->  the lane's midpoint

That is a fully determined rigid transform; scale is already metric, so nothing
is stretched. What the capture *cannot* say is which end you started from — the
axis and the lane agree just as well 180 degrees about up — so that one bit
comes from the capture id by convention, or from a `capture.json` beside the
panoramas when a corridor was walked the other way.

`align_residual_m` is how far the walk's own endpoints land from the lane's. It
is computed from the capture alone, so it means the same thing for a simulated
corridor and a real one.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from render_video import capture_path, straight_path


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else v


def _frame(fwd: np.ndarray, up: np.ndarray) -> np.ndarray:
    """Orthonormal basis as columns, from a forward and a rough up."""
    f = _unit(fwd)
    u = _unit(up - f * float(up @ f))       # up, made perpendicular to forward
    return np.stack([f, u, np.cross(f, u)], axis=1)


def lane(plan: dict, edge_id: str) -> dict | None:
    """The lane an edge id names, with its endpoints in metres."""
    for data in plan["levels"].values():
        for e in data["edges"]:
            if e["id"] != edge_id:
                continue
            pos = {v["id"]: (v["x"], v["y"]) for v in data["vertices"]}
            return {"a": e["a"], "b": e["b"],
                    "pa": np.array([*pos[e["a"]], 0.0]),
                    "pb": np.array([*pos[e["b"]], 0.0]),
                    "length_m": e["length_m"]}
    return None


def walked_from(scene: Path, lane_info: dict) -> str:
    """Which endpoint the walk started at.

    The id sorts its endpoints, and the capture procedure is to walk them in
    that order. A `capture.json` next to the panoramas overrides it, for a
    corridor someone walked the other way."""
    for name in ("capture.json",):
        f = scene / name
        if f.is_file():
            try:
                got = (json.loads(f.read_text()) or {}).get("from")
                if got in (lane_info["a"], lane_info["b"]):
                    return got
            except (OSError, ValueError):
                pass
    return lane_info["a"]


def solve(scene: Path, plan: dict, edge_id: str, panos_dir: Path | None = None):
    """(R, t, report) placing this splat in building coordinates."""
    info = lane(plan, edge_id)
    if info is None:
        return None, None, {"aligned": False, "why": f"'{edge_id}' is not a lane"}

    centres, up = capture_path(scene / "undistorted" / "sparse" / "0")
    if len(centres) < 2:
        return None, None, {"aligned": False,
                            "why": f"only {len(centres)} standpoint(s); need 2+"}
    eyes, axis = straight_path(centres, 2)      # axis runs the way you walked

    start = walked_from(panos_dir or scene, info)
    pa, pb = (info["pa"], info["pb"]) if start == info["a"] else (info["pb"], info["pa"])
    lane_dir = _unit(pb - pa)

    # cameras look level, so their up is the building's up
    R = _frame(lane_dir, np.array([0.0, 0.0, 1.0])) @ _frame(axis, up).T
    t = (pa + pb) / 2.0 - R @ centres.mean(0)

    placed = centres @ R.T + t
    span = float(np.linalg.norm(placed[-1] - placed[0]))
    # Measured along the lane, not straight-line to its ends. A capture weaves
    # across the corridor on purpose, so it is laterally offset from the
    # centreline by design; what would mean the splat is misplaced is arriving
    # at the wrong point *along* the corridor.
    proj = (placed - pa) @ lane_dir
    residual = max(abs(float(proj[0])),
                   abs(float(proj[-1]) - info["length_m"]))
    return R, t, {
        "aligned": True,
        "edge": edge_id,
        "walked_from": start,
        "walk_span_m": round(span, 3),
        "lane_length_m": round(info["length_m"], 3),
        "align_residual_m": round(residual, 3),
        "transform": {"R": [[round(v, 8) for v in row] for row in R.tolist()],
                      "t": [round(v, 6) for v in t.tolist()]},
    }


def _quat_mul(q: np.ndarray, r: np.ndarray) -> np.ndarray:
    """Hamilton product, (w, x, y, z) — the order the PLY stores rotations."""
    w0, x0, y0, z0 = q
    w1, x1, y1, z1 = r[:, 0], r[:, 1], r[:, 2], r[:, 3]
    return np.stack([
        w0 * w1 - x0 * x1 - y0 * y1 - z0 * z1,
        w0 * x1 + x0 * w1 + y0 * z1 - z0 * y1,
        w0 * y1 - x0 * z1 + y0 * w1 + z0 * x1,
        w0 * z1 + x0 * y1 - y0 * x1 + z0 * w1,
    ], axis=1)


def _R_to_quat(R: np.ndarray) -> np.ndarray:
    """Rotation matrix -> (w, x, y, z), via the largest diagonal term so the
    square root is never taken of something near zero."""
    tr = float(np.trace(R))
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        return np.array([0.25 * s, (R[2, 1] - R[1, 2]) / s,
                         (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s])
    i = int(np.argmax(np.diag(R)))
    j, k = (i + 1) % 3, (i + 2) % 3
    s = math.sqrt(1.0 + R[i, i] - R[j, j] - R[k, k]) * 2
    q = np.zeros(4)
    q[0] = (R[k, j] - R[j, k]) / s
    q[i + 1] = 0.25 * s
    q[j + 1] = (R[j, i] + R[i, j]) / s
    q[k + 1] = (R[k, i] + R[i, k]) / s
    return q


def apply_to_ply(ply: Path, R: np.ndarray, t: np.ndarray) -> int:
    """Rewrite a 3DGS PLY in the new frame: move the means, turn the rotations.

    Scales and colours are untouched — this is a rigid motion, so a gaussian
    keeps its shape and, with no view-dependent terms at SH degree 0, its
    appearance."""
    from plyfile import PlyData, PlyElement

    data = PlyData.read(str(ply))
    v = data["vertex"].data.copy()

    xyz = np.stack([v["x"], v["y"], v["z"]], 1)
    moved = xyz @ R.T + t
    v["x"], v["y"], v["z"] = moved[:, 0], moved[:, 1], moved[:, 2]

    quats = np.stack([v[f"rot_{i}"] for i in range(4)], 1)
    turned = _quat_mul(_R_to_quat(R), quats)
    turned /= np.maximum(np.linalg.norm(turned, axis=1, keepdims=True), 1e-12)
    for i in range(4):
        v[f"rot_{i}"] = turned[:, i]

    PlyData([PlyElement.describe(v, "vertex")], text=False).write(str(ply))
    return len(v)
