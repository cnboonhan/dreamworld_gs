#!/usr/bin/env python3
"""interactive — drive the building by tool call: the splat viewer walks it, the
Galaxea R1 walks it in Gazebo, and both stay edge-for-edge in step.

Ported from dreamworld/docker/dream_interactive/interactive.py. The tool surface is
the same surface, name for name and argument for argument, so a client written
against the dream dashboard drives this one unchanged:

    go_to  turn  face  open_door  close_door        navigation
    take_lift  select_lift  call_lift               lifts
    pick  place                                     items
    plan_route  where  get_path  get_graph          planning / report
    write_mission  write_todos

What changed is what a tool call *rolls out onto*. The dream stitched pre-rendered
library clips into an MJPEG pane; here there is no video to stitch. Each waypoint is
a gaussian-splat world of its own, so a walk is the viewer riding that world's marked
corridor and handing over at the vertex — live, at whatever framerate the box can
draw. The viewer connects to /viewer/events and reports back on /viewer/done, so the
tool call does not return until the walk has actually landed.

The robot half is unchanged: every traversal is mirrored onto POST /goto of the
galaxea bridge (docker/rmf-tools/robot_bridge.py), doors on the path are opened
through it, and it is the bridge's RMF state that says a motion finished. Set
GALAXEA_URL to enable it; with it unset the viewer walks alone.

    python interactive.py --nav <nav_graphs/0.yaml> --building <x.building.yaml>
                          --level L11 --start lift_lobby --port 8086
"""

import argparse
import collections
import functools
import heapq
import json
import math
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request

import yaml
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

# The viewer is served by nginx on another port, so every route it touches has to
# answer a cross-origin request. It is one page on this box talking to one server
# on this box; there is nothing here to protect from itself.
@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# The robot's pacing, and now the camera's. These are the bridge's own defaults,
# read from the same environment so there is one source of truth: the splat walk
# is live rather than rendered, so nothing makes it take the right length of time
# unless it is told how fast the robot moves.
DRIVE_SPEED = float(os.environ.get("DRIVE_SPEED", "2.0"))   # m/s along a leg
TURN_RATE = float(os.environ.get("TURN_RATE", "1.25"))      # rad/s in place

ST = {}                       # the live world state, guarded by ST["lock"]
TOOLS = {}


# ---- nav graph --------------------------------------------------------------
# Ported from dreamworld/docker/common/nav_path.py, so the graph this reasons over
# is the graph the robot bridge drives over: same file, same indices, same costs.
def load_nav(nav_yaml, level=None):
    data = yaml.safe_load(open(nav_yaml))
    levels = data.get("levels") or {}
    if not levels:
        raise SystemExit(f"no levels in {nav_yaml}")
    if level is None or level not in levels:
        level = next(iter(levels))
    lvl = levels[level]
    verts = []
    for v in lvl.get("vertices", []):
        p = v[2] if len(v) > 2 and isinstance(v[2], dict) else {}
        verts.append((p.get("name", ""), float(v[0]), float(v[1])))
    adj = {i: [] for i in range(len(verts))}
    # Lanes are directed [i, j, params]; a bidirectional lane appears as both
    # [i,j] and [j,i], so adding each as one directed edge is exact.
    for lane in lvl.get("lanes", []):
        i, j = int(lane[0]), int(lane[1])
        cost = math.hypot(verts[j][1] - verts[i][1], verts[j][2] - verts[i][2])
        adj[i].append((j, cost))
    return level, verts, adj, lvl


def levels_of(nav_yaml):
    """Every level the nav graph declares, so a level argument can be checked."""
    return list((yaml.safe_load(open(nav_yaml)).get("levels") or {}))


def doors_of(nav_yaml, level):
    """{door_name: params} for the doors on a level, straight out of the graph."""
    data = yaml.safe_load(open(nav_yaml))
    return {n: d for n, d in (data.get("doors") or {}).items()
            if d.get("map") in (None, level)}


def lift_of(nav_yaml, level):
    """{vertex index: lift name} for the lift-cabin vertices on a level."""
    data = yaml.safe_load(open(nav_yaml))
    lvl = (data.get("levels") or {}).get(level) or {}
    out = {}
    for i, v in enumerate(lvl.get("vertices", [])):
        p = v[2] if len(v) > 2 and isinstance(v[2], dict) else {}
        name = p.get("lift_cabin") or p.get("lift")
        if name:
            out[i] = name if isinstance(name, str) else str(name)
    return out


def find_vertex(verts, key):
    """A waypoint by name, by v<index>, or by bare index — the three ways every
    other part of this repo spells the same thing."""
    key = str(key).strip()
    for i, (n, _, _) in enumerate(verts):
        if n and n.lower() == key.lower():
            return i
    m = re.fullmatch(r"v?(\d+)", key, re.I)
    if m and int(m.group(1)) < len(verts):
        return int(m.group(1))
    raise SystemExit(f"no waypoint {key!r}")


def dijkstra(adj, start, goal):
    dist, prev, heap = {start: 0.0}, {}, [(0.0, start)]
    while heap:
        d, u = heapq.heappop(heap)
        if u == goal:
            path = [u]
            while u in prev:
                u = prev[u]
                path.append(u)
            return list(reversed(path))
        if d > dist.get(u, float("inf")):
            continue
        for v, cost in adj.get(u, []):
            nd = d + cost
            if nd < dist.get(v, float("inf")):
                dist[v], prev[v] = nd, u
                heapq.heappush(heap, (nd, v))
    return None


def lab(i):
    """The label a waypoint answers to: its name, or v<index> when it has none."""
    n = ST["verts"][i][0]
    return n if n else f"v{i}"


def scene_of(i):
    """The splat world for a waypoint — <level>.<label>, the id `just generate` built."""
    return f"{ST['level']}.{lab(i)}"


def leg_motion(u, v):
    """The exact turn and travel for the leg u->v, in the nav graph's own frame.

    Both ends of every edge are known in both frames — the graph has them in
    metres, and the marked walk has them in the splat world's own coordinates —
    so the motion is not something to approximate from a rate and hope both sides
    landed on the same answer. The arc is the difference between the bearing we
    are holding and the bearing of this leg, taken the short way round exactly as
    drive_path takes it; the distance is the lane's. Handing both sides the same
    two numbers is what makes them the same motion rather than two motions timed
    alike.
    """
    heading = ST.get("yaw")
    if heading is None:
        heading = _az(ST["prev"], u) if ST.get("prev") is not None else _az(u, v)
    target = _az(u, v)
    arc = (target - heading + math.pi) % (2 * math.pi) - math.pi   # shortest turn
    metres = math.hypot(ST["verts"][v][1] - ST["verts"][u][1],
                        ST["verts"][v][2] - ST["verts"][u][2])
    return {"arc": round(arc, 6), "yaw": round(target, 6),
            "metres": round(metres, 4),
            "turn_ms": round(abs(arc) / TURN_RATE * 1000),
            "walk_ms": round(metres / DRIVE_SPEED * 1000)}


def _az(a, b):
    """Bearing from waypoint a to waypoint b, in the nav graph's own frame."""
    return math.atan2(ST["verts"][b][2] - ST["verts"][a][2],
                      ST["verts"][b][1] - ST["verts"][a][1])


# ---- event bus: one queue per listener --------------------------------------
class Bus:
    """Fan out state to every listener. Ported from the dream dashboard's
    Broadcaster; a slow listener drops its own events rather than stalling the
    walk for everyone else."""

    def __init__(self):
        self.qs, self.lock = [], threading.Lock()
        self.recent = collections.deque(maxlen=60)

    def listen(self):
        q = queue.Queue(maxsize=64)
        with self.lock:
            self.qs.append(q)
        return q

    def drop(self, q):
        with self.lock:
            if q in self.qs:
                self.qs.remove(q)

    def recent_log(self, n=12):
        return list(self.recent)[-n:]

    def send(self, msg):
        if msg.get("type") in ("log", "tool"):
            with self.lock:
                self.recent.append(msg.get("text") or
                                   f"{msg.get('tool')} -> {json.dumps(msg.get('result'))[:160]}")
        with self.lock:
            targets = list(self.qs)
        for q in targets:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


BUS = Bus()          # to the dashboard page (state, logs)
VIEWER = Bus()       # to the splat viewer (commands)


def log(text, level="ok"):
    print(f"[interactive] {text}", flush=True)
    BUS.send({"type": "log", "level": level, "text": text})


# ---- the splat viewer, as the rollout ---------------------------------------
# A command goes out on /viewer/events and the viewer answers on /viewer/done. The
# call blocks on that answer, because a tool that returned before the camera moved
# would let an agent queue its next step into a world it has not reached.
def viewer_call(op, timeout=180, **kw):
    # No viewer is a fine state, not an error: it is one of two rollouts, and the
    # robot is the other. Saying "ok, but nothing was shown" lets the walk proceed
    # on the robot alone — which is what a box with no browser open should do.
    if not ST.get("viewer_up"):
        return {"ok": True, "no_viewer": True}
    cid = f"c{int(time.time() * 1000)}_{ST['seq']}"
    ST["seq"] += 1
    done = threading.Event()
    box = {}

    with ST["lock"]:
        ST["waiting"][cid] = (done, box)
    VIEWER.send({"id": cid, "op": op,
                 "pace": {"speed": DRIVE_SPEED, "turn_rate": TURN_RATE}, **kw})
    if not done.wait(timeout):
        with ST["lock"]:
            ST["waiting"].pop(cid, None)
        return {"ok": False, "error": f"viewer did not finish {op} within {timeout}s"}
    return box.get("res") or {"ok": False, "error": "empty result"}


# ---- the galaxea bridge, as the robot ---------------------------------------
# Every one of these mirrors a viewer motion onto the real sim. Unset GALAXEA_URL
# and they all no-op, so the viewer half runs on a box with no Gazebo at all.
def bridge(path, body=None, timeout=10):
    url = ST.get("galaxea")
    if not url:
        return {}
    try:
        req = urllib.request.Request(
            f"{url}{path}",
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="GET" if body is None else "POST")
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        log(f"galaxea {path}: {e}", "err")
        return {}


def drive_robot(path):
    """Send the robot down the same waypoints the viewer is about to walk."""
    if not ST.get("galaxea"):
        return
    pts = [[ST["verts"][i][1], ST["verts"][i][2]] for i in path]
    ST["moving"] = True
    try:
        bridge("/goto", {"waypoints": pts, "level": ST["level"]})
    finally:
        ST["moving"] = False


def turn_robot(yaw):
    if ST.get("galaxea"):
        bridge("/turn", {"yaw": yaw})


def robot_state():
    return (bridge("/state") or {}).get("state") or {}


def wait_robot(x, y, timeout=90, tol=0.6):
    """Block until the bridge's RMF state puts the robot at (x, y).

    This is the completion gate the dream harness used, kept for the same reason:
    the viewer finishing its walk says the *camera* arrived, and only the robot's
    own state says the robot did.
    """
    if not ST.get("galaxea"):
        return True
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = robot_state()
        if s.get("x") is not None and math.hypot(s["x"] - x, s["y"] - y) <= tol:
            return True
        time.sleep(0.3)
    return False


# ---- doors ------------------------------------------------------------------
def door_between(u, v):
    """The door on the lane u->v, by name, or "" if it is a clear corridor."""
    ax, ay = ST["verts"][u][1], ST["verts"][u][2]
    bx, by = ST["verts"][v][1], ST["verts"][v][2]

    def _cross(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)

    for name, d in ST["doors"].items():
        try:
            (e1x, e1y), (e2x, e2y) = d["endpoints"]
        except (KeyError, TypeError, ValueError):
            continue
        d1 = _cross(e1x, e1y, e2x, e2y, ax, ay)
        d2 = _cross(e1x, e1y, e2x, e2y, bx, by)
        d3 = _cross(ax, ay, bx, by, e1x, e1y)
        d4 = _cross(ax, ay, bx, by, e2x, e2y)
        if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
            return name
    return ""


DOOR_OPEN = 2      # rmf_lift_msgs LiftState.DOOR_OPEN


def lift_states():
    """{lift: {floor, door, motion}} from the bridge, or {} without one."""
    return (bridge("/lift_state") or {}).get("lifts") or {}


def crossable(u, v):
    """False if the edge u->v is an OBSTACLE — a door or a lift that has not been
    opened. Ported from the dream's cross(): there a door edge is one the clip
    manifest has a door_open clip for, and it blocks unless already opened. There
    is no manifest here, so a door edge is one the nav graph puts a door on, or one
    that enters a lift cabin — liblift owns those, so they are in no door list and
    have to come from lift_of, exactly as the dream identified them.
    """
    lift = ST["lift_of"].get(u) or ST["lift_of"].get(v)
    if lift:
        return lift in ST["open_doors"]
    name = door_between(u, v)
    return not name or name in ST["open_doors"]


def route_stop(path):
    """(reached, obstacle) for walking `path`, opening nothing.

    Ported from build_route_stop: walk from path[0], stopping BEFORE the first
    unopened door/lift edge. `reached` is the path index that can be walked to;
    `obstacle` is the blocking (u, v) edge, or None when the whole path is clear.
    """
    for k in range(len(path) - 1):
        u, v = path[k], path[k + 1]
        if not crossable(u, v):
            return k, (u, v)
    return len(path) - 1, None


def blocked_message(tgt, path, reached, obstacle):
    """The dream's own BLOCKED wording, which the agent prompt is written against."""
    u, v = obstacle
    lift = ST["lift_of"].get(v) or ST["lift_of"].get(u)
    what = f"lift to {lab(v)}" if lift else f"door to {lab(v)}"
    act = "call_lift then open_door" if lift else "open_door"
    if reached == 0:                                  # the obstacle is right here
        return (f"BLOCKED: the path to {lab(tgt)} is blocked by the {what} at "
                f"{lab(ST['cur'])}. go_to only walks clear paths — {act} here first, "
                f"then go_to.")
    return (f"BLOCKED: the path to {lab(tgt)} is blocked by the {what} beyond "
            f"{lab(path[reached])}. go_to {lab(path[reached])} first (that leg "
            f"is clear), then {act}, then go_to {lab(tgt)}.")


# ---- state ------------------------------------------------------------------
def built_scenes():
    """The waypoints that actually have a splat world on disk.

    Read per call rather than cached: worlds land one at a time over hours, and a
    map that still showed yesterday's coverage would be the least useful part of
    the page.
    """
    root = os.path.join("/projects", ST.get("project", ""), "splats")
    try:
        names = os.listdir(root)
    except OSError:
        return []
    pre = ST["level"] + "."
    return sorted(n[len(pre):] for n in names
                  if n.startswith(pre)
                  and os.path.isfile(os.path.join(root, n, "world.ply")))


def state_dict():
    cur = ST["cur"]
    m2px, px, py, dirv = ST.get("m2px"), None, None, None
    if m2px:
        px, py = m2px(ST["verts"][cur][1], ST["verts"][cur][2])
        px, py = round(px, 1), round(py, 1)
        # Prefer the heading actually being held over the waypoint being faced:
        # after a reset there is a heading but nothing is "faced", and the marker
        # fell back to a directionless dot for want of one.
        # After a teleport or a level change the model holds no yaw of its own;
        # the robot's last reported one is still true, and without it the marker
        # fell back to a directionless dot — which is what "the triangle keeps
        # becoming a circle" was. It was never a regression, it was this branch.
        yaw = ST.get("yaw")
        if yaw is None:
            yaw = ST.get("yaw_live")
        if yaw is not None:
            ex, ey = m2px(ST["verts"][cur][1] + math.cos(yaw),
                          ST["verts"][cur][2] + math.sin(yaw))
            d = math.hypot(ex - px, ey - py) or 1.0
            dirv = [(ex - px) / d, (ey - py) / d]
        elif ST["face"] is not None:
            fx, fy = m2px(ST["verts"][ST["face"]][1], ST["verts"][ST["face"]][2])
            d = math.hypot(fx - px, fy - py) or 1.0
            dirv = [(fx - px) / d, (fy - py) / d]
    heading = None if ST["face"] is None else round(math.degrees(_az(cur, ST["face"])) % 360)
    return {"level": ST["level"], "cur": cur, "at": lab(cur), "scene": scene_of(cur),
            "cur_label": lab(cur), "px": px, "py": py, "dir": dirv, "heading": heading,
            "door_open": ", ".join(sorted(ST["open_doors"])) or "",
            "neighbors": [{"id": v, "label": lab(v), "facing": v == ST["face"]}
                          for v, _ in ST["adj"].get(cur, [])],
            "prev": None if ST["prev"] is None else lab(ST["prev"]),
            "face": None if ST["face"] is None else lab(ST["face"]),
            "neighbours": [lab(v) for v, _ in ST["adj"].get(cur, [])],
            "open_doors": sorted(ST["open_doors"]),
            "inventory": list(ST["inventory"]),
            "viewer": bool(ST.get("viewer_up")),
            "galaxea": bool(ST.get("galaxea")),
            "mission": ST.get("mission", ""),
            "todos": ST.get("todos", []),
            "built": built_scenes()}


# Anything that puts the tour somewhere new. Serialized against each other so
# no two are ever in flight at once.
MOVERS = {"go_to", "face", "turn", "take_lift"}
MOVE = threading.Lock()


def pose_pump():
    """Stream where the robot actually is, between waypoints.

    The model's position is only redefined at waypoints, so the dashboard marker
    sat on the vertex a walk had left and jumped to the next one on arrival —
    which reads as the dashboard lagging, and makes it impossible to see that a
    walk is under way at all. RMF publishes the robot pose at about 1.5 Hz; this
    forwards it as its own small event so the marker moves along the corridor
    without redrawing the whole page.
    """
    last = None
    while True:
        time.sleep(0.25)
        try:
            if not ST.get("galaxea") or not ST.get("m2px"):
                continue
            st = (bridge("/state", timeout=3) or {}).get("state") or {}
            if st.get("x") is None:
                continue
            here_now = (round(st["x"], 2), round(st["y"], 2),
                        round(st.get("yaw") or 0.0, 2))
            if here_now == last:
                continue
            last = here_now
            # The marker needs a direction to be a triangle at all, and the
            # model's own yaw is unset after a teleport or a level change. The
            # robot always has one, so remember it as the fallback rather than
            # letting the marker degrade to a dot that says nothing.
            ST["yaw_live"] = here_now[2]
            px, py = ST["m2px"](st["x"], st["y"])
            ex, ey = ST["m2px"](st["x"] + math.cos(here_now[2]),
                                st["y"] + math.sin(here_now[2]))
            BUS.send({"type": "pose", "px": px, "py": py,
                      "dir": [ex - px, ey - py], "moving": MOVE.locked()})
        except Exception:                                      # noqa: BLE001
            pass                      # the bridge going quiet is already reported


def push_state():
    """Publish the truth. This server is the only writer of where the tour is;
    the splat viewer and the robot are followers, and both hear about a change
    the moment it happens rather than discovering it on their next poll.

    The viewer used to learn this by fetching /state every two seconds and
    needing to disagree twice before acting, so a divergence could stand for
    four seconds. Pushing costs nothing — the command channel is already open —
    and closes that to about as long as one HTTP hop.
    """
    st = state_dict()
    BUS.send({"type": "state", **st})
    VIEWER.send({"op": "truth", "scene": st.get("scene"),
                 "level": st.get("level"), "yaw": st.get("heading")})


def robot_watchdog():
    """Hold the robot to the truth as well.

    Nothing else writes the robot's position, so it cannot wander — but a nav
    that quietly failed to arrive leaves it somewhere the model does not think
    it is, and that reads exactly like a desync. This checks while the robot is
    still, and only corrects a gap too large to be the arrival tolerance.
    """
    was = None                       # last sampled position, to tell moving from stuck
    strikes = 0
    while True:
        time.sleep(5)
        try:
            if MOVE.locked() or not ST.get("galaxea") or ST.get("cur") is None:
                continue
            st = (bridge("/state") or {}).get("state") or {}
            if st.get("x") is None:
                continue
            here_now = (st["x"], st["y"])
            moving = was is not None and math.dist(here_now, was) > 0.05
            was = here_now
            v = ST["verts"][ST["cur"]]
            gap = math.dist(here_now, (v[1], v[2]))
            # /goto returns as soon as the drive is dispatched, so the flag above
            # is down long before the robot arrives. Correcting on that alone
            # would teleport it backwards mid-corridor. So: only when it has
            # stopped, and only when it is still short twice running — a robot
            # that is moving is on its way, and one that stopped once at a
            # doorway is not yet stranded.
            if moving or gap < 1.5:
                strikes = 0
                continue
            strikes += 1
            if strikes < 2:
                continue
            strikes = 0
            log(f"robot stopped {gap:.1f} m from {lab(ST['cur'])} — putting it back",
                "err")
            bridge("/reset", {"level": ST["level"], "x": v[1], "y": v[2],
                              "yaw": ST.get("yaw") or 0.0})
        except Exception as e:                                 # noqa: BLE001
            log(f"watchdog: {e}", "err")


# ---- tools ------------------------------------------------------------------
def tool(desc, params=()):
    def deco(fn):
        TOOLS[fn.__name__] = {"fn": fn, "desc": desc, "params": list(params)}
        return fn
    return deco


@tool("Walk to a waypoint (by name or id) — ONLY if the whole path is UNBLOCKED (no "
      "closed door in the way). Walks it corridor by corridor in the splat viewer and "
      "drives the robot along the same waypoints. If the path is blocked it moves "
      "NOTHING and says which door to open first.",
      [("vertex", "waypoint name or id — reachable without a closed door")])
def go_to(vertex):
    try:
        tgt = find_vertex(ST["verts"], vertex)
    except SystemExit as e:
        return {"ok": False, "error": str(e)}
    cur = ST["cur"]
    if tgt == cur:
        return {"ok": True, "at": lab(cur), "note": "already there"}
    path = dijkstra(ST["adj"], cur, tgt)
    if not path or len(path) < 2:
        return {"ok": False, "error": f"no path from {lab(cur)} to {lab(tgt)}"}
    # go_to ONLY accepts UNBLOCKED paths: if any door/lift on the route is closed the
    # target is invalid — it moves nothing and names the clear waypoint to go_to
    # instead, plus the interaction that clears the obstacle. So a mission decomposes
    # into go_to (clear waypoint) -> open_door/call_lift -> go_to ...
    reached, obstacle = route_stop(path)
    if obstacle:
        return {"ok": False, "error": blocked_message(tgt, path, reached, obstacle)}

    if not ST.get("viewer_up") and not ST.get("galaxea"):
        return {"ok": False, "error": "nothing to walk with — no splat viewer "
                                      f"connected ({ST['viewer_url']}) and no robot "
                                      "bridge. Neither would move."}

    # One edge at a time on BOTH sides, started together. Sending the robot the
    # whole polyline up front and walking the viewer corridor by corridor let the
    # two drift apart over a long route with nothing to pull them back; per edge,
    # any difference is bounded by one corridor and is corrected at every vertex.
    walked = []
    for u, v in zip(path, path[1:]):
        # /goto's first act on a leg is to turn to face it, at TURN_RATE — the
        # same turn the viewer now makes before it departs. Both start here, and
        # both are given the SAME arc and distance rather than each deriving its
        # own, so the two motions are equal by construction.
        motion = leg_motion(u, v)
        drive_robot([u, v])
        res = viewer_call("walk", to=lab(v), motion=motion)
        with ST["lock"]:
            ST["yaw"] = motion["yaw"]        # the heading both are now holding
        if not res.get("ok"):
            # The viewer failed partway. The robot is already driving the whole
            # line, so stop it where the walk actually got to rather than letting
            # it run on to a destination the state will not agree it reached.
            with ST["lock"]:
                ST["prev"], ST["cur"], ST["face"] = ST["prev"], u, None
            drive_robot([u])          # stop it where the walk actually reached
            push_state()
            return {"ok": False, "error": res.get("error"), "reached": walked,
                    "at": lab(u)}
        if res.get("no_viewer"):
            # No viewer to pace against, so the robot's own RMF state is the gate.
            wait_robot(ST["verts"][v][1], ST["verts"][v][2])
        with ST["lock"]:
            ST["prev"], ST["cur"], ST["face"] = u, v, None
        walked.append(lab(v))
        push_state()
    if not wait_robot(ST["verts"][tgt][1], ST["verts"][tgt][2]):
        log(f"robot has not reported reaching {lab(tgt)}", "err")
    return {"ok": True, "at": lab(tgt), "route": [lab(i) for i in path],
            "shown": not res.get("no_viewer")}


@tool("Turn in place to FACE an adjacent waypoint, without moving.",
      [("target", "adjacent waypoint name/id to face")])
def face(target):
    try:
        tgt = find_vertex(ST["verts"], target)
    except SystemExit as e:
        return {"ok": False, "error": str(e)}
    cur = ST["cur"]
    if tgt not in [v for v, _ in ST["adj"].get(cur, [])]:
        return {"ok": False,
                "error": f"{lab(tgt)} is not adjacent to {lab(cur)}. "
                         f"Neighbours: {', '.join(lab(v) for v, _ in ST['adj'][cur])}."}
    if not ST.get("viewer_up") and not ST.get("galaxea"):
        return {"ok": False, "error": "nothing to turn — no splat viewer connected "
                                      "and no robot bridge"}
    yaw = _az(cur, tgt)
    arc = (yaw - (ST.get("yaw") if ST.get("yaw") is not None else yaw)
           + math.pi) % (2 * math.pi) - math.pi
    turn_robot(yaw)
    res = viewer_call("face", to=lab(tgt), timeout=30,
                      motion={"arc": round(arc, 6), "yaw": round(yaw, 6),
                              "turn_ms": round(abs(arc) / TURN_RATE * 1000)})
    with ST["lock"]:
        ST["yaw"] = yaw
    if not res.get("ok"):
        return res
    with ST["lock"]:
        ST["face"] = tgt
    push_state()
    return {"ok": True, "at": lab(cur), "facing": lab(tgt), "yaw": round(yaw, 4)}


@tool("Turn in place to look around, to the next neighbour clockwise or anticlockwise.",
      [("direction", "'left' or 'right'")])
def turn(direction):
    cur = ST["cur"]
    ns = [v for v, _ in ST["adj"].get(cur, [])]
    if not ns:
        return {"ok": False, "error": f"{lab(cur)} has no neighbours to turn to"}
    ns.sort(key=lambda v: _az(cur, v))
    yaw = heading_now()
    if ST["face"] in ns:
        start = ns.index(ST["face"])
    elif yaw is not None:
        # Turning starts from where the robot is actually pointing, not from
        # whichever neighbour happens to sort first — otherwise the first turn
        # after a teleport swings somewhere unrelated to the arrow on screen.
        start = ns.index(min(ns, key=lambda v: toward(cur, v, yaw)))
    else:
        start = 0
    i = ((start + (1 if str(direction).lower().startswith("l") else -1)) % len(ns)
         if (ST["face"] in ns or yaw is not None) else 0)
    return face(lab(ns[i]))


@tool("Open the door you are facing.", [("to", "waypoint beyond the door (optional)")])
def open_door(to=""):
    return _door(to, "open")


@tool("Close the door you are facing.", [("to", "waypoint beyond the door (optional)")])
def close_door(to=""):
    return _door(to, "close")


def has_door(u, v):
    """Whether the edge u->v carries a door at all — hinged, or a lift's.

    Either end being a cabin makes it a lift edge: standing in the cabin, the door
    out is still the lift's. This is the same test crossable() applies before
    asking whether it is open, so the two cannot disagree about what a door is.
    """
    return bool(door_between(u, v) or ST["lift_of"].get(u) or ST["lift_of"].get(v))


def door_neighbors(cur):
    """Neighbours of cur whose edge carries a door."""
    return [v for v, _ in ST["adj"].get(cur, []) if has_door(cur, v)]


def door_target(cur):
    """Which door a bare `open_door` means. (vertex, error). Ported from
    _door_target: the faced one, else the only one, else the one you are heading
    toward, else ask rather than guess."""
    dn = door_neighbors(cur)
    if ST["face"] in dn:
        return ST["face"], None
    if len(dn) == 1:
        return dn[0], None
    if not dn:
        return None, f"no door adjacent to {lab(cur)}"
    return None, ("which door? turn to face one, or 'open door to <"
                  + " | ".join(ST["lift_of"].get(x) or lab(x) for x in dn) + ">'")


def _door(to, mode):
    # Which door an edge crosses is worked out from where the robot is, so
    # this has to be asked of a robot that has finished moving.
    wait_still()
    cur = ST["cur"]
    tgt = ST["face"]
    if str(to).strip():
        try:
            tgt = find_vertex(ST["verts"], to)
        except SystemExit as e:
            return {"ok": False, "error": str(e)}
    if tgt is None:
        tgt, err = door_target(cur)
        if err:
            return {"ok": False, "error": err}
    name = door_between(cur, tgt)
    lift_here = ST["lift_of"].get(tgt) or ST["lift_of"].get(cur)
    if not name and not lift_here:
        return {"ok": False, "error": f"no door between {lab(cur)} and {lab(tgt)}"}
    if lift_here and mode == "open":
        # The bridge refuses a shaft door whose cabin is elsewhere (409), which is
        # what stops `open_door` from summoning a lift by opening its door.
        st = lift_states().get(lift_here) or {}
        if st.get("floor") and st["floor"] != ST["level"]:
            return {"ok": False, "error": f"{lift_here} is at {st['floor']}, not "
                                          f"{ST['level']} — call_lift {ST['level']} first"}
    # The bridge names the door from the EDGE, not from a name we pass it: two lift
    # doors can be colinear, and only the edge tells them apart. So send the two
    # waypoints and let it decide, which is also what makes its answer worth having.
    res = bridge("/door", {"waypoints": [[ST["verts"][cur][1], ST["verts"][cur][2]],
                                         [ST["verts"][tgt][1], ST["verts"][tgt][2]]],
                           "mode": mode})
    if ST.get("galaxea") and not res.get("ok", True):
        return {"ok": False, "error": res.get("error") or f"the robot refused to {mode} it",
                "door": name}
    # Prefer the bridge's own naming; ours is a fallback for a viewer-only run.
    names = res.get("doors") or [name]
    lift = ST["lift_of"].get(tgt) or ST["lift_of"].get(cur)
    if lift and not wait_lift_door(lift, mode == "open"):
        return {"ok": False, "door": names[0],
                "error": f"{lift}'s door did not reach {mode}"}
    if not lift and not wait_door(names, mode == "open"):
        st = door_states()
        return {"ok": False, "door": names[0],
                "error": f"{names[0]} did not reach {mode} — it is "
                         f"{'moving' if st.get(names[0]) == DOOR_MOVING else st.get(names[0])}"}
    with ST["lock"]:
        for n in list(names) + ([lift] if lift else []):
            ST["open_doors"].add(n) if mode == "open" else ST["open_doors"].discard(n)
    push_state()
    return {"ok": True, "door": names[0], "doors": names, "mode": mode}


@tool("Which lift to take, chosen before facing it.", [("lift", "lift name, e.g. lift1")])
def select_lift(lift=""):
    wait_still()
    cur = ST["cur"]
    cabins = {v: ST["lift_of"][v] for v, _ in ST["adj"].get(cur, []) if v in ST["lift_of"]}
    if not cabins:
        return {"ok": False, "error": f"no lift at {lab(cur)} — go_to a lift lobby first"}
    here = sorted(set(cabins.values()))
    match = next((l for l in here if l.lower() == str(lift).strip().lower()), None)
    if match is None:
        return {"ok": False,
                "error": f"'{lift}' is not a lift at {lab(cur)}. Available: {', '.join(here)}."}
    with ST["lock"]:
        ST["selected_lift"] = match
        ST["sel_cabin"] = next(v for v, l in cabins.items() if l == match)
    return {"ok": True, "selected_lift": match, "cabin": lab(ST["sel_cabin"]),
            "next": f"face {lab(ST['sel_cabin'])}"}


@tool("Call the lift you are facing to a LEVEL so its door can open. The argument is a "
      "level (e.g. L1 or L11) — never a lift name.",
      [("level", "target level, e.g. L1 or L11")])
def call_lift(level=""):
    wait_still()
    lift = ST.get("selected_lift")
    if not lift:
        cabins = [ST["lift_of"][v] for v, _ in ST["adj"].get(ST["cur"], [])
                  if v in ST["lift_of"]]
        if not cabins:
            return {"ok": False, "error": f"no lift at {lab(ST['cur'])}"}
        lift = cabins[0]
    floor = str(level).strip() or ST["level"]
    # `ride` tells the bridge the robot is standing in the cabin, so the lift
    # physically carries it and the bridge re-levels itself on arrival. It must not
    # teleport the robot instead — that fights the cabin.
    riding = floor != ST["level"] and ST["lift_of"].get(ST["cur"]) == lift
    res = bridge("/call_lift", {"lift": lift, "floor": floor, "ride": riding})
    # Riding it is not the same as calling it: you have ridden only if you were
    # standing in the cabin when it moved. Then the graph underneath changes, and
    # every level-keyed thing has to be rebuilt before the next tool call reads it.
    rode = False
    if riding:
        rode = _ride_lift(lift, floor)
    elif not wait_lift(lift, floor):
        return {"ok": False, "lift": lift, "floor": floor,
                "error": f"{lift} has not reached {floor} — it is at "
                         f"{(lift_states().get(lift) or {}).get('floor')}"}
    return {"ok": True, "lift": lift, "floor": floor, "rode": rode,
            "at": lab(ST["cur"]), "level": ST["level"]}


@tool("Take a lift to another LEVEL. Installs the canonical lift-taking template as "
      "subtasks (select_lift -> face lift -> call_lift <this level> -> open_door -> "
      "go_to <cabin> (enter) -> call_lift <target level> -> open_door -> go_to <lobby> "
      "(exit)) and you then execute them IN ORDER. Use this for every level change "
      "instead of improvising.",
      [("to_level", "destination level, e.g. L1 or L11")])
def take_lift(to_level=""):
    to = str(to_level).strip().upper()
    levels = ST.get("levels") or []
    if levels and to not in levels:
        return {"ok": False, "error": f"'{to_level}' is not a level. "
                                      f"Levels: {', '.join(levels)}."}
    cur = ST["cur"]
    cabins = [v for v, _ in ST["adj"].get(cur, []) if v in ST["lift_of"]]
    if not cabins:
        return {"ok": False, "error": f"no lift at {lab(cur)} — go_to a lift lobby first"}
    cabin = ST["face"] if ST["face"] in cabins else cabins[0]
    here = ST.get("level", "")
    if to == here.upper():
        return {"ok": False, "error": f"already on {here}"}
    lift_name = ST["lift_of"].get(cabin)
    # Resolve the EXIT: the lobby vertex adjacent to this lift's cabin on the TARGET
    # level, by name where possible, so it still resolves after the ride switches the
    # graph out from under it.
    exit_ref = "lift_lobby"
    try:
        _, tverts, tadj, _ = load_nav(ST["nav"], to)
        tlift = lift_of(ST["nav"], to)
        tcabin = next((v for v, l in tlift.items() if l == lift_name), None)
        if tcabin is not None and tadj.get(tcabin):
            n0 = tadj[tcabin][0][0]
            exit_ref = tverts[n0][0] or f"v{n0}"
    except (OSError, ValueError, KeyError, StopIteration):
        pass
    # The fixed template — each step exactly ONE control-tool call. Select the lift
    # first, then face it; go_to (never forward) for entering and leaving the cabin.
    template = [f"select_lift {lift_name}", f"face {lab(cabin)}", f"call_lift {here}",
                "open_door", f"go_to {lab(cabin)}", f"call_lift {to}", "open_door",
                f"go_to {exit_ref}"]
    # Tag each step 'via take_lift' — the gate only lets the lift primitives run when
    # they belong to a take_lift-installed subtask, so a level change can never be
    # improvised.
    with ST["lock"]:
        ST["todos"] = [{"step": c, "status": "pending", "via": "take_lift"}
                       for c in template]
        ST["todos"][0]["status"] = "in_progress"
    push_state()
    _push_context()
    return {"ok": True, "template": template, "lift": lift_name, "next": template[0],
            "note": "execute these subtasks strictly in order (each verified)."}


def wait_still(timeout=30, quiet=0.6):
    """Block until the robot has stopped moving.

    An interaction is with something in front of you, and where "in front" is
    depends on having finished turning to face it. A drive or a spin runs in a
    thread on the bridge and the HTTP call that started it returned long ago, so
    a door opened mid-turn is opened from wherever the robot happened to be
    passing — sometimes the wrong door entirely, since which door an edge crosses
    is decided from the position.

    There is no "am I moving" on the bridge, so this watches its reported pose
    and waits for it to hold still. `quiet` is how long it must not change:
    long enough to outlast the ~1.5 Hz state publishing, short enough not to be
    felt between steps of a plan.
    """
    if not ST.get("galaxea"):
        return True
    end = time.time() + timeout
    last, since = None, time.time()
    while time.time() < end:
        st = robot_state()
        now = (round(st.get("x", 0), 3), round(st.get("y", 0), 3),
               round(st.get("yaw", 0), 3))
        if now != last:
            last, since = now, time.time()
        elif time.time() - since >= quiet:
            return True
        time.sleep(0.15)
    log("robot is still moving after 30s", "err")
    return False


def wait_lift(lift, floor, timeout=120):
    """Block until `lift` has arrived at `floor` with motion stopped.

    Ported from _wait_lift. A control tool blocks until the bridge's RMF state
    confirms the motion actually finished — otherwise call_lift returns while the
    cabin is still travelling and the next step opens a door onto an empty shaft.
    """
    if not ST.get("galaxea") or not lift:
        return True
    end = time.time() + timeout
    while time.time() < end:
        st = lift_states().get(lift) or {}
        if st.get("floor") == floor and int(st.get("motion", 0)) == 0:
            return True
        time.sleep(0.4)
    return False


DOOR_MOVING = 1    # rmf_door_msgs DoorMode.MODE_MOVING


def door_states():
    return (bridge("/door_state") or {}).get("doors") or {}


def wait_door(names, want_open, timeout=25):
    """Block until every named door has finished moving into the wanted state.

    A DoorRequest is a request: the leaf takes seconds to swing, and open_door
    used to return the moment it was posted. go_to would then walk a robot at
    2 m/s through a doorway still opening. Nothing caught it, because the verify
    read a `doors` field the bridge did not publish, so its check was skipped and
    it passed unconditionally.
    """
    if not ST.get("galaxea") or not names:
        return True
    want = DOOR_OPEN if want_open else 0
    end = time.time() + timeout
    while time.time() < end:
        st = door_states()
        if all(st.get(n, want) == want for n in names):
            return True
        time.sleep(0.25)
    return False


def wait_lift_door(lift, want_open, timeout=20):
    """Block until `lift`'s door reaches open (2) / closed (0). From _wait_lift_door."""
    if not ST.get("galaxea") or not lift:
        return True
    target = DOOR_OPEN if want_open else 0
    end = time.time() + timeout
    while time.time() < end:
        if (lift_states().get(lift) or {}).get("door") == target:
            return True
        time.sleep(0.3)
    return False


def _ride_lift(lift, floor):
    """Wait for the gazebo lift to reach `floor`, then switch the map there."""
    deadline = time.time() + 90
    arrived = False
    while ST.get("galaxea") and time.time() < deadline:
        time.sleep(1.0)
        st = lift_states().get(lift) or {}
        if st.get("floor") == floor and int(st.get("motion", 0)) == 0:
            arrived = True
            break
    if not arrived and ST.get("galaxea"):
        log(f"{lift} has not reported reaching {floor}", "err")
    elif not ST.get("galaxea"):
        time.sleep(3)          # no sim to ride -> switch after a nominal delay
    return switch_level(floor, lift)


def switch_level(to, lift=None, land_on=None):
    """Move the whole world model onto another level.

    Everything keyed to a level has to be rebuilt, not adjusted: the vertices are a
    different list with a different numbering, and so are the lanes, the doors and
    the lift cabins. Doors opened on the old level are forgotten because they are
    not these doors.

    Where it lands is either this lift's cabin — after a ride, that is where the
    robot physically is — or a named waypoint, when an operator has put it
    somewhere directly.
    """
    level, verts, adj, _ = load_nav(ST["nav"], to)
    lifts = lift_of(ST["nav"], level)
    if land_on is not None:
        try:
            cabin = find_vertex(verts, land_on)
        except SystemExit:
            return False
    else:
        cabin = next((v for v, l in lifts.items() if l == lift), None)
    if cabin is None:
        return False
    with ST["lock"]:
        ST.update({"level": level, "verts": verts, "adj": adj,
                   "doors": doors_of(ST["nav"], level), "lift_of": lifts,
                   "open_doors": set(), "cur": cabin, "prev": None, "face": None,
                   "yaw": None})
    # AFTER the vertices are swapped: the affine is fitted from the waypoints the
    # drawing and the nav graph both name, and it reads ST["verts"] for them. Run
    # before, it fits the new level's drawing against the old level's waypoints,
    # finds one name in common, gives up, and every vertex is then drawn in raw
    # metres on a canvas sized in pixels — the whole graph in one corner. The
    # dream's own comment says to do it here, and I had moved it.
    png, fw, fh, m2px = build_floorplan(ST.get("building", ""), level)
    with ST["lock"]:
        ST.update({"fp_png": png, "fp_w": fw, "fp_h": fh, "m2px": m2px})
    log(f"now on {level} at {lab(cabin)}")
    BUS.send({"type": "level", "level": level})    # the page reloads the graph on this
    push_state()
    _push_context()
    return True


@tool("Pick an item up at this waypoint.", [("item", "item name")])
def pick(item=""):
    wait_still()
    res = bridge("/pick", {"item": str(item), "vertex": lab(ST["cur"])})
    with ST["lock"]:
        ST["inventory"].append(str(item))
    push_state()
    return {"ok": True, "item": item, "inventory": list(ST["inventory"]), "bridge": res}


@tool("Put an item down at this waypoint.", [("item", "item name")])
def place(item=""):
    wait_still()
    if str(item) not in ST["inventory"]:
        return {"ok": False, "error": f"not carrying {item}"}
    res = bridge("/place", {"item": str(item), "vertex": lab(ST["cur"])})
    with ST["lock"]:
        ST["inventory"].remove(str(item))
    push_state()
    return {"ok": True, "item": item, "inventory": list(ST["inventory"]), "bridge": res}


def find_level_of(key):
    """The OTHER level on which waypoint `key` exists, or None. From _find_level_of."""
    for lvl in (ST.get("levels") or []):
        if lvl.upper() == str(ST.get("level", "")).upper():
            continue
        try:
            _, verts, _, _ = load_nav(ST["nav"], lvl)
            find_vertex(verts, key)
            return lvl
        except (SystemExit, OSError, ValueError, KeyError):
            continue
    return None


def nearest_lift_lobby(cur):
    """The vertex nearest cur that is adjacent to a lift cabin. From
    _nearest_lift_lobby — nearest by hop count, which is what the walk costs."""
    best, bestd = None, 1e9
    for i in ST["adj"]:
        if any(v in ST["lift_of"] for v, _ in ST["adj"].get(i, [])):
            p = dijkstra(ST["adj"], cur, i)
            if p and len(p) < bestd:
                best, bestd = i, len(p)
    return best


def decompose_doors(cur, tgt):
    """(steps, route) opening every closed door on the way, or None."""
    path = dijkstra(ST["adj"], cur, tgt)
    if not path or len(path) < 2:
        return None
    steps, walked = [], cur
    for u, v in zip(path, path[1:]):
        if crossable(u, v):
            continue
        cabin = ST["lift_of"].get(v) or ST["lift_of"].get(u)
        if walked != u:
            steps.append(f"go_to {lab(u)}")
            walked = u
        if cabin:
            # A lift is not a door with extra steps: it has to be called to this
            # floor before its door means anything, so it gets its own sequence.
            steps += [f"select_lift {cabin}", f"face {lab(v)}",
                      f"call_lift {ST['level']}", "open_door"]
        else:
            steps += [f"face {lab(v)}", "open_door"]
    steps.append(f"go_to {lab(tgt)}")
    return steps, [lab(i) for i in path]


@tool("Plan the FULL obstacle-aware route to a waypoint WITHOUT trial-and-error. "
      "Returns an ordered 'steps' list (go_to -> face -> open_door -> go_to ...) that "
      "already opens every closed door on the way, so you NEVER hit a BLOCKED go_to. "
      "A waypoint on ANOTHER level returns the steps to a lift lobby plus the "
      "take_lift to call — do those, then call plan_route again on arrival.",
      [("to", "destination waypoint name/id")])
def plan_route(to):
    cur = ST["cur"]
    try:                                       # same level -> full door-aware plan
        tgt = find_vertex(ST["verts"], to)
        if tgt == cur:
            return {"ok": True, "steps": [], "note": f"already at {lab(cur)}"}
        d = decompose_doors(cur, tgt)
        if not d:
            return {"ok": False, "error": f"no path to {to}"}
        steps, route = d
        return {"ok": True, "steps": steps, "route": route,
                "lifts": sorted({ST["lift_of"][i] for i in dijkstra(ST["adj"], cur, tgt)
                                 if i in ST["lift_of"]}),
                "note": "put 'steps' into write_todos and execute in order — no BLOCKED."}
    except SystemExit:
        pass
    lvl = find_level_of(str(to))               # different level -> lift first
    if not lvl:
        return {"ok": False, "error": f"'{to}' is not a known waypoint on any level."}
    lobby = nearest_lift_lobby(cur)
    pre = (decompose_doors(cur, lobby) or ([], []))[0] if lobby is not None else []
    return {"ok": True, "needs_level_change": lvl, "steps": pre,
            "then": f"take_lift {lvl}",
            "note": f"'{to}' is on {lvl}. write_todos the 'steps' (reach a lift lobby) "
                    f"and do them, THEN call take_lift {lvl} (it installs the lift "
                    f"template). After you arrive on {lvl}, call plan_route {to} again "
                    f"to plan the final leg."}


@tool("Where you are, what is around you, and what you are carrying.")
def where():
    seen = viewer_call("where", timeout=20) if ST.get("viewer_up") else {}
    return {"ok": True, **state_dict(), "viewer_reports": seen}


@tool("The waypoints between here and a destination.", [("to", "destination waypoint")])
def get_path(to):
    try:
        tgt = find_vertex(ST["verts"], to)
    except SystemExit as e:
        return {"ok": False, "error": str(e)}
    path = dijkstra(ST["adj"], ST["cur"], tgt)
    return ({"ok": True, "path": [lab(i) for i in path]} if path
            else {"ok": False, "error": f"no path to {to}"})


@tool("The whole nav graph for this level: waypoints, their positions, their lanes.")
def get_graph():
    return {"ok": True, "level": ST["level"],
            "vertices": [{"id": lab(i), "x": round(v[1], 3), "y": round(v[2], 3),
                          "lanes": [lab(j) for j, _ in ST["adj"].get(i, [])],
                          "lift": ST["lift_of"].get(i, "")}
                         for i, v in enumerate(ST["verts"])]}


@tool("Record what this run is trying to do.", [("text", "the mission")])
def write_mission(text=""):
    with ST["lock"]:
        ST["mission"] = str(text)
    push_state()
    return {"ok": True, "mission": ST["mission"]}


@tool("Record the steps this run will take.", [("todos", "list of step descriptions")])
def write_todos(todos=None):
    items = todos or []
    if isinstance(items, str):
        items = [s.strip() for s in items.split("\n") if s.strip()]
    bad = [x for x in items if not _subtask_tool(x)]
    if bad:
        return {"ok": False, "rejected": True,
                "error": "Every subtask must be exactly one control-tool call, starting "
                         "with the tool name. Not valid: " + "; ".join(bad),
                "allowed_tools": sorted(SUBTASK_TOOLS),
                "example": ["go_to lift_lobby_north", "face v0", "open_door",
                            "go_to apex_lab", "pick apple"]}
    with ST["lock"]:
        ST["todos"] = [{"step": x, "status": "pending"} for x in items]
        if ST["todos"]:
            ST["todos"][0]["status"] = "in_progress"
    push_state()
    _push_context()
    return {"ok": True, "todos": ST["todos"]}


# ---- natural language -------------------------------------------------------
def heading_now():
    """Which way the robot is pointing — the one heading everything should use.

    The model's own yaw when it has one, and the robot's last reported heading
    when it does not (after a teleport or a level change it has none). This is
    exactly what the marker on the floor plan draws, so anything deciding "which
    way is forward" agrees with the arrow by construction rather than by
    coincidence.
    """
    y = ST.get("yaw")
    return ST.get("yaw_live") if y is None else y


def toward(cur, v, yaw):
    """How far off `yaw` the corridor to v lies, in radians, 0..pi."""
    return abs((_az(cur, v) - yaw + math.pi) % (2 * math.pi) - math.pi)


def choose_forward():
    """The neighbour that continues the way you are already walking.

    Ported from the dream's choose_forward — facing wins if you are facing one,
    and doubling back is the last resort rather than the first, so at a junction
    the arrow key keeps going instead of turning round.

    With one addition: the heading itself is consulted before the way you came
    in. Neither of the ported cues survives a teleport — nothing is faced and
    there is no previous waypoint — so this fell through to the first neighbour
    in the list, which is how the arrow could point down one corridor while
    forward walked into another and reported a door across it.
    """
    cur, prev = ST["cur"], ST["prev"]
    ns = [v for v, _ in ST["adj"].get(cur, [])]
    if not ns:
        return cur
    if ST["face"] in ns:
        return ST["face"]
    yaw = heading_now()
    if yaw is not None:
        best = min(ns, key=lambda v: toward(cur, v, yaw))
        # Only when it is genuinely ahead. Pointing at a wall makes "forward"
        # ambiguous, and the way you came in is the better answer then.
        if toward(cur, best, yaw) < math.radians(75):
            return best
    if prev is None or prev not in [v for v, _ in ST["adj"].get(cur, [])] and prev != cur:
        came = None
    else:
        came = _az(prev, cur) if prev is not None else None
    if came is None:
        return ns[0]
    def off(v):
        d = abs((_az(cur, v) - came + math.pi) % (2 * math.pi) - math.pi)
        return d
    ahead = [v for v in ns if v != prev]
    return min(ahead or ns, key=off)


def handle(text):
    """The same phrasings the dream dashboard's text box took."""
    t = " ".join(str(text).split()).lower()
    if not t:
        return {"ok": False, "error": "say something"}
    if t in ("where", "where am i", "look"):
        return {**(where()), "_tool": "where"}
    m = re.fullmatch(r"(?:go|walk|go to|walk to)\s+(?:to\s+)?(.+)", t)
    if m:
        return {**(go_to(m.group(1))), "_tool": "go_to"}
    m = re.fullmatch(r"face\s+(.+)", t)
    if m:
        return {**(face(m.group(1))), "_tool": "face"}
    if t == "forward":
        return {**(go_to(lab(choose_forward()))), "_tool": "go_to"}
    m = re.fullmatch(r"turn\s+(left|right)", t)
    if m:
        return {**(turn(m.group(1))), "_tool": "turn"}
    m = re.fullmatch(r"open\s+door(?:\s+to\s+(.+))?", t)
    if m:
        return {**(open_door(m.group(1) or "")), "_tool": "open_door"}
    m = re.fullmatch(r"close\s+door(?:\s+to\s+(.+))?", t)
    if m:
        return {**(close_door(m.group(1) or "")), "_tool": "close_door"}
    m = re.fullmatch(r"call\s+lift\s*(.*)", t)
    if m:
        return {**(call_lift(m.group(1))), "_tool": "call_lift"}
    return {"ok": False, "error": f"did not understand {text!r}. "
                                  f"Try: go to <waypoint>, face <waypoint>, turn left, "
                                  f"open door, where."}


# ---- floorplan overlay: nav-metres -> floorplan-pixels affine ----------------
# The building.yaml drawing (the floorplan PNG) has vertices in PIXELS; the nav
# graph is in METRES. Both name the same waypoints, so fit an affine from the
# shared names and project every nav vertex onto the image. Ported from the dream
# dashboard — the same fit this repo measured at 0.04875 m/px on L11.
def _solve3(A, b):
    M = [A[i][:] + [b[i]] for i in range(3)]
    for i in range(3):
        p = max(range(i, 3), key=lambda r: abs(M[r][i]))
        M[i], M[p] = M[p], M[i]
        piv = M[i][i] or 1e-9
        for j in range(i, 4):
            M[i][j] /= piv
        for r in range(3):
            if r != i:
                f = M[r][i]
                for j in range(i, 4):
                    M[r][j] -= f * M[i][j]
    return [M[0][3], M[1][3], M[2][3]]


def _fit_affine(src, tgt):
    sxx = sxy = sx = syy = sy = n = tx = ty = t = 0.0
    for (ax, ay), m in zip(src, tgt):
        sxx += ax * ax; sxy += ax * ay; sx += ax
        syy += ay * ay; sy += ay; n += 1
        tx += ax * m; ty += ay * m; t += m
    return _solve3([[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, n]], [tx, ty, t])


def build_floorplan(building, level):
    """(png path, w, h, metres->pixels) for a level, or (None, 0, 0, None)."""
    if not building or not os.path.isfile(building):
        return None, 0, 0, None
    B = yaml.safe_load(open(building))
    BL = (B.get("levels") or {}).get(level)
    if not BL:
        return None, 0, 0, None
    png = os.path.join(os.path.dirname(building),
                       (BL.get("drawing") or {}).get("filename", ""))
    if not os.path.isfile(png):
        return None, 0, 0, None
    w = h = 0
    try:
        import struct
        with open(png, "rb") as f:
            f.read(16)
            w, h = struct.unpack(">II", f.read(8))      # PNG IHDR
    except (OSError, struct.error):
        pass
    # A drawing vertex is [px, py, z, name, params]; only the named ones can be
    # matched to the nav graph, and three of them pin an affine.
    bname = {v[3]: (v[0], v[1]) for v in BL.get("vertices", [])
             if len(v) > 3 and isinstance(v[3], str) and v[3]}
    nname = {n: (x, y) for (n, x, y) in ST["verts"] if n}
    shared = [k for k in bname if k in nname]
    if len(shared) < 3:
        print(f"[interactive] floorplan: only {len(shared)} shared name(s), "
              f"no projection", flush=True)
        return png, w, h, None
    src = [nname[k] for k in shared]
    ax = _fit_affine(src, [bname[k][0] for k in shared])
    ay = _fit_affine(src, [bname[k][1] for k in shared])
    print(f"[interactive] floorplan: {os.path.basename(png)} {w}x{h}, "
          f"fitted on {len(shared)} shared waypoints", flush=True)
    return png, w, h, (lambda x, y: (ax[0] * x + ax[1] * y + ax[2],
                                     ay[0] * x + ay[1] * y + ay[2]))


# ---- verification: a subtask completes when the world says it did ------------
# Ported from the dream harness, and kept for the same reason: an agent that marks
# its own work done will mark it done. Here the world is the viewer's reported
# position and the bridge's RMF state, so a subtask cannot complete unverified.

VERIFY = {}
SUBTASK_TOOLS = {"go_to", "turn", "face", "open_door", "close_door",
                 "select_lift", "call_lift", "pick", "place", "take_lift"}


def verifies(*names):
    def deco(fn):
        for n in names:
            VERIFY[n] = fn
        return fn
    return deco


def _arg(args, key):
    if isinstance(args, dict):
        return args.get(key)
    if isinstance(args, (list, tuple)):
        return args[0] if args else None
    return args


@verifies("go_to")
def _v_go_to(args, r):
    if not r.get("ok"):
        return False, str(r.get("error") or "did not move")
    try:
        tgt = find_vertex(ST["verts"], str(_arg(args, "vertex")))
    except SystemExit:
        return False, "no such waypoint"
    if ST["cur"] != tgt:
        return False, f"still at {lab(ST['cur'])}"
    if ST.get("galaxea"):
        st = robot_state()
        if st.get("x") is not None:
            d = math.hypot(st["x"] - ST["verts"][tgt][1], st["y"] - ST["verts"][tgt][2])
            if d > 0.8:
                return False, f"robot is {d:.1f} m from {lab(tgt)}"
    return True, f"at {lab(tgt)}"


@verifies("face", "turn")
def _v_face(args, r):
    if not r.get("ok"):
        return False, str(r.get("error") or "did not turn")
    return True, f"facing {r.get('facing')}"


@verifies("open_door", "close_door")
def _v_door(args, r):
    if not r.get("ok"):
        return False, str(r.get("error") or "no door")
    names, want = r.get("doors") or [r.get("door")], r.get("mode") == "open"
    if ST.get("galaxea"):
        # The bridge owns the real door; ask it rather than trusting our own
        # record. This read a `doors` field /state did not have, so `name in st`
        # was always false and the check was skipped — it passed whatever the
        # door was doing.
        st = door_states()
        target = DOOR_OPEN if want else 0
        wrong = [n for n in names if st.get(n, target) != target]
        if wrong:
            return False, f"{wrong[0]} is {st.get(wrong[0])}, not {r.get('mode')}"
    return True, f"{', '.join(n for n in names if n)} {r.get('mode')}"


@verifies("pick", "place")
def _v_item(args, r):
    if not r.get("ok"):
        return False, str(r.get("error") or "failed")
    item, held = str(_arg(args, "item")), r.get("inventory") or []
    ok = (item in held) if r.get("_pick", True) else (item not in held)
    return ok, f"inventory: {', '.join(held) or 'empty'}"


@verifies("select_lift", "call_lift")
def _v_lift(args, r):
    return bool(r.get("ok")), str(r.get("error") or r.get("lift") or "ok")


def _subtask_tool(text):
    """The tool a subtask names, or "" when it is prose rather than a call."""
    head = str(text).strip().split()
    return head[0] if head and head[0] in SUBTASK_TOOLS else ""


def _record_verify(name, args, result):
    """Run this tool's verify against the current subtask and advance the plan."""
    if name not in VERIFY or not isinstance(result, dict):
        return
    try:
        passed, note = VERIFY[name](args, result)
    except Exception as e:
        passed, note = False, f"verify raised {type(e).__name__}: {e}"
    with ST["lock"]:
        todos = ST.get("todos") or []
        cur = next((t for t in todos if t["status"] == "in_progress"), None) or \
              next((t for t in todos if t["status"] == "pending"), None)
        if cur is None or _subtask_tool(cur["step"]) != name:
            return
        cur["verify"] = {"passed": passed, "note": note}
        if passed:
            cur["status"] = "completed"
            nxt = next((t for t in todos if t["status"] == "pending"), None)
            if nxt:
                nxt["status"] = "in_progress"
        else:
            cur["status"] = "in_progress"
    BUS.send({"type": "log", "level": "ok" if passed else "err",
              "text": f"  ↳ verify {'✓' if passed else '✗'} {note}"})
    push_state()
    _push_context()


def _mission_gate(name):
    """Reject a tool call that is not the subtask now due.

    The harness owns the order, so an agent cannot work ahead into a world it has
    not walked to yet. Report tools are always allowed — looking is free.
    """
    if name not in SUBTASK_TOOLS:
        return None
    todos = ST.get("todos") or []
    due = next((t for t in todos if t["status"] in ("in_progress", "pending")), None)
    # Level changes MUST go through take_lift: the lift primitives may only run when
    # the current subtask is one take_lift installed. An improvised call_lift is
    # refused, so a lift ride cannot be hand-rolled a step at a time.
    if name in ("select_lift", "call_lift") and (due is None or due.get("via") != "take_lift"):
        return {"ok": False, "use_take_lift": True,
                "error": f"Do not call {name} directly — level changes MUST go through "
                         "take_lift. Call take_lift <target level> (from a lift lobby); "
                         "it installs the correct verified sequence (select_lift -> face "
                         "-> call_lift -> open_door -> go_to cabin -> call_lift -> "
                         "open_door -> go_to lobby), then execute those in order."}
    if due is None:
        return None
    want = _subtask_tool(due["step"])
    if want and want != name:
        return {"ok": False, "out_of_order": True,
                "error": f"the subtask due now is {due['step']!r} — call {want}, not {name}"}
    return None


def _post_tool(name, args, result):
    """Shared by every path into a tool: verify, then hand back the fresh log as
    state so the caller (agent or operator) reacts to what actually happened."""
    if isinstance(result, dict):
        _record_verify(name, args, result)
        if name != "write_todos":
            result = {**result, "recent_log": BUS.recent_log(12)}
    return result


# ---- the world model the agent reads each turn ------------------------------
def build_context():
    s = state_dict()
    icon = {"completed": "✓", "in_progress": "▶", "pending": "○"}
    L = ["## Mission", ST.get("mission") or "(none)", "", "## Subtasks"]
    todos = ST.get("todos") or []
    for t in todos:
        line = f"{icon.get(t['status'], '○')} {t['step']}"
        v = t.get("verify")
        if v:
            line += "\n    ↳ verify " + ("✓ " if v["passed"] else "✗ ") + (v.get("note") or "")
        L.append(line)
    if not todos:
        L.append("(none)")
    L += ["", "## Robot", f"- at {s['at']}  ·  level {s['level']}",
          f"- facing {s['face'] or '(nothing)'}",
          f"- neighbours: {', '.join(s['neighbours'])}",
          f"- doors open: {', '.join(s['open_doors']) or 'none'}",
          f"- splat world on screen: {s['scene']}"]
    if ST.get("galaxea"):
        st = robot_state()
        if st.get("x") is not None:
            L.append(f"- gazebo r1 at ({st['x']:.2f}, {st['y']:.2f}) on {st.get('level')}")
    here = (ST.get("items") or {}).get(s["at"]) or []
    if here:
        L += ["", "## Interactable here", "- " + ", ".join(here) + "  (use `pick <item>`)"]
    L += ["", "## Robot inventory", "- " + (", ".join(s["inventory"]) or "(empty)")]
    lifts = {v: ST["lift_of"][v] for v in ST["lift_of"]}
    if lifts:
        L += ["", "## Lifts",
              "(these are LIFTS; 'lift1' is a lift, its floor is a LEVEL like L1)"]
        L += [f"- {name} at cabin {lab(v)}" for v, name in sorted(lifts.items())]
    L += ["", "## Recent log"] + ["- " + x for x in BUS.recent_log(12)] or ["- (none)"]
    return "\n".join(L)


def _push_context():
    BUS.send({"type": "statemodel", "text": build_context()})


# ---- the mission agent ------------------------------------------------------
# Optional: it needs deepagents installed AND a VLM endpoint. Without either, every
# tool stays directly callable — the agent is a driver of this surface, not a part
# of it.
VLM_BASE_URL = os.environ.get("VLM_BASE_URL", "")
VLM_API_KEY = os.environ.get("VLM_API_KEY", "x")
VLM_MODEL = os.environ.get("VLM_MODEL", "Qwen/Qwen3-VL-8B-Instruct")

AGENT_PROMPT = (
    "You are an agent that walks a building to carry out missions. The building is "
    "real: you see it as gaussian-splat worlds photographed at each waypoint, and a "
    "Galaxea R1 robot walks the same route in a Gazebo simulation of it.\n"
    "You act ONLY through the CONTROL tools, one action each: go_to, turn, face, "
    "open_door, close_door, select_lift, call_lift, pick, place. There is NO 'forward' "
    "— ALL movement is a go_to. get_graph/where/get_path/plan_route report; "
    "write_mission/write_todos track.\n"
    "HOW NAVIGATION WORKS: go_to walks ONLY fully-UNBLOCKED paths. Do NOT probe for "
    "obstacles by trying a go_to and waiting for BLOCKED — that wastes a turn. Call "
    "plan_route <dest> FIRST: it returns the obstacle-aware 'steps' list (go_to -> face "
    "-> open_door -> go_to ...) that already opens every closed door on the route, and "
    "you feed that straight into write_todos.\n"
    "WORKFLOW:\n"
    "1. write_mission with the goal. Then plan_route <destination>, and write_todos with "
    "the steps it returns (adding pick/place where the mission needs them). Every subtask "
    "MUST be exactly one control-tool call and start with the tool name.\n"
    "2. Execute STRICTLY IN ORDER, one tool call at a time. You do NOT set statuses — the "
    "HARNESS owns them: the moment a call's verify passes it auto-marks that subtask "
    "completed and moves the next to in_progress. Calling any tool other than the one due "
    "is REJECTED as out_of_order.\n"
    "3. VERIFY IS AUTOMATIC: after each control call the harness checks the world — where "
    "the viewer says it stands, and where the robot's RMF state says it is. On ✗ the "
    "subtask stays in_progress; re-check with 'where' and retry that same call.\n"
    "Every tool result carries 'recent_log' — read it, and call write_todos to revise the "
    "plan when it reveals something new. Do not end while any subtask is unfinished. "
    "Keep replies short."
)

_AGENT = {"graph": None, "err": "not built"}
_RUN = threading.Event()      # set = running, cleared = paused
_RUN.set()
_CANCEL = threading.Event()   # set = cancelled; tool calls become no-ops


def _gate(fn):
    """Wrap a tool so it blocks while paused, no-ops once cancelled, refuses to run
    out of order, and verifies afterwards. functools.wraps keeps the signature, which
    is what langchain infers the argument schema from."""
    @functools.wraps(fn)
    def w(*a, **k):
        _RUN.wait()
        if _CANCEL.is_set():
            return {"ok": False, "cancelled": True,
                    "error": "mission cancelled by the operator — stop, do nothing further"}
        blocked = _mission_gate(fn.__name__)
        if blocked is not None:
            return blocked
        return _post_tool(fn.__name__, k or list(a), fn(*a, **k))
    return w


def build_agent():
    if not VLM_BASE_URL:
        _AGENT["err"] = "no VLM_BASE_URL configured"
        return None
    try:
        from deepagents import create_deep_agent
        from langchain_core.tools import StructuredTool
        from langchain_openai import ChatOpenAI
        from langgraph.checkpoint.memory import MemorySaver
    except Exception as e:
        _AGENT["err"] = f"deepagents/langchain not installed ({e})"
        return None
    tools = [StructuredTool.from_function(_gate(t["fn"]), name=n, description=t["desc"])
             for n, t in TOOLS.items()]
    llm = ChatOpenAI(base_url=VLM_BASE_URL, api_key=VLM_API_KEY, model=VLM_MODEL,
                     streaming=True, max_retries=1, timeout=120)
    _AGENT["graph"] = create_deep_agent(model=llm, tools=tools,
                                        system_prompt=AGENT_PROMPT,
                                        checkpointer=MemorySaver())
    _AGENT["err"] = ""
    return _AGENT["graph"]


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>interactive — walk the building</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font-family:'Courier New',monospace;height:100vh;display:flex;flex-direction:column}
header{background:#161b22;border-bottom:1px solid #30363d;padding:9px 18px;display:flex;align-items:center;gap:12px;flex-shrink:0}
header h1{font-size:15px;font-weight:600;color:#58a6ff}
#dot{width:10px;height:10px;border-radius:50%;background:#3fb950;animation:pulse 2s infinite}
#dot.off{background:#f85149;animation:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
#stat{font-size:12px;color:#8b949e}
#pos{font-size:12px;color:#e3b341;margin-left:auto}
/* A move in flight is worth seeing: it is the window in which the position on
   screen is a corridor rather than a waypoint. */
#pos.moving{color:#7aa2f7}
#pos.moving::after{content:' · walking';opacity:.7}
main{display:flex;flex:1;overflow:hidden;gap:1px;background:#30363d}
.panel{background:#0d1117;display:flex;flex-direction:column;overflow:hidden}
#stack{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px;background:#30363d}
/* The map takes the column, so it gets the whole width the window can give it —
   the camera panel that used to sit under it has nothing to show here. */
#mid{flex:1;min-height:0}
#right{width:50%;flex-shrink:0}
#drag{width:5px;flex-shrink:0;cursor:col-resize;background:#30363d}
#drag:hover,#drag.on{background:#58a6ff}
.ph{background:#161b22;padding:7px 13px;font-size:12px;color:#8b949e;border-bottom:1px solid #30363d;flex-shrink:0}
.ph b{color:#58a6ff}
.pb{flex:1;overflow:auto;padding:10px;display:flex;flex-direction:column;gap:8px}
.pb::-webkit-scrollbar{width:6px}.pb::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}
#pad{display:flex;gap:6px;flex-wrap:wrap;align-items:stretch;justify-content:center}
#pad button{background:#21262d;border:1px solid #30363d;color:#e6edf3;font-size:18px;padding:8px 14px;border-radius:5px;cursor:pointer}
#pad button:hover{background:#30363d}
#pad .wide{font-size:13px}
.bar{display:flex;gap:6px}
input{flex:1;padding:9px;font-size:13px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:5px;font-family:inherit}
input:focus{outline:0;border-color:#1f6feb}
.go{padding:9px 14px;background:#1f6feb;color:#fff;border:0;border-radius:5px;cursor:pointer;font-size:13px}
#cancelbtn{padding:9px 12px;background:#30363d;color:#ff7b72;border:1px solid #f85149;border-radius:5px;cursor:pointer;font-size:13px;flex-shrink:0}
#cancelbtn:hover{background:#5a1d1d}
.go.running{background:#9e6a03}   /* mission running -> button shows PAUSE (amber) */
.go.paused{background:#238636}    /* paused -> button shows RESUME (green) */
.hint{color:#6e7681;font-size:11px}
#mapwrap{padding:4px;overflow:hidden;background:#0a0d12;flex:1;min-height:0;display:flex}
#map{display:block;width:100%;height:100%;image-rendering:auto;border:1px solid #30363d;border-radius:4px}
#mission{font-size:13px;color:#cae8ff;min-height:20px;white-space:pre-wrap;background:#051d40;border:1px solid #1f6feb;border-radius:4px;padding:8px}
#mission.empty{color:#6e7681;background:#0d1117;border-color:#30363d}
#tools{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px;justify-content:center}
.tool{font-size:10px;background:#161b22;border:1px solid #30363d;color:#79c0ff;padding:2px 7px;border-radius:10px;cursor:pointer}
.tool:hover{background:#1f6feb;color:#fff;border-color:#1f6feb}
.tool.sel{background:#1f6feb;color:#fff;border-color:#58a6ff}
#todos{margin:3px 0}
.todo{font-size:11px;color:#8b949e;padding:1px 0}
#right #agentwrap{flex:1;min-height:60px;overflow:auto;padding:8px}   /* agent panel in right column, above log */
#agentwrap::-webkit-scrollbar{width:6px}#agentwrap::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}
#statemodel{margin:0;font:11px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word;color:#8b949e}
#statemodel .h{color:#58a6ff;font-weight:600}
#statemodel .done{color:#56d364}
#statemodel .prog{color:#e3b341}
#statemodel .pend{color:#6e7681}
#log{flex:none;height:150px;min-height:44px;overflow:auto;font-size:11px;line-height:1.6;display:flex;flex-direction:column;gap:2px}
#log::-webkit-scrollbar{width:6px}#log::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}
#logdrag,#middrag{height:5px;flex-shrink:0;cursor:row-resize;background:#30363d}
#logdrag:hover,#logdrag.on,#middrag:hover,#middrag.on{background:#58a6ff}
.ev{padding:4px 8px;border-left:3px solid #30363d;border-radius:0 4px 4px 0;word-break:break-word}
.ev.cmd{border-color:#1f6feb;color:#79c0ff;background:#0b1a2e}
.ev.ok{border-color:#238636;color:#56d364}
.ev.err{border-color:#f85149;color:#ff7b72;background:#1a0004}
.ev.mission{border-color:#d29922;color:#e3b341;background:#161004}
.ev .t{color:#484f58;margin-right:5px}
.leg{display:flex;gap:12px;font-size:10px;color:#8b949e;flex-wrap:wrap}
.leg i{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:3px;vertical-align:middle}
header h1,#stat,#pos,#vlink{flex-shrink:0}
#vlink{font-size:12px;color:#8b949e;text-decoration:none;border:1px solid #30363d;
 border-radius:4px;padding:3px 8px}
#vlink:hover{border-color:#58a6ff;color:#58a6ff}
#vlink.on{color:#56d364;border-color:#238636}
header #mbox{flex:1;min-width:140px}
#tip{position:fixed;pointer-events:none;display:none;z-index:60;background:#161b22;
 border:1px solid #58a6ff;color:#e6edf3;font-size:11px;padding:3px 8px;border-radius:4px;white-space:nowrap}
#mapwrap{position:relative}
</style></head><body>
<div id=tip></div>
<header><div id=dot></div><h1>interactive</h1>
<input id=mbox placeholder="mission for the agent — describe the goal, then Run">
<button class=go id=runbtn onclick="onRunBtn()">run mission</button>
<button id=cancelbtn onclick="cancelMission()" title="cancel the running mission and clear it">✕ clear mission</button>
<span id=stat>connecting…</span><a id=vlink target=_blank rel=noopener></a>
<span id=pos></span></header>
<main>
 <div id=stack>
  <div class="panel" id=mid>
   <div class=ph><b>floorplan</b> + nav graph + position
    <span class=leg style="float:right"><span><i style="background:#3fb950"></i>you</span>
    <span><i style="background:#7aa2f7"></i>waypoint</span>
    <span><i style="background:#e0a030"></i>door</span>
    <span><i style="background:#d24dcf"></i>lift</span>
    <span><i style="background:#0d1117;border:1px solid #484f58"></i>no world yet</span></span></div>
   <div id=mapwrap><canvas id=map></canvas></div>
   <div class=ph style="border-top:1px solid #30363d"><b>controls</b></div>
   <div class=pb style="flex:none;padding-bottom:0;align-items:stretch">
    <div id=pad>
     <button onclick="cmd('turn left')" title="←">↰</button>
     <button onclick="cmd('forward')" title="↑">↑</button>
     <button onclick="cmd('turn right')" title="→">↱</button>
    </div>
    <div id=tools></div>
    <div class=bar id=toolarg style="display:none">
     <input id=argbox><button class=go onclick="runTool()">go</button></div>
    <div class=hint id=toolhint>click a tool — ones that need input reveal a field</div>
    <div class=bar style="margin-top:6px">
     <input id=tpwhere placeholder="reset at — waypoint, or click one on the map">
     <input id=tplevel placeholder="level" style="flex:0 0 70px">
     <button class=go id=tpgo style="background:#6e40c9">reset</button></div>
    <div class=hint id=tphint>click a waypoint on the map to fill this. Puts the
     robot there, shuts every door and lift, and starts again — an operator move,
     not something the agent can do</div>
   </div>
  </div>
 </div>
 <div id=drag title="drag to resize"></div>
 <div class="panel" id=right>
  <div class=ph><b>agent</b> — mission &amp; subtasks (RMF-tracked)</div>
  <div class=pb id=agentwrap><pre id=statemodel>no mission — type a goal above and hit “run mission”</pre></div>
  <div id=logdrag title="drag to resize log"></div>
  <div class=ph style="border-top:1px solid #30363d"><b>log</b></div>
  <div class=pb id=log></div>
 </div>
</main>
<script>
// Resizable right column: drag the splitter to set #right width.
(function(){
  const d=document.getElementById('drag'), r=document.getElementById('right');
  let x0=0,w0=0,on=false;
  d.addEventListener('mousedown',e=>{on=true;x0=e.clientX;w0=r.offsetWidth;d.classList.add('on');document.body.style.userSelect='none';e.preventDefault();});
  window.addEventListener('mousemove',e=>{if(!on)return;const w=Math.max(220,Math.min(window.innerWidth-260,w0-(e.clientX-x0)));r.style.width=w+'px';});
  window.addEventListener('mouseup',()=>{if(!on)return;on=false;d.classList.remove('on');document.body.style.userSelect='';});
})();
// Resizable log: drag the horizontal splitter to set #log height (agent panel above flexes).
(function(){
  const d=document.getElementById('logdrag'), log=document.getElementById('log'), col=document.getElementById('right');
  let on=false;
  d.addEventListener('mousedown',e=>{on=true;d.classList.add('on');document.body.style.userSelect='none';e.preventDefault();});
  window.addEventListener('mousemove',e=>{if(!on)return;const r=col.getBoundingClientRect();
    const h=Math.max(44,Math.min(r.height-120, r.bottom-e.clientY));log.style.height=h+'px';});
  window.addEventListener('mouseup',()=>{if(!on)return;on=false;d.classList.remove('on');document.body.style.userSelect='';});
})();
const $=id=>document.getElementById(id);
let G=null, FP=new Image(), fpReady=false, last=null;
function ts(){return new Date().toTimeString().slice(0,8)}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function logEv(cls,text){const e=document.createElement('div');e.className='ev '+cls;
 e.innerHTML='<span class=t>'+ts()+'</span>'+esc(text);$('log').appendChild(e);
 $('log').scrollTop=$('log').scrollHeight}
function setMission(m){const el=$('mission');if(!el)return;
 if(m&&m.trim()){el.textContent=m;el.classList.remove('empty')}
 else{el.textContent='no mission set';el.classList.add('empty')}}

function fitCanvas(){const c=$('map');if(!G)return;
 // Fill the pane and letterbox inside it, rather than sizing the canvas to the
 // floorplan's aspect: the map owns the column now, and a 1744x608 plan sized by
 // width alone would leave most of the height empty.
 const r=$('mapwrap').getBoundingClientRect();
 c.width=Math.max(50,r.width-8);c.height=Math.max(30,r.height-8)}

function drawMap(s){const c=$('map'),g=c.getContext('2d');if(!G){return}
 const W=c.width,H=c.height;g.clearRect(0,0,W,H);
 // one scale for both axes, centred — the plan must not be stretched to the pane
 const k=Math.min(W/(G.w||1),H/(G.h||1));
 const ox=(W-(G.w||1)*k)/2, oy=(H-(G.h||1)*k)/2, sx=k, sy=k;
 const P=(px,py)=>[px*k+ox,py*k+oy];
 if(fpReady){g.globalAlpha=.55;g.drawImage(FP,ox,oy,(G.w||1)*k,(G.h||1)*k);g.globalAlpha=1}
 else{g.fillStyle='#11151c';g.fillRect(0,0,W,H)}
 // edges
 const V={};for(const v of G.verts)V[v.id]=v;
 for(const e of G.edges){const a=V[e.u],b=V[e.v];if(!a||!b)continue;
  const[ax,ay]=P(a.px,a.py),[bx,by]=P(b.px,b.py);
  g.strokeStyle=e.door?'#8a6d1f':'#2f4568';g.lineWidth=e.door?2.5:1.5;
  g.beginPath();g.moveTo(ax,ay);g.lineTo(bx,by);g.stroke()}
 g.lineWidth=1;
 // vertices
 for(const v of G.verts){const[x,y]=P(v.px,v.py);
  g.fillStyle=v.lift?'#d24dcf':(v.door?'#e0a030':'#7aa2f7');
  g.beginPath();g.arc(x,y,4,0,7);g.fill();
  // hollow ring = no splat world generated for this waypoint yet
  if(!v.built){g.strokeStyle='#0d1117';g.lineWidth=2;g.stroke();
   g.strokeStyle='#484f58';g.lineWidth=1;g.beginPath();g.arc(x,y,6,0,7);g.stroke()}
  if(v.name){g.fillStyle='#7d8590';g.font='9px sans-serif';g.textAlign='center';
   g.fillText(v.name,x,y-7)}}
 // robot + facing arrow (dir is a unit vector already projected into floorplan pixels)
 if(s&&s.px!=null){const[x,y]=P(s.px,s.py);
  const dir=s.dir||headingDir(s);
  // A triangle, not a dot: the robot has a heading and the marker should carry
  // it. A dot with a separate arrow said the same thing twice and still left
  // the position looking directionless at a glance, which is the glance the
  // minimap exists for.
  if(dir){const dx=dir[0]*sx,dy=dir[1]*sy,dl=Math.hypot(dx,dy)||1;
   const a=Math.atan2(dy/dl,dx/dl);
   const P2=(r,off)=>[x+r*Math.cos(a+off),y+r*Math.sin(a+off)];
   const tip=P2(13,0),l=P2(9,2.55),r=P2(9,-2.55);      // long nose, swept back
   g.fillStyle='#3fb950';g.beginPath();
   g.moveTo(tip[0],tip[1]);g.lineTo(l[0],l[1]);
   g.lineTo(x,y);g.lineTo(r[0],r[1]);                  // notched tail, so the
   g.closePath();g.fill();                             // point is unmistakable
   g.strokeStyle='#0d1117';g.lineWidth=1.5;g.stroke();g.lineWidth=1}
  else{g.fillStyle='#3fb950';g.beginPath();g.arc(x,y,7,0,7);g.fill();
   g.strokeStyle='#0d1117';g.lineWidth=2;g.stroke();g.lineWidth=1}}
}
function headingDir(s){if(!G||!s||s.facing==null)return null;
 // point the arrow at the currently-faced neighbour (pixel space)
 const V={};for(const v of G.verts)V[v.id]=v;const me=V[s.cur];
 // facing is a label; find neighbour vertex by matching its label id
 for(const n of (s.neighbors||[])){if(n.facing){const t=V[n.id];if(t&&me){
   const dx=t.px-me.px,dy=t.py-me.py,d=Math.hypot(dx,dy)||1;return[dx/d,dy/d]}}}
 return null}

async function cmd(text){logEv('cmd','> '+text);
 try{const r=await fetch('/command',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({text})});const j=await r.json();
  logEv(j.ok?'ok':'err',(j.ok?'':'! ')+j.message)}
 catch(e){logEv('err','! '+e)}}
const P=(p,b)=>fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
// run/pause/resume button. While a mission runs, the button becomes PAUSE (halts the agent's
// tool calls + harness/state updates); paused it becomes RESUME.
let agentRunning=false, agentPaused=false;
function updateRunBtn(){const b=$('runbtn');if(!b)return;
 b.textContent=!agentRunning?'run mission':(agentPaused?'resume':'pause');
 b.classList.toggle('running',agentRunning&&!agentPaused);
 b.classList.toggle('paused',agentRunning&&agentPaused)}
function onRunBtn(){
 if(!agentRunning)return runAgent();
 if(!agentPaused){agentPaused=true;updateRunBtn();P('/pause',{}).catch(()=>{})}
 else{agentPaused=false;updateRunBtn();P('/resume',{}).catch(()=>{})}}
async function runAgent(){const t=$('mbox').value.trim();if(!t)return;$('mbox').value='';
 agentRunning=true;agentPaused=false;updateRunBtn();
 try{const r=await P('/agent',{text:t});const j=await r.json();
  if(!j.ok)logEv('err','agent: '+(j.error||'unavailable'))}catch(e){logEv('err','! '+e)}
 finally{agentRunning=false;agentPaused=false;updateRunBtn()}}
function cancelMission(){agentRunning=false;agentPaused=false;updateRunBtn();  // stop the agent + clear
 P('/cancel',{}).catch(()=>{})}   // server wipes mission+subtasks and broadcasts the cleared state
$('mbox').addEventListener('keydown',e=>{if(e.key==='Enter')onRunBtn()});
// Tool buttons. HIDE: not shown. NOINPUT: run on click even though they take an optional
// arg (open_door figures out the door from the current vertex + facing). Everything else
// with a param reveals an input field (placeholder = the param description) only when needed.
const HIDE=['turn','close_door','write_mission','write_todos','forward','get_path'], NOINPUT=['open_door'];
let TOOLD={}, selTool=null;
function loadTools(){fetch('/tools').then(r=>r.json()).then(ts=>{TOOLD={};
 $('tools').innerHTML=ts.filter(t=>!HIDE.includes(t.name)).map(t=>{TOOLD[t.name]=t;
  return `<span class=tool title="${esc(t.desc)}" onclick="selectTool('${t.name}')">${t.name}</span>`}).join('')})}
function hideArg(){$('toolarg').style.display='none';selTool=null;
 for(const el of document.querySelectorAll('.tool'))el.classList.remove('sel')}
function selectTool(name){const t=TOOLD[name];if(!t)return;
 for(const el of document.querySelectorAll('.tool'))el.classList.toggle('sel',el.textContent===name);
 if(t.params&&t.params.length&&!NOINPUT.includes(name)){selTool=name;const p=t.params[0];
  $('toolarg').style.display='flex';$('argbox').placeholder=p.desc||p.name;
  $('argbox').value=(name==='call_lift'?curLevel:'');$('argbox').focus();$('argbox').select();
  $('toolhint').textContent=name+' — '+(p.desc||p.name)}
 else{hideArg();$('toolhint').textContent='running '+name+'…';
  P('/invoke',{text:name}).catch(e=>logEv('err','! '+e))}}
async function runTool(){if(!selTool)return;const nm=selTool,v=$('argbox').value.trim();
 $('argbox').value='';hideArg();
 try{await P('/invoke',{text:(nm+' '+v).trim()})}catch(e){logEv('err','! '+e)}}
$('argbox').addEventListener('keydown',e=>{if(e.key==='Enter')runTool()});
function setTodos(td){const el=$('todos');if(!el)return;el.innerHTML=(td&&td.length)?td.map(t=>`<div class=todo>○ ${esc(typeof t==='string'?t:(t.content||''))}</div>`).join(''):''}
function setStateModel(text){const el=$('statemodel');if(!el)return;
 el.innerHTML=(text||'').split('\n').map(l=>{const e=esc(l);
  if(l.startsWith('## '))return '<span class=h>'+e+'</span>';
  if(l[0]==='✓')return '<span class=done>'+e+'</span>';   // ✓ completed
  if(l[0]==='▶')return '<span class=prog>'+e+'</span>';   // ▶ in_progress
  if(l[0]==='○')return '<span class=pend>'+e+'</span>';   // ○ pending
  return e;}).join('\n')}
// Teleport is the operator's, not the agent's: it is not in /tools and there is
// no path to it through the harness, so a mission cannot skip a corridor by
// wishing itself past it. Here it is a button because setting up a test from a
// particular waypoint should not mean walking there first.
async function resetAt(where,level){if(!where)return;
 $("tpgo").disabled=true;
 try{const j=await(await P('/reset',{waypoint:where,level:level||''})).json();
  // No reload here. A teleport that changes level makes the server broadcast
  // {type:'level'}, which reloads the graph and the floorplan; one that does not
  // needs no reload at all, because the state message moves the marker. Calling
  // it here refetched a 1.8 MB floorplan and redrew the map to show the same map.
  if(j.ok){logEv('ok','reset at '+j.message);$("tpwhere").value='';}
  else logEv('err','! '+j.error);}
 catch(e){logEv('err','! '+e);}finally{$("tpgo").disabled=false;}}
$("tpgo").onclick=()=>resetAt($("tpwhere").value.trim(),$("tplevel").value.trim());
$("tpwhere").addEventListener('keydown',e=>{if(e.key==='Enter')$("tpgo").click()});
$("tplevel").addEventListener('keydown',e=>{if(e.key==='Enter')$("tpgo").click()});

const KEYS={ArrowUp:'forward',ArrowLeft:'turn left',ArrowRight:'turn right'};
document.addEventListener('keydown',e=>{const a=document.activeElement;
 if(a&&a.tagName==='INPUT')return;if(KEYS[e.key]){e.preventDefault();cmd(KEYS[e.key])}});

let curLevel='';
function onState(s){
 // Keep the last known direction if this update carries none. A state event
 // without a heading says nothing about where the robot is pointing, and
 // dropping it turned the marker back into a dot until the next pose arrived.
 if(s.dir==null&&last&&last.dir)s.dir=last.dir;
 last=s;if(s.level)curLevel=s.level;
 if(!$('tplevel').value)$('tplevel').value=curLevel;   // never blank on arrival
 $('pos').textContent='@ '+s.cur_label+(s.level?'  ·  '+s.level:'')
  +(s.heading!=null?'  ·  hdg '+s.heading+'°':'')
  +(s.door_open?'  ·  door→'+s.door_open+' open':'');
 setMission(s.mission);viewerLink(s);drawMap(s)}

// The rollout is the splat viewer, in its own window rather than a pane here: it
// is a live WebGL scene, not a stream to embed. The link carries ?agent=, which is
// what hands its camera to this server.
// The model redefines position only at waypoints, so between them the marker
// used to sit on the vertex the walk had left. These carry the robot's actual
// pose, and are drawn straight onto the map without touching anything else on
// the page — a walk is then visibly under way instead of looking like a stall.
function onPose(d){if(!last)return;
 last.px=d.px;last.py=d.py;last.dir=d.dir;
 $('pos').classList.toggle('moving',!!d.moving);
 drawMap(last)}

function viewerLink(s){const a=$('vlink');if(!a||!s.scene)return;
 a.href=`${VIEWER}/?url=files/${PROJECT}/splats/${s.scene}/world.ply`
       +`&agent=${encodeURIComponent(location.origin)}`;
 a.textContent=(s.viewer?'viewer connected':'open the splat viewer')+' ↗';
 a.className=s.viewer?'on':''}
function connect(){const es=new EventSource('/events');
 es.onopen=()=>{$('dot').classList.remove('off');$('stat').textContent='live';
  fetch('/statemodel').then(r=>r.json()).then(d=>setStateModel(d.text)).catch(()=>{})};
 es.onmessage=ev=>{let d;try{d=JSON.parse(ev.data)}catch{return}
  if(d.type==='state')onState(d);
  else if(d.type==='mission')setMission(d.text);
  else if(d.type==='todos')setTodos(d.todos);
  else if(d.type==='statemodel')setStateModel(d.text);   // world model: mission+subtasks+RMF state
  else if(d.type==='pose')onPose(d);         // live robot position between waypoints
  else if(d.type==='level')loadGraph();      // lift ride -> reload map/graph for new level
  else if(d.type==='log')logEv(d.level||'ok',d.text)};
 es.onerror=()=>{$('dot').classList.add('off');$('stat').textContent='reconnecting…';
  es.close();setTimeout(connect,3000)}}

function loadGraph(){fetch('/graph').then(r=>r.json()).then(g=>{G=g;
 if(g.has_floorplan){fpReady=false;FP.onload=()=>{fpReady=true;fitCanvas();if(last)drawMap(last)};
  FP.src='/floorplan.png?t='+Date.now()}
 fitCanvas();fetch('/state').then(r=>r.json()).then(onState)})}
loadGraph();
window.addEventListener('resize',()=>{fitCanvas();if(last)drawMap(last)});
// hover a map vertex -> tooltip; click a vertex while a tool input is active -> fill it in
(function(){const map=$('map'),tip=$('tip');
 function vertAt(e){if(!G)return null;const r=map.getBoundingClientRect();
  const mx=(e.clientX-r.left)*map.width/r.width,my=(e.clientY-r.top)*map.height/r.height;
  const k=Math.min(map.width/(G.w||1),map.height/(G.h||1));
  const ox=(map.width-(G.w||1)*k)/2, oy=(map.height-(G.h||1)*k)/2;
  let best=null,bd=14;for(const v of G.verts){const d=Math.hypot(v.px*k+ox-mx,v.py*k+oy-my);if(d<bd){bd=d;best=v}}
  return best}
 map.addEventListener('mousemove',e=>{const best=vertAt(e);
  if(best){tip.textContent=best.name||('vertex '+best.id);
   tip.style.left=(e.clientX+13)+'px';tip.style.top=(e.clientY+13)+'px';tip.style.display='block';
   map.style.cursor=selTool?'crosshair':'pointer'}
  else{tip.style.display='none';map.style.cursor='default'}});
 map.addEventListener('mouseleave',()=>{tip.style.display='none'});

 // Click a waypoint to name it somewhere. With a tool field open that is the
 // tool's argument; otherwise it is the teleport box, which is the other thing
 // on this page that takes a waypoint — and doing nothing was the third option,
 // which is what it used to do.
 map.addEventListener('click',e=>{const best=vertAt(e);if(!best)return;
  const label=best.name||('v'+best.id);
  if(selTool){$('argbox').value=label;$('argbox').focus()}
  // The map only ever draws one level, so a waypoint clicked on it is on that
  // level and the box should say which — an empty level box means "wherever the
  // dashboard already is", which is the same place today and not tomorrow.
  else{$('tpwhere').value=label;$('tplevel').value=curLevel;$('tpwhere').focus()}})})();
loadTools();
connect();
</script></body></html>"""


def as_message(name, res):
    """A tool result as one human line — the `message` the dashboard logs.

    Every tool returns a dict shaped for the caller that needs it; this is the one
    place that renders it for someone reading. Without it the page logged
    `undefined`, because it asks for a field the dream's routes supplied and mine
    did not.
    """
    if not isinstance(res, dict):
        return str(res)
    if not res.get("ok"):
        return str(res.get("error") or res.get("message") or "failed")
    if res.get("message"):
        return str(res["message"])
    if name == "go_to":
        if res.get("shown") is False:
            return f"arrived {res.get('at')} — robot only, no viewer connected"
        return f"arrived {res.get('at')}" + (
            f" via {' -> '.join(res.get('route', [])[1:-1])}"
            if len(res.get("route") or []) > 2 else "")
    if name in ("face", "turn"):
        return f"facing {res.get('facing')}"
    if name == "go_to" and res.get("shown") is False:
        return f"arrived {res.get('at')} (robot only — no viewer connected)"
    if name in ("open_door", "close_door"):
        return f"{res.get('door')} {res.get('mode')}"
    if name == "plan_route":
        head = (f"{res['needs_level_change']} first — " if res.get("needs_level_change")
                else "")
        tail = f"; then {res['then']}" if res.get("then") else ""
        return (head + f"{len(res.get('steps') or [])} step(s): "
                + "; ".join(res.get("steps") or ["(already there)"]) + tail)
    if name == "get_path":
        return " -> ".join(res.get("path") or [])
    if name == "where":
        return (f"at {res.get('at')} on {res.get('level')}, facing "
                f"{res.get('face') or 'nothing'}; neighbours "
                f"{', '.join(res.get('neighbours') or [])}")
    if name == "get_graph":
        return f"{len(res.get('vertices') or [])} waypoints on {res.get('level')}"
    if name in ("pick", "place"):
        return f"{name} {res.get('item')} — carrying {', '.join(res.get('inventory') or []) or 'nothing'}"
    if name == "write_todos":
        return f"{len(res.get('todos') or [])} subtask(s)"
    if name == "write_mission":
        return f"mission: {res.get('mission')}"
    if name == "take_lift":
        return (f"{res.get('lift')}: {len(res.get('template') or [])} steps installed — "
                f"next {res.get('next')}")
    if name == "select_lift":
        return f"selected {res.get('selected_lift')}, cabin {res.get('cabin')}"
    if name == "call_lift":
        if res.get("rode"):
            return (f"rode {res.get('lift')} to {res.get('level')} — now at "
                    f"{res.get('at')}")
        return f"{res.get('lift')} called to {res.get('floor')}"
    return json.dumps({k: v for k, v in res.items()
                       if k not in ("recent_log", "ok")})[:200]


# ---- HTTP -------------------------------------------------------------------
@app.route("/")
def r_index():
    """The dashboard: the splat viewer as the rollout, the tools beside it."""
    return (PAGE.replace("VIEWER", json.dumps(ST["viewer_base"]))
                .replace("PROJECT", json.dumps(ST["project"])),
            200, {"Content-Type": "text/html; charset=utf-8"})


@app.route("/state")
def r_state():
    return jsonify(state_dict())


@app.route("/tools")
def r_tools():
    return jsonify([{"name": n, "desc": t["desc"],
                     "params": [{"name": p, "desc": d} for p, d in t["params"]]}
                    for n, t in sorted(TOOLS.items())])


@app.route("/tool", methods=["POST", "OPTIONS"])
@app.route("/invoke", methods=["POST", "OPTIONS"])
def r_tool():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(force=True, silent=True) or {}
    name = body.get("tool") or body.get("name") or ""
    args = body.get("args") or body.get("arguments") or {}
    # The dashboard sends "toolname arg" as one string — a tool button with its one
    # field filled in. Split it against the tool's own first parameter.
    if not name and body.get("text"):
        head, _, rest = str(body["text"]).strip().partition(" ")
        name, t = head, TOOLS.get(head)
        if t and t["params"] and rest.strip():
            args = {t["params"][0][0]: rest.strip()}
    t = TOOLS.get(name)
    if not t:
        return jsonify(ok=False, error=f"no such tool: {name}",
                       tools=sorted(TOOLS)), 400
    log(f"{name}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
    blocked = _mission_gate(name)
    if blocked is not None:
        res = blocked
    elif name in MOVERS:
        # One move at a time. Two overlapping moves are the surest way to
        # desync: the second reads a position the first is halfway through
        # leaving, then commands a corridor out of a waypoint nobody is at.
        # Queue rather than refuse — a caller that asked to move wants the move,
        # and every mover here already blocks until it lands, so waiting behind
        # one is the same promise it was already making.
        waited = time.time()
        with MOVE:
            held = time.time() - waited
            if held > 0.5:
                log(f"{name} waited {held:.1f}s for the previous move", "err")
            res = _post_tool(name, args, t["fn"](**args))
    else:
        res = _post_tool(name, args, t["fn"](**args))
    msg = as_message(name, res)
    log(("" if res.get("ok") else "! ") + msg, "ok" if res.get("ok") else "err")
    return jsonify({**res, "message": msg})


@app.route("/command", methods=["POST", "OPTIONS"])
def r_command():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(force=True, silent=True) or {}
    text = body.get("text") or body.get("command") or ""
    res = handle(text)
    return jsonify({**res, "message": as_message(res.pop("_tool", ""), res)})


@app.route("/graph")
def r_graph():
    """The nav graph, in floorplan pixels where a projection could be fitted, so
    the minimap draws the building rather than an abstract diagram."""
    m2px = ST.get("m2px")
    built = set(built_scenes())
    verts = []
    for i, (name, x, y) in enumerate(ST["verts"]):
        px, py = m2px(x, y) if m2px else (x, y)
        verts.append({"id": i, "name": name or f"v{i}",
                      "px": round(px, 1), "py": round(py, 1),
                      "lift": i in ST["lift_of"], "built": lab(i) in built,
                      "door": any(door_between(i, j) for j, _ in ST["adj"].get(i, []))})
    edges, seen = [], set()
    for u in ST["adj"]:
        for v, _ in ST["adj"][u]:
            key = tuple(sorted((u, v)))
            if key in seen:
                continue
            seen.add(key)
            name = door_between(u, v)
            edges.append({"u": u, "v": v, "door": bool(name),
                          "open": name in ST["open_doors"]})
    return jsonify(w=ST.get("fp_w", 0), h=ST.get("fp_h", 0),
                   has_floorplan=bool(ST.get("fp_png") and m2px),
                   level=ST["level"], verts=verts, edges=edges)


@app.route("/floorplan.png")
def r_floorplan():
    png = ST.get("fp_png")
    if not png:
        return ("no floorplan for this level", 404)
    with open(png, "rb") as f:
        return (f.read(), 200, {"Content-Type": "image/png",
                                "Cache-Control": "max-age=3600"})


@app.route("/events")
def r_events():
    def stream(q):
        try:
            yield f"data: {json.dumps({'type': 'state', **state_dict()})}\n\n"
            while True:
                try:
                    yield f"data: {json.dumps(q.get(timeout=20))}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            BUS.drop(q)
    return Response(stream(BUS.listen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/viewer/events")
def r_viewer_events():
    """The command channel the splat viewer opens. One viewer at a time — two
    would each walk half the corridors and neither would be where the robot is."""
    def stream(q):
        # One viewer at a time, enforced rather than merely asserted above. A tab
        # left open from an earlier reload keeps its stream alive, receives every
        # command, and reports where IT stands — so the model follows whichever
        # tab moved last. That is the desync that kept coming back: three tabs
        # were attached, two of them forgotten. The newest is the one someone is
        # actually looking at, so the older ones are told to stand down and are
        # dropped from the fan-out before any command can reach them.
        with VIEWER.lock:
            stale = [oq for oq in VIEWER.qs if oq is not q]
            VIEWER.qs = [oq for oq in VIEWER.qs if oq is q]
        for oq in stale:
            try:
                oq.put_nowait({"op": "bye", "why": "a newer viewer tab took over"})
            except queue.Full:
                pass
        if stale:
            log(f"{len(stale)} older viewer tab(s) stood down — newest one drives",
                "err")
        with ST["lock"]:
            # Counted from the fan-out itself. A page that navigated away leaves
            # its connection to be collected whenever the server next tries to
            # write, so a flag set by any listener and cleared by any disconnect
            # can read "attached" while nothing is listening.
            ST["viewers"] = len(VIEWER.qs)
            ST["viewer_up"] = True
        log("splat viewer connected")
        push_state()
        try:
            while True:
                try:
                    msg = q.get(timeout=20)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(msg)}\n\n"
                if isinstance(msg, dict) and msg.get("op") == "bye":
                    # Close it here. Dropping the queue from the fan-out is not
                    # enough on its own: the stream would sit in this loop
                    # sending keepalives, so the evicted tab would still look
                    # connected to itself and never say it had stood down.
                    return
        finally:
            VIEWER.drop(q)
            with ST["lock"]:
                # From the fan-out, not a decrement: an evicted tab disconnecting
                # must not zero the count while the newest viewer is still live.
                ST["viewers"] = len(VIEWER.qs)
                ST["viewer_up"] = ST["viewers"] > 0
            log(f"splat viewer disconnected ({ST['viewers']} left)", "err")
            push_state()
    return Response(stream(VIEWER.listen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/viewer/done", methods=["POST", "OPTIONS"])
def r_viewer_done():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(force=True, silent=True) or {}
    with ST["lock"]:
        waiting = ST["waiting"].pop(body.get("id"), None)
    if waiting:
        done, box = waiting
        box["res"] = body
        done.set()
    return jsonify(ok=True)


@app.route("/viewer/at", methods=["POST", "OPTIONS"])
def r_viewer_at():
    """The viewer says where it has arrived; the model follows it.

    A corridor can be walked by hand from the viewer's own panel, with nothing
    sent through the tool surface. The server used to hear nothing, so it went on
    believing the robot stood where it last put it — and the next command named a
    neighbour of that waypoint, which the world the camera is actually in has
    never heard of. That is the "X is not a lane out of Y" refusal: not a bad
    command, two halves in different places.

    The robot is moved to match, because it is the model that is behind, not the
    viewer.
    """
    if request.method == "OPTIONS":
        return ("", 204)
    scene = str((request.get_json(force=True, silent=True) or {}).get("scene", ""))
    level, _, name = scene.partition(".")
    if not name:
        return jsonify(ok=False, error="need a scene"), 400
    if level and level != ST["level"] and level in ST["levels"]:
        switch_level(level, land_on=name)
        return jsonify(ok=True, at=lab(ST["cur"]), level=ST["level"])
    try:
        v = find_vertex(ST["verts"], name)
    except SystemExit as e:
        return jsonify(ok=False, error=str(e)), 400
    if v == ST["cur"]:
        return jsonify(ok=True, at=lab(v), level=ST["level"], note="already there")
    if lab(v) not in built_scenes():
        # It cannot be standing in a world that does not exist, so this is a
        # stale or malformed report. Refusing keeps the model where it is rather
        # than following the viewer somewhere neither of them can be.
        return jsonify(ok=False, error=f"no splat world for {lab(v)}"), 400
    with ST["lock"]:
        ST["prev"], ST["cur"], ST["face"] = ST["cur"], v, None
    # Placed, not driven: drive_path walks pairs of waypoints and does nothing
    # with one, and a camera that jumped there by hand did not walk a corridor
    # either — so the robot should not pretend to.
    nb = next((n for n, _ in ST["adj"].get(v, [])), None)
    yaw = _az(v, nb) if nb is not None else 0.0
    bridge("/reset", {"level": ST["level"], "x": ST["verts"][v][1],
                      "y": ST["verts"][v][2], "yaw": yaw})
    with ST["lock"]:
        ST["yaw"] = yaw
    push_state()
    log(f"viewer walked to {lab(v)} — following")
    return jsonify(ok=True, at=lab(v), level=ST["level"])


@app.route("/viewer/hello", methods=["POST", "OPTIONS"])
def r_viewer_hello():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(force=True, silent=True) or {}
    log(f"viewer standing at {body.get('at') or '?'}")
    return jsonify(ok=True, expect=scene_of(ST["cur"]))


@app.route("/reset", methods=["POST", "OPTIONS"])
@app.route("/teleport", methods=["POST", "OPTIONS"])   # what it was called first
def reset():
    """Put the robot at a waypoint, shut everything, and start again from there.

    Not just a move: a run leaves doors held open behind it, and starting the next
    one in a building someone has already walked through is how a test passes for
    the wrong reason. Everything closes, so the plan that follows has to open what
    it needs.

    An operator action, deliberately not in TOOLS: an agent that can teleport is
    not solving the problem it was asked to solve.

    Both halves move together or the call fails. Resetting the bridge alone would
    leave the dashboard describing a place the robot had left — the failure this
    whole surface exists to avoid, and one it shipped with once already.
    """
    if request.method == "OPTIONS":
        return ("", 204)
    b = request.get_json(force=True, silent=True) or {}
    where = str(b.get("waypoint") or b.get("to") or "").strip()
    level = str(b.get("level") or ST["level"]).strip()
    if not where:
        return jsonify(ok=False, error="need a waypoint"), 400
    if level not in ST["levels"]:
        return jsonify(ok=False, error=f"no level {level}; have {ST['levels']}"), 400

    if level != ST["level"]:
        if not switch_level(level, land_on=where):
            return jsonify(ok=False, error=f"no waypoint {where!r} on {level}"), 400
    else:
        try:
            v = find_vertex(ST["verts"], where)
        except SystemExit as e:
            return jsonify(ok=False, error=str(e)), 400
        with ST["lock"]:
            ST.update({"cur": v, "prev": None, "face": None, "yaw": None,
                       "open_doors": set()})
    cur = ST["cur"]
    # Face the first neighbour, so it lands looking down a corridor rather than at
    # whatever bearing it happened to be holding on another floor.
    nb = next((v for v, _ in ST["adj"].get(cur, [])), None)
    yaw = _az(cur, nb) if nb is not None else 0.0
    res = bridge("/reset", {"level": ST["level"], "x": ST["verts"][cur][1],
                            "y": ST["verts"][cur][2], "yaw": yaw})
    if ST.get("galaxea") and not res.get("ok"):
        return jsonify(ok=False, at=lab(cur), level=ST["level"],
                       error="the world model moved but the robot did not — "
                             f"the bridge said: {res or 'nothing'}"), 502
    with ST["lock"]:
        ST["yaw"] = yaw
        ST["open_doors"] = set()
    # Shut every door and lift, and stop the bridge holding any open — otherwise
    # its door_keeper republishes OPEN and undoes this a moment later.
    # {} not None: bridge() sends GET when there is no body, and /close_all is
    # POST — which came back 405 and was swallowed as "no doors closed".
    shut = bridge("/close_all", {})
    push_state()
    _push_context()
    # Not logged. The log is what the agent reads back as `recent_log` to decide
    # what to do next, and an operator putting the robot somewhere is not
    # something it did or should reason about — it would read as an action of its
    # own that it cannot account for.
    n = shut.get("n", 0)
    return jsonify(ok=True, at=lab(cur), level=ST["level"],
                   facing=lab(nb) if nb is not None else None,
                   robot=bool(ST.get("galaxea")), closed=n,
                   message=f"{lab(cur)} on {ST['level']}"
                           + (f", facing {lab(nb)}" if nb is not None else "")
                           + (f" — {n} doors shut" if n else ""))


@app.route("/statemodel")
def r_statemodel():
    return jsonify(text=build_context())


@app.route("/agent", methods=["POST", "OPTIONS"])
def r_agent():
    if request.method == "OPTIONS":
        return ("", 204)
    if _AGENT["graph"] is None and build_agent() is None:
        return jsonify(ok=False, error=f"agent unavailable: {_AGENT['err']}")
    from langchain_core.messages import HumanMessage
    text = (request.get_json(force=True, silent=True) or {}).get("text", "")
    _CANCEL.clear()
    _RUN.set()
    log(f"agent ▶ {text}", "mission")
    prompt = (f"New mission from the operator: {text}\n\nCurrent world model:\n"
              f"{build_context()}\n\nSet the mission and subtasks, then execute them.")
    cfg = {"configurable": {"thread_id": "interactive"}, "recursion_limit": 300}

    def run():
        try:
            out = _AGENT["graph"].invoke({"messages": [HumanMessage(content=prompt)]}, cfg)
            final = out["messages"][-1].content if out.get("messages") else ""
            todos = ST.get("todos") or []
            if todos and all(t["status"] == "completed" for t in todos):
                log("✓ mission complete — every subtask verified")
            log(str(final)[:400])
        except Exception as e:
            log(f"agent error: {e}", "err")
        finally:
            _RUN.set()
            _push_context()

    # In a thread: a mission is many walks long, and the operator needs /pause and
    # /cancel to be answerable while it runs.
    threading.Thread(target=run, daemon=True).start()
    return jsonify(ok=True, started=True)


@app.route("/pause", methods=["POST", "OPTIONS"])
def r_pause():
    if request.method == "OPTIONS":
        return ("", 204)
    _RUN.clear()
    log("⏸ paused — the agent's next tool call will block", "warn")
    return jsonify(ok=True, paused=True)


@app.route("/resume", methods=["POST", "OPTIONS"])
def r_resume():
    if request.method == "OPTIONS":
        return ("", 204)
    _RUN.set()
    log("▶ resumed")
    return jsonify(ok=True, paused=False)


@app.route("/cancel", methods=["POST", "OPTIONS"])
def r_cancel():
    if request.method == "OPTIONS":
        return ("", 204)
    _CANCEL.set()
    _RUN.set()      # unblock a paused agent so it can see the cancel and stop
    log("✖ mission cancelled", "err")
    return jsonify(ok=True, cancelled=True)


@app.route("/viewer/pose")
def r_viewer_pose():
    """Where the splat camera is, asked of the viewer and answered here.

    The viewer is a static page and cannot host a route of its own, so this is
    the only way to see it from outside a browser. Everything it reports is in
    that world's own coordinates — position, forward, yaw, whether a walk is
    running and how far along — plus the lanes and marks it is working from.
    """
    res = viewer_call("pose", timeout=20)
    if res.get("no_viewer"):
        # Say so rather than answering ok with nothing in it. A pose endpoint
        # that returns success when no viewer is attached is worse than an
        # error: it reads as "the camera is nowhere".
        return jsonify(ok=False, error="no splat viewer connected — open "
                                       f"{ST['viewer_url']} and reload it"), 503
    return jsonify(res)


@app.route("/health")
def r_health():
    return jsonify(ok=True, viewer=bool(ST.get("viewer_up")),
                   galaxea=bool(ST.get("galaxea")), at=lab(ST["cur"]))


def galaxea_reset():
    """Resync the gazebo robot to this dashboard's level + start vertex on startup.

    Ported from galaxea_reset: the bridge keeps its state across dashboard
    restarts — a lift ride can leave it on another floor — so it is the bridge
    that has to be told where we think we are, not the other way round. Retries in
    the background; best-effort, and a no-op with no sim.
    """
    if not ST.get("galaxea"):
        return
    cur = ST["cur"]
    body = {"level": ST["level"], "x": ST["verts"][cur][1], "y": ST["verts"][cur][2],
            "yaw": _az(cur, ST["face"]) if ST["face"] is not None else 0.0}

    def _post():
        for _ in range(30):
            if bridge("/reset", body, timeout=3):
                return
            time.sleep(2)

    threading.Thread(target=_post, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--nav", required=True)
    ap.add_argument("--building", default="")
    ap.add_argument("--level", default="L11")
    ap.add_argument("--start", default="lift_lobby")
    ap.add_argument("--project", default="multilevel_office")
    ap.add_argument("--viewer", default="http://localhost:8081")
    ap.add_argument("--port", type=int, default=8086)
    a = ap.parse_args()

    level, verts, adj, _ = load_nav(a.nav, a.level)
    ST.update({
        "lock": threading.RLock(), "waiting": {}, "seq": 0,
        "level": level, "verts": verts, "adj": adj,
        "doors": doors_of(a.nav, level), "lift_of": lift_of(a.nav, level),
        "nav": a.nav, "levels": levels_of(a.nav), "building": a.building,
        "open_doors": set(), "inventory": [], "mission": "", "todos": [],
        "prev": None, "face": None, "selected_lift": None, "sel_cabin": None,
        # The heading both sides are holding, in the nav graph's frame. Kept here
        # rather than asked for, so the arc of the next turn is computed against
        # the same number the robot last turned to.
        "yaw": None,
        "galaxea": os.environ.get("GALAXEA_URL", "").rstrip("/"),
        "project": a.project,
    })
    ST["cur"] = find_vertex(verts, a.start)
    png, fw, fh, m2px = build_floorplan(a.building, level)
    ST["fp_png"], ST["fp_w"], ST["fp_h"], ST["m2px"] = png, fw, fh, m2px
    ST["viewer_base"] = a.viewer.rstrip("/")
    ST["viewer_url"] = (f"{a.viewer}/?url=files/{a.project}/splats/"
                        f"{scene_of(ST['cur'])}/world.ply"
                        f"&agent=http://localhost:{a.port}")

    galaxea_reset()
    threading.Thread(target=robot_watchdog, daemon=True).start()
    threading.Thread(target=pose_pump, daemon=True).start()
    print(f"[interactive] {len(verts)} waypoints on {level}, standing at {lab(ST['cur'])}",
          flush=True)
    print(f"[interactive] tools: {', '.join(sorted(TOOLS))}", flush=True)
    print(f"[interactive] robot: {ST['galaxea'] or 'none (set GALAXEA_URL)'}", flush=True)
    print(f"[interactive] open the viewer at:\n    {ST['viewer_url']}", flush=True)
    app.run(host="0.0.0.0", port=a.port, threaded=True)


if __name__ == "__main__":
    main()
