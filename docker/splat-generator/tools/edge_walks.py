"""A vertex world has one walk per corridor leaving it, not one walk.

A world generated from a panorama shot at a waypoint is a view of a junction.
Riding a single line through it picks one corridor arbitrarily and ignores the
rest, which is why a fitted walk through HunyuanWorld's planned cameras wanders
— there is no single right line through a place where three corridors meet.

The map says which corridors leave and where they point. The panorama was
turned to face the building before it was generated from, so a bearing in the
building is a direction in the world up to one number: how many world units a
metre is. That is fitted here by ray-casting the floor plan from the waypoint
and comparing it with the depth HunyuanWorld predicted for the same bearings —
one parameter against a few thousand samples, with position and heading already
known.

Writes <world>.paths.json: one walk per lane, in the world's own coordinates,
each carrying the neighbour it leads to and its length in metres.

    python edge_walks.py <scene-dir>
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import torch
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from make_spawn_cam import POINTS, hyworld_frame, unit  # noqa: E402

PROJECTS = Path("/workspace/projects")
# the band around the horizon a corridor lives in; floor and ceiling are near
# and featureless wherever you stand and would tell us nothing about scale
BAND = (0.42, 0.58)


def building(project: str, level: str):
    """(waypoints in metres, lanes, walls) for one level."""
    root = PROJECTS / project
    b = yaml.safe_load((root / "maps" / f"{project}.building.yaml").read_text())
    plan = json.loads(next((root / "worlds").glob("*/capture_plan.json")).read_text())
    named = {v["id"].split(".", 1)[1]: np.array([v["x"], v["y"]])
             for v in plan["levels"][level]["vertices"]}
    px = {v[3]: np.array(v[:2], dtype=float) for v in b["levels"][level]["vertices"] if v[3]}
    both = sorted(set(px) & set(named))
    fit = np.linalg.lstsq(np.c_[np.stack([px[k] for k in both]), np.ones(len(both))],
                          np.stack([named[k] for k in both]), rcond=None)[0]
    to_m = lambda p: np.r_[np.asarray(p, dtype=float), 1.0] @ fit
    V = b["levels"][level]["vertices"]
    walls = np.array([[to_m(V[w[0]][:2]), to_m(V[w[1]][:2])]
                      for w in b["levels"][level]["walls"]])
    lanes = [(e["a"].split(".", 1)[1], e["b"].split(".", 1)[1], e["length_m"])
             for e in plan["levels"][level]["edges"]]
    return named, lanes, walls


def plan_ranges(here: np.ndarray, walls: np.ndarray, bearings: np.ndarray) -> np.ndarray:
    """Distance to the first wall along each bearing, in metres."""
    d = np.stack([np.cos(bearings), np.sin(bearings)], 1)
    a, b = walls[:, 0], walls[:, 1]
    seg = b - a
    # solve here + t*d = a + u*seg for every (ray, wall) pair
    den = d[:, None, 0] * seg[None, :, 1] - d[:, None, 1] * seg[None, :, 0]
    ok = np.abs(den) > 1e-9
    ap = a[None] - here
    t = np.where(ok, (ap[..., 0] * seg[None, :, 1] - ap[..., 1] * seg[None, :, 0]) / np.where(ok, den, 1), np.inf)
    u = np.where(ok, (ap[..., 0] * d[:, None, 1] - ap[..., 1] * d[:, None, 0]) / np.where(ok, den, 1), np.inf)
    t = np.where((t > 0.05) & (u >= 0) & (u <= 1), t, np.inf)
    return t.min(1)


def units_per_metre(scene: Path, here: np.ndarray, walls: np.ndarray) -> float:
    """How many of this world's units a metre is.

    Position and heading are already known — the waypoint, and a panorama
    turned to face the building — so this is one number. The floor plan gives
    the true distance to a wall along every bearing; HunyuanWorld's own depth
    prediction gives its distance along the same bearing. Their ratio is the
    scale, taken as a median over the horizon band and only where the plan
    says a wall is close enough to have been seen.
    """
    pred = torch.load(scene / "render_results" / "full_depth_prediction.pt",
                      map_location="cpu", weights_only=False)
    dist = pred["distance"].numpy().astype(np.float64)
    mask = np.asarray(pred["mask"])
    H, W = dist.shape
    lo, hi = int(BAND[0] * H), int(BAND[1] * H)
    step = max(1, (hi - lo) // 8), max(1, W // 720)
    rows = np.arange(lo, hi, step[0])
    cols = np.arange(0, W, step[1])
    lon = math.pi - (cols + 0.5) / W * 2 * math.pi
    truth = plan_ranges(here, walls, lon)
    seen = dist[np.ix_(rows, cols)]
    good = mask[np.ix_(rows, cols)] & (seen > 1e-3) & np.isfinite(truth)[None] & (truth < 12)[None]
    ratio = (seen / np.broadcast_to(truth, seen.shape))[good]
    return float(np.median(ratio))


def main() -> None:
    scene = Path(sys.argv[1])
    project = scene.parent.parent.name
    level, waypoint = scene.name.split(".", 1)
    named, lanes, walls = building(project, level)
    if waypoint not in named:
        raise SystemExit(f"{scene.name} is not a waypoint of {level}")
    here = named[waypoint]

    rows, centre, _ = hyworld_frame(scene)
    # a building direction in this world's coordinates. The ray convention is
    # fixed and the panorama is aligned, so this is exact — measured on v6 as a
    # pure rotation, determinant +1, no mirror.
    A = np.diag([-1.0, -1.0, 1.0])
    M = rows.T @ A
    upw = unit(M @ [0.0, 0.0, 1.0])

    # Marked by hand where that has been done: no fit relates a generated
    # world to metres reliably, and across 62 bearings of L11.v6 the geometry
    # implied anywhere from 0.65 to 2.95 units per metre.
    saved = {}
    rec = scene.parent / ".aligned" / f"{scene.name}.json"
    if rec.is_file():
        saved = json.loads(rec.read_text())
    upm = saved.get("units_per_metre") or units_per_metre(scene, here, walls)
    eye = float(saved.get("height") or 0.0)
    pitch = math.radians(float(saved.get("pitch_deg") or 0.0))
    out = []
    for a, b, metres in lanes:
        other = b if a == waypoint else (a if b == waypoint else None)
        if other is None:
            continue
        d = named[other] - here
        bearing = math.atan2(d[1], d[0])
        direction = unit(M @ [math.cos(bearing), math.sin(bearing), 0.0])
        # a lane marked by hand overrides the fit for its own corridor
        span = saved.get("lanes", {}).get(other, {}).get("units") or metres * upm
        line = (centre + eye * upw
                + np.outer(np.linspace(0, span, POINTS), direction))
        look = unit(direction + math.tan(pitch) * upw)
        out.append({"to": other, "metres": round(float(metres), 3),
                    "bearing": round(bearing, 5),
                    "look": [round(float(v), 5) for v in look],
                    "points": [[round(float(v), 5) for v in p] for p in line]})

    doc = {"waypoint": f"{level}.{waypoint}", "at": [round(float(v), 3) for v in here],
           "up": [round(float(v), 5) for v in upw],
           "units_per_metre": round(upm, 4), "height": round(eye, 4),
           "pitch_deg": round(math.degrees(pitch), 2),
           "marked": sorted(saved.get("lanes", {})), "walks": out}
    (scene / "world.paths.json").write_text(json.dumps(doc))
    print(f"{scene.name}: {upm:.3f} units/m, {len(out)} lane(s) — "
          + ", ".join(f"{w['to']} {w['metres']}m" for w in out))


if __name__ == "__main__":
    main()
