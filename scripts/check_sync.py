"""Are the splat camera and the Gazebo robot walking the same corridor together?

    python scripts/check_sync.py <from> <to> [level]

The two live in different coordinate systems — the robot in building metres, the
camera in whatever frame HY-World normalised that world into — so their positions
cannot be compared directly. What can is HOW FAR ALONG the shared edge each of
them is: a fraction from 0 at one waypoint to 1 at the other. That is the same
number in both frames, and it is the number that has to match for the two to look
coordinated.

So this issues the walk, then polls both several times a second:

  viewer   `along` from /viewer/pose — the tour parameter, already 0..1
  robot    its position from the bridge, projected onto the lane a->b

and reports the largest gap between them. Perfectly in step is 0. A steady
offset means one started late; a growing one means they are running at different
speeds, which is what the shared DRIVE_SPEED and per-edge pacing exist to stop.

Needs a splat viewer connected with ?agent= — without one the walk is robot-only
and there is nothing to compare.
"""

import json
import math
import sys
import time
import urllib.error
import urllib.request

INTERACTIVE = "http://localhost:8086"
BRIDGE = "http://localhost:8090"


def get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return {}


def post(url, body, timeout=180):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "error": str(e)}


def fraction_along(p, a, b):
    """How far p is from a toward b, 0..1, projected onto the segment."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return 0.0
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2
    return max(0.0, min(1.0, t))


def main() -> int:
    start, dest = sys.argv[1], sys.argv[2]
    level = sys.argv[3] if len(sys.argv) > 3 else ""

    if not get(f"{INTERACTIVE}/viewer/pose").get("ok"):
        print("no splat viewer connected — open the ?agent= URL and reload it",
              file=sys.stderr)
        return 1

    post(f"{INTERACTIVE}/reset", {"waypoint": start, "level": level})
    graph = post(f"{INTERACTIVE}/tool", {"tool": "get_graph", "args": {}})
    pos = {v["id"]: (v["x"], v["y"]) for v in graph.get("vertices", [])}
    if start not in pos or dest not in pos:
        print(f"no such waypoint pair on this level: {start} -> {dest}",
              file=sys.stderr)
        return 1
    a, b = pos[start], pos[dest]
    print(f"  {start} {a} -> {dest} {b}   "
          f"{math.dist(a, b):.2f} m\n")

    # Poll while the walk runs. go_to blocks until it lands, so it goes in a
    # thread and the sampling happens here.
    import threading
    result = {}
    t = threading.Thread(target=lambda: result.update(
        post(f"{INTERACTIVE}/tool",
             {"tool": "go_to", "args": {"vertex": dest}})), daemon=True)
    t.start()

    print(f"  {'t':>6} {'viewer':>8} {'robot':>8} {'gap':>7}")
    worst, samples = 0.0, 0
    t0 = time.time()
    while t.is_alive() and time.time() - t0 < 60:
        pose = get(f"{INTERACTIVE}/viewer/pose")
        st = (get(f"{BRIDGE}/state") or {}).get("state") or {}
        if pose.get("ok") and st.get("x") is not None:
            v = float(pose.get("along", 0))
            r = fraction_along((st["x"], st["y"]), a, b)
            gap = abs(v - r)
            worst = max(worst, gap)
            samples += 1
            print(f"  {time.time() - t0:6.2f} {v:8.3f} {r:8.3f} {gap:7.3f}")
        time.sleep(0.15)
    t.join(timeout=5)

    print(f"\n  {result.get('message', '(no result)')}")
    if not samples:
        print("  nothing sampled — the walk was over before the first poll, or "
              "one side never answered")
        return 1
    print(f"  {samples} samples, worst gap {worst:.3f} of the corridor "
          f"({worst * math.dist(a, b):.2f} m)")
    print("  0.00 is perfectly in step. A steady gap means one started late; a\n"
          "  growing one means they are travelling at different speeds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
