"""Report a project's capture plan against what is actually on disk.

build-world writes worlds/<map>/capture_plan.json: every waypoint of the nav
graph, and the lanes between them. A waypoint is what you photograph — one
panorama, one generated world — so this reads that back against the project
directory, and the gap between what the building has and what has been shot is
a list rather than a guess.

Four things have to be true before you can walk out of a waypoint, and the
state column is whichever is missing first:

  1. a panorama of it exists                    panos/<id>.jpg
  2. it has been turned to face the building    scripts/align_panos.py
  3. a world has been generated from it         just generate <id>
  4. its neighbours are marked in that world    the viewer's edge panel

    python plan_report.py <project-dir> [missing]

Used by `just plan`; standalone so the recipe stays a one-liner.
"""

import json
import sys
from pathlib import Path

IMAGES = (".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG")


def pano_of(panos: Path, vid: str) -> Path | None:
    """The panorama for a waypoint, whatever extension the camera wrote."""
    for ext in IMAGES:
        f = panos / (vid + ext)
        if f.is_file():
            return f
    return None


def marks(splat: Path) -> tuple[int, int]:
    """(lanes marked, lanes total) in a generated world.

    A walk exists only where both ends are marked, so this counts the lanes
    that have a walk against the lanes leaving the waypoint at all.
    """
    f = splat / "world.paths.json"
    if not f.is_file():
        return 0, 0
    try:
        doc = json.loads(f.read_text())
    except ValueError:
        return 0, 0
    return len(doc.get("walks") or []), len(doc.get("lanes") or [])


def state_of(root: Path, vid: str) -> tuple[str, bool]:
    """(what stage this waypoint has reached, whether it is finished)."""
    panos = root / "panos"
    pano = pano_of(panos, vid)
    if not pano:
        return "—", False
    if not (panos / ".aligned" / (pano.name + ".json")).is_file():
        return "shot, not aligned", False
    splat = root / "splats" / vid
    if not (splat / "world.ply").is_file():
        return "aligned, not generated", False
    done, total = marks(splat)
    if not total:
        return "built, no lanes", False
    if done < total:
        return f"built, {done}/{total} lanes walkable", False
    return f"built, {total} lanes walkable", True


def report(project_dir: Path, only_missing: bool = False) -> int:
    plans = sorted(project_dir.glob("worlds/*/capture_plan.json"))
    if not plans:
        print(f"no capture plan in {project_dir} — run: just world",
              file=sys.stderr)
        return 1

    for path in plans:
        doc = json.loads(path.read_text())
        rows, ready = [], 0
        ids = [v["id"] for data in doc["levels"].values()
               for v in data["vertices"]]
        for vid in ids:
            state, ok = state_of(project_dir, vid)
            ready += ok
            if not (only_missing and ok):
                rows.append((vid.split(".")[0], vid, state))
        print(f"{doc['project']}/{doc['map']}")
        if rows:
            width = max(len(r[1]) for r in rows)
            for level, vid, state in rows:
                print(f"  {level:5} {vid:{width}}  {state}")
        note = "  (showing only what is unfinished)" if only_missing else ""
        print(f"  -- {ready}/{len(ids)} waypoints walkable{note}")

    known = {v["id"] for p in plans
             for data in json.loads(p.read_text())["levels"].values()
             for v in data["vertices"]}
    for splat in sorted(project_dir.glob("splats/*/world.ply")):
        if splat.parent.name not in known:
            print(f"  (splats/{splat.parent.name} is built but is not a "
                  f"waypoint of any map)")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(report(Path(args[0]), len(args) > 1 and args[1] == "missing"))
