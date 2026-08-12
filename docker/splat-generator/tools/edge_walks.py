"""A vertex world has one walk per corridor leaving it, not one walk.

A world generated from a panorama shot at a waypoint is a view of a junction.
Riding a single line through it picks one corridor arbitrarily and ignores the
rest, which is why a fitted walk through HunyuanWorld's planned cameras wanders
— there is no single right line through a place where three corridors meet.

The map says which corridors leave a waypoint and where they point, and the
panorama was turned to face the building before it was generated from, so a
bearing in the building is a direction in the world. What the map cannot give
is how far a metre is here — no fit did either, and across 62 bearings of
L11.v6 the geometry implied anywhere from 0.65 to 2.95 units per metre. So a
walk exists only between two positions marked by hand, and a lane without them
is published as a direction and nothing more.

Writes <world>.paths.json: the waypoint's origin, its lanes, and a walk for
each pair of marked ends.

    python edge_walks.py <scene-dir>
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from make_spawn_cam import POINTS, hyworld_frame, unit  # noqa: E402

PROJECTS = Path("/workspace/projects")


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
    lanes = [(e["a"].split(".", 1)[1], e["b"].split(".", 1)[1], e["length_m"])
             for e in plan["levels"][level]["edges"]]
    return named, lanes


def main() -> None:
    scene = Path(sys.argv[1])
    project = scene.parent.parent.name
    if "." not in scene.name:
        print(f"{scene.name}: not a waypoint id, no lanes")
        return
    level, waypoint = scene.name.split(".", 1)
    named, lanes = building(project, level)
    if waypoint not in named:
        # an edge world, or a loose panorama: no lanes leave it, so there is
        # nothing to walk between. Not a failure — most scenes are not
        # waypoints, and export runs this over all of them.
        print(f"{scene.name}: not a waypoint of {level}, no lanes")
        return
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
    # The waypoint's OWN position needs no marking — the panorama was shot
    # standing at it, so the frame's centre is where it is, and asking someone
    # to mark the place they are already standing is asking them to confirm the
    # one thing this world is certain of. An explicit mark still wins, for the
    # case where the generated centre is off.
    placed = {waypoint: [round(float(v), 5) for v in centre],
              **saved.get("placed", {})}
    out, lanes_out = [], []
    for a, b, metres in lanes:
        other = b if a == waypoint else (a if b == waypoint else None)
        if other is None:
            continue
        d = named[other] - here
        bearing = math.atan2(d[1], d[0])
        # the map's bearing through the panorama alignment: the only direction
        # there is for a corridor nobody has marked yet
        direction = unit(M @ [math.cos(bearing), math.sin(bearing), 0.0])
        marked = waypoint in placed and other in placed
        if marked:
            # Two marked positions beat any fit — for the lane as much as the
            # walk. The viewer aims a departure, faces a neighbour and turns an
            # arrival from lane.dir, but rides the walk between the marks; a
            # lane that disagrees with its own walk aims one way and travels
            # another. On L11.v9->v7 the bearing (aligned when this world was
            # mislabelled v6) sat 148 degrees off the marks, and every leg
            # touching that edge landed facing backwards.
            start = np.asarray(placed[waypoint], dtype=np.float64)
            end = np.asarray(placed[other], dtype=np.float64)
            direction = unit(end - start)
        lanes_out.append({"to": other, "metres": round(float(metres), 3),
                          "bearing": round(bearing, 5),
                          "dir": [round(float(v), 5) for v in direction]})
        # No walk until both ends are marked. A default one would be drawn from
        # a scale that was wrong by 2x and direction-dependent by 4.5x, and a
        # wrong walk is harder to notice than a missing one.
        if not marked:
            continue
        line = np.linspace(start, end, POINTS)
        out.append({"to": other, "metres": round(float(metres), 3),
                    "bearing": round(bearing, 5),
                    "points": [[round(float(v), 5) for v in p] for p in line]})

    doc = {"waypoint": f"{level}.{waypoint}", "at": [round(float(v), 3) for v in here],
           "up": [round(float(v), 5) for v in upw],
           # where the panorama was shot, in this world's own coordinates. A
           # world nobody has marked yet still has to be arrived at somewhere,
           # and this is the one point it knows about itself.
           "origin": [round(float(v), 5) for v in centre],
           # `placed` as the viewer sees it, including the implicit self-mark,
           # so a waypoint shows as placed without anyone having placed it.
           "lanes": lanes_out,
           "placed": placed,
           "walks": out}
    (scene / "world.paths.json").write_text(json.dumps(doc))

    # the viewer's world list, served by nginx like everything else, so the
    # browser needs nothing but the files it is already reading
    index = []
    for d in sorted(scene.parent.iterdir()):
        paths = d / "world.paths.json"
        if not (d / "world.ply").is_file() or not paths.is_file():
            continue
        p = json.loads(paths.read_text())
        index.append({"scene": d.name, "lanes": len(p.get("lanes", [])),
                      "walks": len(p.get("walks", [])),
                      "placed": len(p.get("placed", {}))})
    (scene.parent / "scenes.json").write_text(json.dumps(index))
    print(f"{scene.name}: {len(lanes_out)} lane(s), {len(out)} walk(s) from "
          f"{len(saved.get('placed', {}))} marked vertex/vertices")


if __name__ == "__main__":
    main()
