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
    if not ST.get("viewer_up"):
        return {"ok": False, "error": "no splat viewer connected — open "
                                      f"{ST['viewer_url']} in a browser"}
    cid = f"c{int(time.time() * 1000)}_{ST['seq']}"
    ST["seq"] += 1
    done = threading.Event()
    box = {}

    with ST["lock"]:
        ST["waiting"][cid] = (done, box)
    VIEWER.send({"id": cid, "op": op, **kw})
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
    bridge("/goto", {"waypoints": pts, "level": ST["level"]})


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


def blocked_on(path):
    """[(u, v, door)] for every closed door the path would walk through."""
    out = []
    for u, v in zip(path, path[1:]):
        name = door_between(u, v)
        if name and name not in ST["open_doors"]:
            out.append((u, v, name))
    return out


# ---- state ------------------------------------------------------------------
def state_dict():
    cur = ST["cur"]
    return {"level": ST["level"], "cur": cur, "at": lab(cur), "scene": scene_of(cur),
            "prev": None if ST["prev"] is None else lab(ST["prev"]),
            "face": None if ST["face"] is None else lab(ST["face"]),
            "neighbours": [lab(v) for v, _ in ST["adj"].get(cur, [])],
            "open_doors": sorted(ST["open_doors"]),
            "inventory": list(ST["inventory"]),
            "viewer": bool(ST.get("viewer_up")),
            "galaxea": bool(ST.get("galaxea")),
            "mission": ST.get("mission", ""),
            "todos": ST.get("todos", [])}


def push_state():
    BUS.send({"type": "state", **state_dict()})


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
    shut = blocked_on(path)
    if shut:
        u, v, name = shut[0]
        return {"ok": False,
                "error": f"BLOCKED: {name} between {lab(u)} and {lab(v)} is shut. "
                         f"go_to {lab(u)}, then face {lab(v)}, then open_door, "
                         f"then go_to {lab(tgt)}."}

    # The robot drives the whole polyline at once, the viewer walks it a corridor at
    # a time — one is a pose-follow along a line, the other is a world per vertex.
    drive_robot(path)
    walked = []
    for u, v in zip(path, path[1:]):
        res = viewer_call("walk", to=lab(v))
        if not res.get("ok"):
            with ST["lock"]:
                ST["cur"], ST["prev"] = u, ST["prev"]
            push_state()
            return {"ok": False, "error": res.get("error"), "reached": walked}
        with ST["lock"]:
            ST["prev"], ST["cur"], ST["face"] = u, v, None
        walked.append(lab(v))
        push_state()
    if not wait_robot(ST["verts"][tgt][1], ST["verts"][tgt][2]):
        log(f"robot has not reported reaching {lab(tgt)}", "err")
    return {"ok": True, "at": lab(tgt), "route": [lab(i) for i in path]}


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
    yaw = _az(cur, tgt)
    turn_robot(yaw)
    res = viewer_call("face", to=lab(tgt), timeout=30)
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
    if ST["face"] in ns:
        i = ns.index(ST["face"])
        i = (i + (1 if str(direction).lower().startswith("l") else -1)) % len(ns)
    else:
        i = 0
    return face(lab(ns[i]))


@tool("Open the door you are facing.", [("to", "waypoint beyond the door (optional)")])
def open_door(to=""):
    return _door(to, "open")


@tool("Close the door you are facing.", [("to", "waypoint beyond the door (optional)")])
def close_door(to=""):
    return _door(to, "close")


def _door(to, mode):
    cur = ST["cur"]
    tgt = ST["face"]
    if str(to).strip():
        try:
            tgt = find_vertex(ST["verts"], to)
        except SystemExit as e:
            return {"ok": False, "error": str(e)}
    if tgt is None:
        return {"ok": False, "error": f"not facing anything — face <waypoint> first"}
    name = door_between(cur, tgt)
    if not name:
        return {"ok": False, "error": f"no door between {lab(cur)} and {lab(tgt)}"}
    bridge("/door", {"door": name, "mode": mode})
    with ST["lock"]:
        if mode == "open":
            ST["open_doors"].add(name)
        else:
            ST["open_doors"].discard(name)
    push_state()
    return {"ok": True, "door": name, "mode": mode}


@tool("Which lift to take, chosen before facing it.", [("lift", "lift name, e.g. lift1")])
def select_lift(lift=""):
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
    lift = ST.get("selected_lift")
    if not lift:
        cabins = [ST["lift_of"][v] for v, _ in ST["adj"].get(ST["cur"], [])
                  if v in ST["lift_of"]]
        if not cabins:
            return {"ok": False, "error": f"no lift at {lab(ST['cur'])}"}
        lift = cabins[0]
    floor = str(level).strip() or ST["level"]
    res = bridge("/call_lift", {"lift": lift, "floor": floor})
    return {"ok": True, "lift": lift, "floor": floor, "bridge": res}


@tool("Pick an item up at this waypoint.", [("item", "item name")])
def pick(item=""):
    res = bridge("/pick", {"item": str(item), "vertex": lab(ST["cur"])})
    with ST["lock"]:
        ST["inventory"].append(str(item))
    push_state()
    return {"ok": True, "item": item, "inventory": list(ST["inventory"]), "bridge": res}


@tool("Put an item down at this waypoint.", [("item", "item name")])
def place(item=""):
    if str(item) not in ST["inventory"]:
        return {"ok": False, "error": f"not carrying {item}"}
    res = bridge("/place", {"item": str(item), "vertex": lab(ST["cur"])})
    with ST["lock"]:
        ST["inventory"].remove(str(item))
    push_state()
    return {"ok": True, "item": item, "inventory": list(ST["inventory"]), "bridge": res}


@tool("The obstacle-aware sequence of steps to reach a waypoint, without moving.",
      [("to", "destination waypoint name/id")])
def plan_route(to):
    try:
        tgt = find_vertex(ST["verts"], to)
    except SystemExit as e:
        return {"ok": False, "error": str(e)}
    path = dijkstra(ST["adj"], ST["cur"], tgt)
    if not path:
        return {"ok": False, "error": f"no path to {to}"}
    steps, walked = [], ST["cur"]
    for u, v in zip(path, path[1:]):
        name = door_between(u, v)
        if name and name not in ST["open_doors"]:
            if walked != u:
                steps.append(f"go_to {lab(u)}")
                walked = u
            steps += [f"face {lab(v)}", "open_door"]
    steps.append(f"go_to {lab(tgt)}")
    return {"ok": True, "route": [lab(i) for i in path], "steps": steps}


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
def handle(text):
    """The same phrasings the dream dashboard's text box took."""
    t = " ".join(str(text).split()).lower()
    if not t:
        return {"ok": False, "error": "say something"}
    if t in ("where", "where am i", "look"):
        return where()
    m = re.fullmatch(r"(?:go|walk|go to|walk to)\s+(?:to\s+)?(.+)", t)
    if m:
        return go_to(m.group(1))
    m = re.fullmatch(r"face\s+(.+)", t)
    if m:
        return face(m.group(1))
    m = re.fullmatch(r"turn\s+(left|right)", t)
    if m:
        return turn(m.group(1))
    m = re.fullmatch(r"open\s+door(?:\s+to\s+(.+))?", t)
    if m:
        return open_door(m.group(1) or "")
    m = re.fullmatch(r"close\s+door(?:\s+to\s+(.+))?", t)
    if m:
        return close_door(m.group(1) or "")
    m = re.fullmatch(r"call\s+lift\s*(.*)", t)
    if m:
        return call_lift(m.group(1))
    return {"ok": False, "error": f"did not understand {text!r}. "
                                  f"Try: go to <waypoint>, face <waypoint>, turn left, "
                                  f"open door, where."}


# ---- verification: a subtask completes when the world says it did ------------
# Ported from the dream harness, and kept for the same reason: an agent that marks
# its own work done will mark it done. Here the world is the viewer's reported
# position and the bridge's RMF state, so a subtask cannot complete unverified.

VERIFY = {}
SUBTASK_TOOLS = {"go_to", "turn", "face", "open_door", "close_door",
                 "select_lift", "call_lift", "pick", "place"}


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
    name, want = r.get("door"), r.get("mode") == "open"
    if ST.get("galaxea"):
        # The bridge owns the real door; ask it rather than trusting our own record.
        st = (bridge("/state") or {}).get("doors") or {}
        if name in st and bool(st[name]) != want:
            return False, f"{name} did not reach {r.get('mode')}"
    return True, f"{name} {r.get('mode')}"


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
<title>interactive — walk the building</title>
<style>
:root{--bg:#0d1117;--fg:#c9d1d9;--dim:#8b98a8;--line:#22303f;--ok:#6ea8fe;--err:#ff7b72}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;height:100vh;display:flex}
#left{flex:1 1 auto;display:flex;flex-direction:column;min-width:0}
#right{flex:0 0 380px;border-left:1px solid var(--line);display:flex;flex-direction:column;overflow:hidden}
iframe{flex:1 1 auto;border:0;width:100%;background:#000}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin:0;padding:8px 10px;border-bottom:1px solid var(--line)}
.pane{display:flex;flex-direction:column;min-height:0;border-bottom:1px solid var(--line)}
.pane.grow{flex:1 1 0}
pre{margin:0;padding:8px 10px;overflow:auto;white-space:pre-wrap;font-size:12px;flex:1 1 auto}
#bar{display:flex;gap:6px;padding:8px;border-top:1px solid var(--line);background:#0b0f14}
input,button,select{background:#161b22;color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:6px 8px;font:inherit}
input{flex:1 1 auto;min-width:0}
button{cursor:pointer;white-space:nowrap}
button:hover{border-color:var(--ok);color:var(--ok)}
#tools{display:flex;flex-wrap:wrap;gap:4px;padding:8px}
#tools button{font-size:11px;padding:3px 7px}
.log div{padding:1px 10px}
.log .err{color:var(--err)} .log .warn{color:#e3b341} .log .mission{color:var(--ok)}
#status{padding:6px 10px;color:var(--dim);border-bottom:1px solid var(--line);font-size:12px}
#status b{color:var(--fg);font-weight:600}
canvas{display:block;width:100%;background:#0b0f14}
</style></head><body>
<div id=left>
  <div id=status>connecting…</div>
  <iframe id=view></iframe>
  <div id=bar>
    <input id=cmd placeholder="go to apex_lab   ·   face v0   ·   open door   ·   where">
    <button onclick=send()>run</button>
    <button onclick=mission()>mission</button>
    <button onclick=post('/pause')>pause</button>
    <button onclick=post('/resume')>resume</button>
    <button onclick=post('/cancel')>cancel</button>
  </div>
</div>
<div id=right>
  <div class=pane><h2>tools</h2><div id=tools></div></div>
  <div class="pane grow"><h2>world model</h2><pre id=model>…</pre></div>
  <div class="pane grow"><h2>log</h2><pre class=log id=log></pre></div>
</div>
<script>
const $ = (id) => document.getElementById(id);
let ST = {};

const say = (text, level) => {
  const d = document.createElement("div");
  d.className = level || "ok"; d.textContent = text;
  $("log").appendChild(d); $("log").scrollTop = 1e9;
};

const post = (path, body) =>
  fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
               body: JSON.stringify(body || {})}).then((r) => r.json());

function send() {
  const text = $("cmd").value.trim();
  if (!text) return;
  $("cmd").value = "";
  say("> " + text);
  post("/command", {text}).then((r) => { if (!r.ok) say(r.error, "err"); });
}
function mission() {
  const text = $("cmd").value.trim() || prompt("mission?");
  if (!text) return;
  $("cmd").value = "";
  say("mission: " + text, "mission");
  post("/agent", {text}).then((r) => { if (!r.ok) say(r.error, "err"); });
}
$("cmd").addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });

// The viewer is the rollout, embedded rather than linked: one page, and the
// ?agent= parameter is what hands its camera to this server.
function openView(scene) {
  const url = `${VIEWER}/?url=files/${PROJECT}/splats/${scene}/world.ply` +
              `&agent=${encodeURIComponent(location.origin)}`;
  if ($("view").dataset.scene !== scene) {
    $("view").dataset.scene = scene;
    $("view").src = url;
  }
}

function paint(s) {
  ST = s;
  $("status").innerHTML =
    `at <b>${s.at}</b> · ${s.level} · facing ${s.face || "—"} · ` +
    `neighbours ${s.neighbours.join(", ")} · ` +
    `viewer ${s.viewer ? "<b>on</b>" : "off"} · robot ${s.galaxea ? "<b>on</b>" : "off"}`;
  if (!$("view").dataset.scene) openView(s.scene);
}

fetch("/tools").then((r) => r.json()).then((tools) => {
  for (const name of Object.keys(tools).sort()) {
    const b = document.createElement("button");
    b.textContent = name;
    b.title = tools[name].desc;
    b.onclick = () => {
      const args = {};
      for (const p of tools[name].params) {
        const v = prompt(`${name}: ${p.name}\n${p.desc}`);
        if (v === null) return;
        if (v !== "") args[p.name] = v;
      }
      say(`> ${name}(${JSON.stringify(args)})`);
      post("/tool", {tool: name, args}).then((r) => {
        if (!r.ok) say(r.error || JSON.stringify(r), "err");
      });
    };
    $("tools").appendChild(b);
  }
});

const es = new EventSource("/events");
es.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.type === "state") paint(m);
  else if (m.type === "log") say(m.text, m.level);
  else if (m.type === "statemodel") $("model").textContent = m.text;
  else if (m.type === "tool") say(`  ${m.tool} -> ${JSON.stringify(m.result).slice(0, 200)}`);
};
es.onerror = () => say("dashboard disconnected — retrying", "err");
fetch("/statemodel").then((r) => r.json()).then((d) => { $("model").textContent = d.text; });
</script></body></html>"""


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
    return jsonify({n: {"desc": t["desc"],
                        "params": [{"name": p, "desc": d} for p, d in t["params"]]}
                    for n, t in TOOLS.items()})


@app.route("/tool", methods=["POST", "OPTIONS"])
@app.route("/invoke", methods=["POST", "OPTIONS"])
def r_tool():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(force=True, silent=True) or {}
    name = body.get("tool") or body.get("name") or ""
    args = body.get("args") or body.get("arguments") or {}
    t = TOOLS.get(name)
    if not t:
        return jsonify(ok=False, error=f"no such tool: {name}",
                       tools=sorted(TOOLS)), 400
    log(f"{name}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
    blocked = _mission_gate(name)
    res = blocked if blocked is not None else _post_tool(name, args, t["fn"](**args))
    BUS.send({"type": "tool", "tool": name, "args": args, "result": res})
    return jsonify(res)


@app.route("/command", methods=["POST", "OPTIONS"])
def r_command():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(force=True, silent=True) or {}
    return jsonify(handle(body.get("text") or body.get("command") or ""))


@app.route("/graph")
def r_graph():
    return jsonify(get_graph())


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
        with ST["lock"]:
            ST["viewer_up"] = True
        log("splat viewer connected")
        push_state()
        try:
            while True:
                try:
                    yield f"data: {json.dumps(q.get(timeout=20))}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            VIEWER.drop(q)
            with ST["lock"]:
                ST["viewer_up"] = False
            log("splat viewer disconnected", "err")
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


@app.route("/viewer/hello", methods=["POST", "OPTIONS"])
def r_viewer_hello():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(force=True, silent=True) or {}
    log(f"viewer standing at {body.get('at') or '?'}")
    return jsonify(ok=True, expect=scene_of(ST["cur"]))


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


@app.route("/health")
def r_health():
    return jsonify(ok=True, viewer=bool(ST.get("viewer_up")),
                   galaxea=bool(ST.get("galaxea")), at=lab(ST["cur"]))


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
        "open_doors": set(), "inventory": [], "mission": "", "todos": [],
        "prev": None, "face": None, "selected_lift": None, "sel_cabin": None,
        "galaxea": os.environ.get("GALAXEA_URL", "").rstrip("/"),
        "project": a.project,
    })
    ST["cur"] = find_vertex(verts, a.start)
    ST["viewer_base"] = a.viewer.rstrip("/")
    ST["viewer_url"] = (f"{a.viewer}/?url=files/{a.project}/splats/"
                        f"{scene_of(ST['cur'])}/world.ply"
                        f"&agent=http://localhost:{a.port}")

    print(f"[interactive] {len(verts)} waypoints on {level}, standing at {lab(ST['cur'])}",
          flush=True)
    print(f"[interactive] tools: {', '.join(sorted(TOOLS))}", flush=True)
    print(f"[interactive] robot: {ST['galaxea'] or 'none (set GALAXEA_URL)'}", flush=True)
    print(f"[interactive] open the viewer at:\n    {ST['viewer_url']}", flush=True)
    app.run(host="0.0.0.0", port=a.port, threaded=True)


if __name__ == "__main__":
    main()
