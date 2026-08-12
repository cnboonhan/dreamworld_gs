"""The whole project in one screen: waypoints, quality, progress, addresses.

Four questions that used to be four recipes. They are sections here rather than
separate scripts because they are always read together and always in this order —
where the waypoints are, how good each world came out, how far along the whole
thing is, and what to open.

Each section still delegates to the script that owns that answer, so there is one
implementation of each and this cannot drift from it.

    python scripts/summary.py <assets-dir> <project> [level]
    python scripts/summary.py <assets-dir> <project> --urls    # just the addresses

The addresses are probed rather than asserted: a port that is listed but dead is
the thing actually worth knowing, and /dev/tcp answers for a noVNC screen or an
MJPEG endpoint as readily as for JSON, needing nothing installed.
"""

import json
import os
import socket
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import plan_report                                             # noqa: E402
import splat_quality                                           # noqa: E402
import vertices as vertices_report                             # noqa: E402

INTERACTIVE = int(os.environ.get("DW_INTERACTIVE_PORT", "8086"))
VIEWER = int(os.environ.get("DW_VIEWER_PORT", "8081"))


def rule(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 70 - len(title)))


def listening(port: int, timeout: float = 0.4) -> bool:
    """Whether anything answers on this port, without caring what it serves."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout):
            return True
    except OSError:
        return False


def standing_at(default: str = "<scene>") -> str:
    """The waypoint the interactive server is standing at, for the viewer URL."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{INTERACTIVE}/state", timeout=2) as r:
            return json.load(r)["scene"]
    except (OSError, ValueError, KeyError):
        return default


def urls(project: str) -> None:
    scene = standing_at()
    # ?speed= is how fast a hand-driven walk crosses a corridor, in m/s. The
    # default matches the robot; slower is easier to mark and to watch.
    speed = os.environ.get("DW_VIEWER_SPEED", "")
    # No &agent= row any more: the viewer connects to :8086 on the host it was
    # served from, without being told to. One URL, and the chip in its corner
    # says whether anything is driving it.
    view = (f"http://localhost:{VIEWER}/?url=files/{project}/splats/{scene}/world.ply"
            + (f"&speed={speed}" if speed else ""))
    rows = [
        (4200, "jobs + logs", "http://localhost:4200"),
        (VIEWER, "splat viewer", view),
        (8082, "360 viewer", "http://localhost:8082"),
        (8083, "rmf sim", "http://localhost:8083"),
        (8084, "traffic ed", "http://localhost:8084"),
        (8085, "align panos", "http://localhost:8085          (just align)"),
        (8087, "pano editor", "http://localhost:8087"),
        (INTERACTIVE, "dashboard", f"http://localhost:{INTERACTIVE}"),
        (None, "", ""),
        (INTERACTIVE, "tools api", f"http://localhost:{INTERACTIVE}/tools"),
        (8090, "robot bridge", "http://localhost:8090/state"),
        (8088, "edit model", "http://localhost:8088/health"),
        (8000, "vlm", "http://localhost:8000/v1/models"),
    ]
    print(f"\n  project  {project}   (just use <name> to switch)\n")
    for port, label, url in rows:
        if port is None:
            print()
            continue
        print(f"  {'ok' if listening(port) else '--'} {label:<14} {url}")
    print("\n  ok = answering  ·  -- = not up\n")
    # Built from the same list, so it cannot fall behind it — which it had, twice.
    ports = sorted({p for p, _, _ in rows if p})
    print("  remote?")
    print("    ssh " + " ".join(f"-L {p}:localhost:{p}" for p in ports) + " <this-host>")
    print()


def main() -> int:
    assets, project = Path(sys.argv[1]), sys.argv[2]
    rest = sys.argv[3:]
    if "--urls" in rest:
        urls(project)
        return 0
    level = next((a for a in rest if not a.startswith("--")), "")

    rule("waypoints")
    vertices_report.report(project, level)
    rule("splat quality")
    splat_quality.report(assets / "projects" / project, "--renders" in rest)
    rule("progress")
    plan_report.report(assets / "projects" / project, only_missing="--missing" in rest)
    rule("where to open it")
    urls(project)
    return 0


if __name__ == "__main__":
    sys.exit(main())
