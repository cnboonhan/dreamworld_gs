"""Report a project's capture plan against what is actually on disk.

build-world writes worlds/<map>/capture_plan.json: one entry per vertex and per
edge of the nav graph, with the id its panoramas belong under. This reads that
back against the project directory, so the gap between what the building has
and what has been photographed is a list rather than a guess — followed by the
quality of every splat already built, so two of them can be compared without
opening two flow runs.

    python plan_report.py <project-dir> [missing]

Used by `just plan`; standalone so the recipe stays a one-liner.
"""

import json
import re
import sys
from pathlib import Path

# how many registered views, as a fraction of those offered, before the
# reconstruction is thin enough to be worth flagging
THIN_REGISTRATION = 0.5
# held-out PSNR below this reads as blurry rather than merely soft
LOW_PSNR = 22.0


def ply_gaussians(ply: Path) -> int | None:
    """Gaussian count from the PLY header, for splats built before the pipeline
    recorded its own metrics. The header is ASCII however large the body is."""
    try:
        with ply.open("rb") as fh:
            head = fh.read(2048).decode("ascii", "replace")
    except OSError:
        return None
    m = re.search(r"element vertex (\d+)", head)
    return int(m.group(1)) if m else None


def splat_metrics(splat: Path) -> dict:
    """What is known about a built splat, from whatever it left behind."""
    info = {}
    f = splat / "world.info.json"
    if f.is_file():
        try:
            info = json.loads(f.read_text())
        except ValueError:
            info = {}
    ply = splat / "world.ply"
    if "gaussians" not in info:
        n = ply_gaussians(ply)
        if n is not None:
            info["gaussians"] = n
            info["partial"] = True          # predates full metric recording
    info["mb"] = round(ply.stat().st_size / 1e6, 1) if ply.is_file() else None
    info["video"] = (splat / "walkthrough.mp4").is_file()
    return info


def fmt(v, spec="", dash="—"):
    return dash if v is None else format(v, spec)


def coverage(doc: dict, root: Path, only_missing: bool) -> tuple[list, int, int]:
    rows, have = [], 0
    for c in doc["capture"]:
        panos = root / c["panos"]
        n = len(list(panos.glob("*"))) if panos.is_dir() else 0
        built = (root / c["splat"] / "world.ply").is_file()
        if n:
            have += 1
        if only_missing and n:
            continue
        rows.append((c["kind"], c["level"], c["id"],
                     "built" if built else (f"{n} panos" if n else "—")))
    return rows, have, len(doc["capture"])


def quality_table(root: Path) -> list[dict]:
    """Every built splat in the project, with what is known about each."""
    out = []
    for kind in ("vertices", "edges"):
        for splat in sorted((root / "splats" / kind).glob("*")):
            if not (splat / "world.ply").is_file():
                continue
            m = splat_metrics(splat)
            m["kind"] = kind[:-1] if kind.endswith("s") else kind
            m["id"] = splat.name
            out.append(m)
    return out


def print_quality(rows: list[dict]) -> None:
    if not rows:
        return
    w = max(max(len(r["id"]) for r in rows), 2)
    print()
    print("built splats")
    print(f"  {'id':{w}}  {'panos':>5} {'reg/views':>10} {'gaussians':>10} "
          f"{'PSNR':>7} {'scale':>7} {'MB':>7}  video")
    for r in rows:
        reg, views = r.get("registered"), r.get("views")
        regs = f"{reg}/{views}" if reg is not None and views else "—"
        flags = []
        if reg is not None and views and reg < views * THIN_REGISTRATION:
            flags.append("thin SfM")
        if r.get("psnr_db") is not None and r["psnr_db"] < LOW_PSNR:
            flags.append("low PSNR")
        if r.get("sfm_models", 1) > 1:
            flags.append(f"{r['sfm_models']} fragments")
        if r.get("partial"):
            flags.append("metrics not recorded — rebuild to fill in")
        print(f"  {r['id']:{w}}  {fmt(r.get('panoramas'), '>5'):>5} "
              f"{regs:>10} {fmt(r.get('gaussians'), '>10,'):>10} "
              f"{fmt(r.get('psnr_db'), '>7.2f'):>7} "
              f"{fmt(r.get('metric_scale'), '>7.4f'):>7} "
              f"{fmt(r.get('mb'), '>7.1f'):>7}  "
              f"{'yes' if r['video'] else '—'}"
              + (f"   ({'; '.join(flags)})" if flags else ""))
    print(f"  -- {len(rows)} splat(s). PSNR is on held-out views: it says the "
          f"splat matches the photos,")
    print(f"     not that the room is fully covered — that is what the capture "
          f"count above tells you.")


def report(project_dir: Path, only_missing: bool = False) -> int:
    plans = sorted(project_dir.glob("worlds/*/capture_plan.json"))
    if not plans:
        print(f"no capture plan in {project_dir} — run: just world",
              file=sys.stderr)
        return 1

    for path in plans:
        doc = json.loads(path.read_text())
        print(f"{doc['project']}/{doc['map']}")
        rows, have, total = coverage(doc, project_dir, only_missing)
        width = max((len(r[2]) for r in rows), default=10)
        for kind, level, cid, state in rows:
            print(f"  {kind:6} {level:5} {cid:{width}}  {state}")
        note = "  (showing only what is missing)" if only_missing else ""
        print(f"  -- {have}/{total} captured{note}")

    print_quality(quality_table(project_dir))
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(report(Path(args[0]), len(args) > 1 and args[1] == "missing"))
