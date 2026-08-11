"""Every waypoint's id, and where to find it in the traffic editor.

Three numberings exist and none of them agree, which is a reliable way to point
a panorama at the wrong place:

  the drawing   building.yaml numbers all of a level's vertices, wall corners
                included — 230 on L11, of which 20 are traversable
  the nav graph nav_graphs/0.yaml keeps only the traversable ones, renumbered
                from zero
  the id        a vertex's name when the editor gave it one, else v<nav index>

So L11.v6 is nav vertex 6, which is drawing vertex 216 — the two diverge at the
first drawing vertex the nav graph skips, and after that no offset relates
them. This prints the correspondence, so a waypoint id can be found on the
drawing and a drawing vertex can be named.

    python scripts/vertices.py [project] [level]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import yaml

REPO = Path(__file__).resolve().parent.parent


def main() -> None:
    report(sys.argv[1] if len(sys.argv) > 1 else "multilevel_office",
           sys.argv[2] if len(sys.argv) > 2 else "")


def report(project: str, only: str = "") -> None:
    root = REPO / "assets" / "projects" / project
    b = yaml.safe_load((root / "maps" / f"{project}.building.yaml").read_text())
    nav = yaml.safe_load(next((root / "worlds").glob("*/nav_graphs/0.yaml")).read_text())

    for level, graph in nav["levels"].items():
        if only and level != only:
            continue
        drawing = b["levels"][level]["vertices"]
        nodes = graph["vertices"]
        # the two files disagree in units, so the map between them is fitted
        # from the vertices named in both rather than assumed
        named_px = {v[3]: np.array(v[:2], dtype=float) for v in drawing if v[3]}
        named_m = {v[2].get("name"): np.array(v[:2]) for v in nodes if v[2].get("name")}
        both = sorted(set(named_px) & set(named_m))
        if len(both) < 3:
            print(f"{level}: only {len(both)} vertices named in both files, "
                  f"not enough to relate them")
            continue
        M = np.stack([named_m[k] for k in both])
        P = np.stack([named_px[k] for k in both])
        fit = np.linalg.lstsq(np.c_[M, np.ones(len(M))], P, rcond=None)[0]

        lanes: dict[int, list[str]] = {}
        for e in graph.get("lanes", []):
            for i, j in ((e[0], e[1]), (e[1], e[0])):
                lanes.setdefault(i, []).append(
                    nodes[j][2].get("name") or f"v{j}")

        print(f"\n{level}: {len(nodes)} waypoints of {len(drawing)} drawing vertices")
        print(f"{'id':24} {'nav':>4} {'drawing':>8} {'x, y (px)':>16}   lanes to")
        for i, v in enumerate(nodes):
            name = v[2].get("name") or ""
            px = np.r_[np.asarray(v[:2], dtype=float), 1.0] @ fit
            j = int(np.argmin([np.linalg.norm(np.asarray(w[:2], dtype=float) - px)
                               for w in drawing]))
            print(f"{level}.{name or 'v' + str(i):23} {i:>4} {j:>8} "
                  f"{px[0]:>7.1f},{px[1]:>7.1f}   "
                  f"{', '.join(sorted(set(lanes.get(i, [])))) or '-'}")


if __name__ == "__main__":
    main()
