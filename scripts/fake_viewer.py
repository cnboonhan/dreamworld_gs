"""A splat viewer with no splats: connects, obeys, and reports where it is.

    python scripts/fake_viewer.py [--url http://localhost:8086]

It speaks the same protocol as the real viewer — opens /viewer/events, executes
each command, posts /viewer/done — and walks a corridor by advancing a fraction
from 0 to 1 over the duration the pace says it should take. That is all the real
viewer does with the timing; the rest of it is drawing.

What this can therefore test: that the server paces a walk correctly, that a turn
takes as long as the robot's, that both start together, and that go_to blocks
until the walk lands. `just sync` measures exactly that against the robot.

What it CANNOT test: anything about the picture. Which way the camera faces, what
a handover looks like, whether a corridor is where the panorama said — every one
of those needs the real viewer and a person looking at it. A green run here means
the protocol and the timing are right, not that the walkthrough looks right.
"""

import argparse
import json
import threading
import time
import urllib.request

STATE = {"scene": "", "along": 0.0, "walking": False, "yaw": 0.0}


def post(base, path, body):
    req = urllib.request.Request(f"{base}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except Exception:                                          # noqa: BLE001
        return {}


def handle(base, cmd):
    """Run one command, then report done — in a thread, so the event stream is
    never blocked by a walk that takes a second and a half."""
    op = cmd.get("op")
    pace = cmd.get("pace") or {}
    motion = cmd.get("motion") or {}
    res = {"ok": True, "at": STATE["scene"]}

    if op == "walk":
        secs = (motion.get("walk_ms") or 1000) / 1000.0
        turn = (motion.get("turn_ms") or 0) / 1000.0
        if turn:                                    # face the leg, as the robot does
            time.sleep(turn)
        STATE["walking"] = True
        t0 = time.time()
        while True:                                 # advance 0 -> 1 over `secs`
            f = 1.0 if secs <= 0 else min(1.0, (time.time() - t0) / secs)
            STATE["along"] = f
            if f >= 1.0:
                break
            time.sleep(0.02)
        STATE["walking"] = False
        STATE["scene"] = cmd.get("scene") or STATE["scene"]
        # the handover: the real viewer unpacks ~50MB here
        time.sleep(0.2)
        STATE["along"] = 0.0
        res["at"] = STATE["scene"]
    elif op == "face":
        time.sleep((motion.get("turn_ms") or 0) / 1000.0)
        res["facing"] = cmd.get("to")
    elif op == "stand":
        STATE["scene"] = cmd.get("scene") or STATE["scene"]
        res["at"] = STATE["scene"]
    elif op in ("where", "pose"):
        res.update(at=STATE["scene"], along=round(STATE["along"], 3),
                   walking=STATE["walking"], position=None, forward=None,
                   note="headless stand-in — no geometry, only timing")
    post(base, "/viewer/done", {"id": cmd.get("id"), **res})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://localhost:8086")
    a = ap.parse_args()

    st = post(a.url, "/viewer/hello", {"at": ""}) or {}
    STATE["scene"] = st.get("expect", "")
    print(f"headless viewer attached to {a.url}, standing at "
          f"{STATE['scene'] or '?'}", flush=True)
    print("timing only — nothing here says the picture is right", flush=True)

    # The event stream, read line by line as SSE.
    with urllib.request.urlopen(f"{a.url}/viewer/events", timeout=None) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                cmd = json.loads(line[5:].strip())
            except ValueError:
                continue
            print(f"  <- {cmd.get('op')} {cmd.get('to') or cmd.get('scene') or ''}",
                  flush=True)
            threading.Thread(target=handle, args=(a.url, cmd), daemon=True).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
