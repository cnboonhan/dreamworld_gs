"""Report a project's capture plan against what is actually on disk.

build-world writes worlds/<map>/capture_plan.json: one entry per vertex and per
edge of the nav graph, with the id its panoramas belong under. This reads that
back against the project directory, so the gap between what the building has
and what has been photographed is a list rather than a guess.

    python plan_report.py <project-dir> [missing]

Used by `just plan`; standalone so the recipe stays a one-liner.
"""

import json
import sys
from pathlib import Path


def report(project_dir: Path, only_missing: bool = False) -> int:
    plans = sorted(project_dir.glob("worlds/*/capture_plan.json"))
    if not plans:
        print(f"no capture plan in {project_dir} — run: just world", file=sys.stderr)
        return 1

    for path in plans:
        doc = json.loads(path.read_text())
        print(f"{doc['project']}/{doc['map']}")
        rows, have = [], 0
        for c in doc["capture"]:
            panos = project_dir / c["panos"]
            n = len(list(panos.glob("*"))) if panos.is_dir() else 0
            built = (project_dir / c["splat"] / "world.ply").is_file()
            if n:
                have += 1
            if only_missing and n:
                continue
            rows.append((c["kind"], c["level"], c["id"],
                         "built" if built else (f"{n} panos" if n else "—")))
        width = max((len(r[2]) for r in rows), default=10)
        for kind, level, cid, state in rows:
            print(f"  {kind:6} {level:5} {cid:{width}}  {state}")
        total = len(doc["capture"])
        note = "  (showing only what is missing)" if only_missing else ""
        print(f"  -- {have}/{total} captured{note}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(report(Path(args[0]), len(args) > 1 and args[1] == "missing"))
