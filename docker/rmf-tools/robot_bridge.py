#!/usr/bin/env python3
"""galaxea robot bridge — spawn the Galaxea R1 (r1) into a running Gazebo and make
it mirror the splat walkthrough, driven over HTTP.

Ported verbatim from dreamworld/docker/dream_interactive/robot_bridge.py; only the
paths differ, because this repo lays a project out as
assets/projects/<p>/worlds/<map>/ rather than <p>/outputs/generate_gz/. The driving,
the timing constants, the door/lift routing and every HTTP route are unchanged, so a
client written against the dream bridge works here without modification.
The model carries a `libslotcar.so` plugin, so once spawned it publishes
rmf_fleet_msgs/RobotState on /robot_state (used here for telemetry). What this bridge
does:

  1. spawn r1 at the start nav-graph waypoint (gz `create` service), then
  2. run a Flask server whose POST /goto drives r1 along a list of nav-graph waypoints
     — the SAME metric frame the splat walkthrough navigates in (both read
     worlds/<map>/nav_graphs/0.yaml).

Driving is by interpolating the robot's POSE along the polyline (gz set_pose service),
NOT by an RMF PathRequest: the wide articulated Galaxea can't physically fit through
the office doorways under slotcar, and the dream itself is a non-physical rendered
walkthrough — a pose-follow reproduces it exactly and passes doorways cleanly. Doors on
the path are still opened (rmf_door_msgs on /door_requests) so they visibly slide aside
as the robot walks through. The interactive server calls POST /goto on every traversal
so the Galaxea mirrors the splat walk edge-for-edge.
"""
import argparse
import json
import math
import os
import re
import subprocess
import threading
import time

import rclpy
import yaml
from flask import Flask, jsonify, request
from geometry_msgs.msg import Pose
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile
from rmf_door_msgs.msg import DoorMode, DoorRequest, DoorState
from rmf_fleet_msgs.msg import RobotState
from rmf_lift_msgs.msg import LiftRequest, LiftState
from ros_gz_interfaces.srv import SetEntityPose
from std_msgs.msg import String

app = Flask(__name__)
G = {}  # shared: node, pubs, clients, robot_state, args, verts, doors

# Pose-follow tuning — MATCHES the rates the dream clips were rendered at (move_gz
# defaults: 2.0 m/s, 1.25 rad/s), so the robot and the dream stay frame-in-step:
#   legs   -> DRIVE_SPEED m/s          (move.py --speed 2.0)
#   turns  -> TURN_RATE rad/s          (move.py --turn-rate 1.25 == frames turn_frames)
#   doors  -> pause DOOR_OPEN_SECS at the door, matching the dream's door_open clip (~33f/30)
DRIVE_SPEED = float(os.environ.get("DRIVE_SPEED", "2.0"))   # m/s along a leg
TURN_RATE = float(os.environ.get("TURN_RATE", "1.25"))      # rad/s in-place turn
DOOR_OPEN_SECS = float(os.environ.get("DOOR_OPEN_SECS", "1.1"))
DRIVE_HZ = 20.0


# ── doors: open the gz doors the robot's path crosses (libdoor.so on /door_requests)
# so the physical robot can pass, mirroring the dream opening doors en route. ───────
def parse_doors(world_file, elevation, ztol=1.0):
    """[(name, x, y)] for every door MODEL on this level (z ~ elevation)."""
    xml = open(world_file).read()
    out = []
    for m in re.finditer(r'<model name="([^"]*[Dd]oor[^"]*)">(.*?)</model>', xml, re.S):
        name, blk = m.group(1), m.group(2)
        pm = re.search(r"<pose>([^<]+)</pose>", blk)
        if not pm:
            continue
        p = pm.group(1).split()
        x, y, z = float(p[0]), float(p[1]), float(p[2])
        if abs(z - elevation) <= ztol:
            out.append((name, x, y))
    return out


def _seg_dist(px, py, ax, ay, bx, by):
    """Distance from point p to segment a-b."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def _seg_has_door(ax, ay, bx, by, thresh=1.2):
    """True if a door sits ON the segment a->b (so this leg walks THROUGH it)."""
    return any(_seg_dist(dx, dy, ax, ay, bx, by) <= thresh for _, dx, dy in G.get("doors", []))


def door_on_edge(ax, ay, bx, by):
    """The single door the FACED edge a->b crosses: on the segment AND nearest its
    midpoint. Disambiguates the colinear lift doors (17->18 = lift1, not lift2)."""
    mx, my = (ax + bx) / 2, (ay + by) / 2
    best, bestd = None, 1e9
    for name, dx, dy in G.get("doors", []):
        if _seg_dist(dx, dy, ax, ay, bx, by) > 1.0:
            continue
        d = math.hypot(dx - mx, dy - my)
        if d < bestd:
            best, bestd = name, d
    return [best] if best else []


def doors_on_path(waypoints, thresh=2.0):
    """Door names whose position is within `thresh` m of any path segment."""
    hit = []
    for name, dx, dy in G.get("doors", []):
        near = any(
            _seg_dist(dx, dy, waypoints[i][0], waypoints[i][1],
                      waypoints[i + 1][0], waypoints[i + 1][1]) <= thresh
            for i in range(len(waypoints) - 1)
        )
        if near:
            hit.append(name)
    return hit


def _lift_of(door_name):
    """'ShaftDoor_lift1_L11_name' -> ('lift1', 'L11'); None for a regular door."""
    m = re.match(r"(?:Shaft|Cabin)Door_(lift\d+)_([A-Za-z0-9]+)_", door_name)
    return (m.group(1), m.group(2)) if m else None


def _lift_session(lift, fresh=False):
    """A lift session id, regenerated on each open<->close TRANSITION. liblift ignores a
    door_state change that arrives on the SAME session it last acted on (so a bridge that
    closed on 'galaxea_bridge' can't reopen on it) — re-assertions reuse the id, only a new
    command mints a new one."""
    s = G.setdefault("lift_sessions", {})
    if fresh or lift not in s:
        s[lift] = f"galaxea_{lift}_{int(time.time() * 1000)}"
    return s[lift]


def _lift_request(lift, floor, door_open, session=None):
    req = LiftRequest()
    req.lift_name = lift
    req.request_time = G["node"].get_clock().now().to_msg()
    req.session_id = session or _lift_session(lift)
    req.request_type = LiftRequest.REQUEST_AGV_MODE
    req.destination_floor = floor
    req.door_state = LiftRequest.DOOR_OPEN if door_open else LiftRequest.DOOR_CLOSED
    G["lift_pub"].publish(req)


def _door_req(name, mode):
    """Open/close the door on the faced edge. A lift shaft/cabin door is owned by the
    liblift plugin — DoorRequest can't move it, only a LiftRequest to its floor can — so
    route those to /lift_requests; everything else is a normal /door_requests door."""
    lift = _lift_of(name)
    if lift:
        _lift_request(lift[0], lift[1], mode == DoorMode.MODE_OPEN)
    else:
        req = DoorRequest()
        req.request_time = G["node"].get_clock().now().to_msg()
        req.requester_id = "galaxea_bridge"
        req.door_name = name
        req.requested_mode.value = mode
        G["door_pub"].publish(req)


def open_doors(names):
    for name in names:
        _door_req(name, DoorMode.MODE_OPEN)


def close_doors(names):
    # One door at a time, and one failure cannot stop the rest: the list mixes
    # plain doors with lift shaft/cabin doors, and a lift whose state has not
    # arrived yet raises out of _door_req — which used to abandon every door
    # after it in the list, so whether a door shut depended on its position.
    for name in names:
        try:
            _door_req(name, DoorMode.MODE_CLOSED)
        except Exception as e:                        # noqa: BLE001
            print(f"[galaxea] close {name}: {e}", flush=True)


def close_all_startup():
    """Force every door + lift to CLOSED at startup. Regular doors auto-close, but liblift
    can boot with the cabin/shaft doors open and holds that state, so re-assert CLOSED for
    a few seconds so the sim always starts with doors shut."""
    time.sleep(5)   # let the world + door/lift plugins come up
    names = [n for n, _, _ in G.get("doors", [])]
    if not names:
        return
    for _ in range(24):
        if G.get("held_doors"):      # an explicit `open door` arrived — stop forcing closed
            break
        close_doors(names)
        time.sleep(0.3)
    print(f"[galaxea] closed {len(names)} doors/lifts at startup", flush=True)


def door_keeper():
    """libdoor.so / liblift auto-close (or need re-assertion to open), so republish OPEN
    every 0.3s for the doors held open by an explicit `open door` (held until `close door`).
    Movement never opens doors — doors are obstacles."""
    while True:
        time.sleep(0.3)   # fast enough that lift doors (which need ~0.2s re-assertion) open
        held = G.get("held_doors") or set()
        if held:
            open_doors(list(held))


# ── nav graph ──────────────────────────────────────────────────────────────────
def load_level(nav_yaml, level):
    """(verts, adj) for a level: verts = [(name, x, y)] in nav METRES (world frame)."""
    levels = yaml.safe_load(open(nav_yaml))["levels"]
    lvl = levels[level] if level in levels else next(iter(levels.values()))
    verts = []
    for v in lvl.get("vertices", []):
        p = v[2] if len(v) > 2 and isinstance(v[2], dict) else {}
        verts.append((p.get("name", ""), float(v[0]), float(v[1])))
    adj = {i: [] for i in range(len(verts))}
    for l in lvl.get("lanes", []):
        if l[0] != l[1]:
            adj[int(l[0])].append(int(l[1]))
    return verts, adj


def find_start(verts, key):
    """Resolve a start waypoint given by name or index -> vertex idx."""
    for i, (n, _, _) in enumerate(verts):
        if n == key:
            return i
    try:
        return int(key)
    except ValueError:
        raise SystemExit(f"start waypoint {key!r} not found")


def level_elevation(building_yaml, level):
    try:
        d = yaml.safe_load(open(building_yaml))
        return float(d["levels"][level].get("elevation", 0))
    except Exception:
        return 0.0


# ── spawn r1 into the running gazebo (gz `create` service) ───────────────────────
def spawn_robot(world, sdf, name, x, y, z, yaw):
    qz, qw = math.sin(yaw / 2.0), math.cos(yaw / 2.0)
    req = (
        f"sdf_filename: '{sdf}', name: '{name}', allow_renaming: false, "
        f"pose: {{position: {{x: {x}, y: {y}, z: {z}}}, "
        f"orientation: {{x: 0, y: 0, z: {qz}, w: {qw}}}}}"
    )
    # Remove a stale r1 first (ignore failure on a fresh world), then create. Retry
    # until the gazebo `create` service is up (world may still be loading).
    subprocess.run(
        ["gz", "service", "-s", f"/world/{world}/remove", "--reqtype", "gz.msgs.Entity",
         "--reptype", "gz.msgs.Boolean", "--timeout", "2000",
         "--req", f"name: '{name}', type: MODEL"],
        capture_output=True,
    )
    deadline = time.time() + 120
    while time.time() < deadline:
        r = subprocess.run(
            ["gz", "service", "-s", f"/world/{world}/create",
             "--reqtype", "gz.msgs.EntityFactory", "--reptype", "gz.msgs.Boolean",
             "--timeout", "3000", "--req", req],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and "data: true" in r.stdout:
            print(f"[galaxea] spawned {name} at ({x:.2f},{y:.2f},{z:.2f}) yaw={yaw:.2f}", flush=True)
            return True
        time.sleep(2)
    print(f"[galaxea] FAILED to spawn {name}: {r.stdout} {r.stderr}", flush=True)
    return False


# ── ROS 2: robot state + path publisher ─────────────────────────────────────────
def _state_cb(msg: RobotState):
    if msg.name == G["args"].robot:
        G["state"] = {
            "level": msg.location.level_name,
            "x": msg.location.x, "y": msg.location.y, "yaw": msg.location.yaw,
            "mode": int(msg.mode.mode),
        }


def _door_cb(msg: DoorState):
    """Remember every door's real state.

    Without this the bridge could open a door and had no way to know it had
    opened — a DoorRequest is a request, and the leaf takes seconds to swing. A
    caller that treats the request as the result walks a robot through a door
    that is still moving.
    """
    G.setdefault("door_state", {})[msg.door_name] = int(msg.current_mode.value)


def _lift_cb(msg: LiftState):
    G.setdefault("lifts", {})[msg.lift_name] = {
        "floor": msg.current_floor, "door": int(msg.door_state), "motion": int(msg.motion_state)}


# ── interactable items + robot inventory ──────────────────────────────────────────────
# A static record (from a user JSON, keyed by nav-vertex NAME) of what can be picked up at
# each vertex, published on /interactable_items; and the robot's carried items on /robot_inventory.
# The dream harness reads both over HTTP and drives picks via POST /pick.
def publish_items():
    if G.get("items_pub"):
        G["items_pub"].publish(String(data=json.dumps(G.get("interactables", {}))))


def publish_inventory():
    if G.get("inventory_pub"):
        G["inventory_pub"].publish(String(data=json.dumps(G.get("inventory", []))))


def call_lift_worker(lift, floor, ride=False):
    """Re-assert a LiftRequest (doors closed while travelling) until the cabin arrives at
    `floor` and stops. If `ride`, the robot is standing in the cabin and the LIFT physically
    carries it down/up — we do NOT teleport it (that fights the cabin). On arrival we only
    re-level the bridge (elevation + door set) so later navigation uses this floor."""
    session = _lift_session(lift, fresh=True)   # new command -> fresh session
    t0 = time.time()
    for _ in range(200):   # up to ~60s (big inter-floor travel is slow)
        _lift_request(lift, floor, door_open=False, session=session)
        st = G.get("lifts", {}).get(lift, {})
        if st.get("floor") == floor and st.get("motion") == 0 and time.time() - t0 > 2:
            if ride:
                G["z"], G["args"].level = level_elevation(G["args"].building, floor), floor
                if G["args"].world_file:
                    G["doors"] = parse_doors(G["args"].world_file, G["z"])
            print(f"[galaxea] {lift} arrived at {floor}", flush=True)
            return
        time.sleep(0.3)


# Which way the model itself faces at yaw 0. The R1's mesh is authored facing
# -X, so a heading computed from the map — atan2 along the segment, which is
# correct — rendered it driving backwards down every corridor. Applied here and
# only here: this is the one place a world heading becomes an orientation, and
# G["cur_yaw"] below stays in world terms, so everything that reads a heading
# back (the dashboard marker, forward, the splat viewer's lanes) is untouched.
MODEL_YAW = float(os.environ.get("DW_ROBOT_YAW_OFFSET", math.pi))


def set_pose(x, y, z, yaw):
    """Teleport r1 to a pose via the bridged gz set_pose service (fire-and-forget)."""
    req = SetEntityPose.Request()
    req.entity.name = G["args"].robot
    req.entity.type = 2  # MODEL
    p = Pose()
    p.position.x, p.position.y, p.position.z = float(x), float(y), float(z)
    face = yaw + MODEL_YAW
    p.orientation.z, p.orientation.w = math.sin(face / 2), math.cos(face / 2)
    req.pose = p
    G["pose_cli"].call_async(req)
    G["cur_yaw"] = yaw
    G["pos"] = (float(x), float(y))


def _lerp(fn, duration, cancel):
    """Drive fn(f), f in [0,1], over `duration` REAL seconds — TIME-based, not step-based,
    so the motion takes exactly `duration` regardless of per-step ROS/sleep overhead. This
    is what keeps the robot frame-in-step with the dream clip (whose duration is the same
    distance/speed or arc/rate). Returns False if cancelled."""
    dt = 1.0 / DRIVE_HZ
    t0 = time.time()
    while True:
        if cancel.is_set():
            return False
        f = 1.0 if duration <= 0 else min(1.0, (time.time() - t0) / duration)
        fn(f)
        if f >= 1.0:
            return True
        time.sleep(dt)


def spin_in_place(target_yaw, z, cancel):
    """Rotate r1 in place (hold x,y) to target_yaw over |arc|/TURN_RATE s — the same
    duration as the dream's spin/turn clip (turn_frames @ 1.25 rad/s)."""
    pos = G.get("pos") or ((G.get("state") or {}).get("x"), (G.get("state") or {}).get("y"))
    if pos[0] is None:
        return
    cyaw = G.get("cur_yaw", 0.0)
    d = (target_yaw - cyaw + math.pi) % (2 * math.pi) - math.pi
    _lerp(lambda f: set_pose(pos[0], pos[1], z, cyaw + d * f), abs(d) / TURN_RATE, cancel)


def drive_path(waypoints, z, cancel):
    """Move r1 along the nav polyline, each motion timed to match the dream clips:
    per leg turn to face it (|arc|/TURN_RATE s), then translate (dist/DRIVE_SPEED s).
    Cancellable — a new /goto pre-empts. Because move_gz rendered the clips at exactly
    DRIVE_SPEED/TURN_RATE, the robot and the dream reach every vertex and turn together.
    Doors are never opened here — a closed door blocks the walker, so any leg r1 is asked
    to walk has already had its door opened by `open door`."""
    cyaw = G.get("cur_yaw", 0.0)
    for i in range(len(waypoints) - 1):
        ax, ay = waypoints[i]
        bx, by = waypoints[i + 1]
        seg = math.hypot(bx - ax, by - ay)
        if seg < 1e-3:
            continue
        tyaw = math.atan2(by - ay, bx - ax)
        d = (tyaw - cyaw + math.pi) % (2 * math.pi) - math.pi   # shortest turn
        c0 = cyaw
        if not _lerp(lambda f, c0=c0, d=d, ax=ax, ay=ay: set_pose(ax, ay, z, c0 + d * f),
                     abs(d) / TURN_RATE, cancel):
            return
        cyaw = tyaw
        if not _lerp(lambda f, ax=ax, ay=ay, bx=bx, by=by, cyaw=cyaw:
                     set_pose(ax + (bx - ax) * f, ay + (by - ay) * f, z, cyaw),
                     seg / DRIVE_SPEED, cancel):
            return


def start_drive(waypoints, z):
    """Cancel any in-flight walk and start a fresh one in a background thread."""
    ev = G.get("cancel")
    if ev:
        ev.set()
    cancel = threading.Event()
    G["cancel"] = cancel
    threading.Thread(target=drive_path, args=(waypoints, z, cancel), daemon=True).start()


def start_spin(target_yaw, z):
    """Cancel any in-flight motion and spin r1 in place to target_yaw."""
    ev = G.get("cancel")
    if ev:
        ev.set()
    cancel = threading.Event()
    G["cancel"] = cancel
    threading.Thread(target=spin_in_place, args=(target_yaw, z, cancel), daemon=True).start()


# ── HTTP API ─────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify(ok=True, robot=G["args"].robot, spawned=G.get("spawned", False))


@app.route("/state")
def state():
    # doors alongside the robot: a caller deciding whether it may walk needs both,
    # and asking twice invites them to disagree about the moment.
    return jsonify(ok=True, state=G.get("state"), doors=G.get("door_state", {}))


@app.route("/door_state")
def door_state():
    """{door_name: mode} — 0 closed, 1 moving, 2 open (rmf_door_msgs/DoorMode)."""
    return jsonify(ok=True, doors=G.get("door_state", {}))


@app.route("/close_all", methods=["POST"])
def close_all():
    """Shut every door and lift on this level, and stop holding any open.

    The door_keeper republishes OPEN for anything in held_doors, so clearing that
    first is what makes the close stick rather than being undone a moment later.
    """
    # Plain doors only. A lift's shaft and cabin doors are liblift's, governed by
    # where the cabin is rather than by a request, and pushing 21 names through
    # one loop meant the lift entries ran first and the doors after them never
    # reliably got theirs.
    G.setdefault("held_doors", set()).clear()
    # Only the doors that are actually open, and by the same call `close door`
    # makes for one of them. Blasting CLOSED at all nineteen every 0.3s did not
    # shut the one that was open — the same door closes immediately when it is
    # the only one asked.
    open_now = [n for n, m in (G.get("door_state") or {}).items()
                if m != 0 and not _lift_of(n)]

    def _shut():
        """One door at a time, spaced, and re-asked until it reports closed.

        Sending CLOSED to every door at once shut none of them; sending it to the
        two that were open shut one. They cannot go out back to back — so each
        gets its own request with a gap, and anything still open on the next pass
        is asked again, which is also what makes this correct if a door was
        mid-swing when we looked.
        """
        for _ in range(10):
            still = [n for n in open_now
                     if (G.get("door_state") or {}).get(n, 0) != 0]
            if not still:
                print(f"[galaxea] closed {len(open_now)} door(s)", flush=True)
                return
            for name in still:
                try:
                    _door_req(name, DoorMode.MODE_CLOSED)
                except Exception as e:                # noqa: BLE001
                    print(f"[galaxea] close {name}: {e}", flush=True)
                time.sleep(0.4)
            time.sleep(1.0)
        print(f"[galaxea] still open after close_all: "
              f"{[n for n in open_now if (G.get('door_state') or {}).get(n, 0) != 0]}",
              flush=True)

    threading.Thread(target=_shut, daemon=True).start()
    return jsonify(ok=True, closed=open_now, n=len(open_now))


@app.route("/door", methods=["POST"])
def door():
    """Body: {"waypoints": [[x,y],[x,y]], "mode": "open"|"close"}. Open/close the gz doors
    on that edge, mirroring the dream's `open door` / `close door`. An opened door is HELD
    open (door_keeper republishes) until an explicit close."""
    body = request.get_json(force=True, silent=True) or {}
    wps = body.get("waypoints") or []
    mode = body.get("mode", "open")
    if len(wps) < 2:
        return jsonify(ok=False, error="need 2 waypoints"), 400
    (ax, ay), (bx, by) = (float(wps[0][0]), float(wps[0][1])), (float(wps[1][0]), float(wps[1][1]))
    names = door_on_edge(ax, ay, bx, by)
    for nm in names:                                   # new command -> fresh lift session
        lo = _lift_of(nm)
        if lo:
            _lift_session(lo[0], fresh=True)
    held = G.setdefault("held_doors", set())
    if mode == "close":
        held.difference_update(names)
        close_doors(names)
    else:
        # A lift shaft door can only be opened if the cabin is AT this level (else opening
        # it would summon the lift). Refuse otherwise — `call_lift` first.
        for name in names:
            lift = _lift_of(name)
            if lift:
                floor = G.get("lifts", {}).get(lift, {}).get("floor")
                if floor and floor != G["args"].level:
                    return jsonify(ok=False, doors=names,
                                   error=f"{lift} is at {floor}, not {G['args'].level}"), 409
        held.update(names)
        open_doors(names)
    return jsonify(ok=True, doors=names, mode=mode)


@app.route("/reset", methods=["POST"])
def reset():
    """Body: {"level","x","y","yaw"}. Re-sync the robot to the dashboard's level + start
    (the bridge keeps state across dashboard restarts — e.g. a lift ride can leave it on
    another floor). Re-levels the bridge and places the robot at (x,y) on `level`."""
    body = request.get_json(force=True, silent=True) or {}
    level, x, y = body.get("level"), body.get("x"), body.get("y")
    if not level or x is None or y is None:
        return jsonify(ok=False, error="need level + x + y"), 400
    z = level_elevation(G["args"].building, level)
    G["z"], G["args"].level = z, level
    if G["args"].world_file:
        G["doors"] = parse_doors(G["args"].world_file, z)
    ev = G.get("cancel")
    if ev:
        ev.set()                                      # stop any in-flight motion
    set_pose(float(x), float(y), z, float(body.get("yaw", 0.0)))
    print(f"[galaxea] reset robot to {level} @ ({float(x):.1f},{float(y):.1f})", flush=True)
    return jsonify(ok=True, level=level, z=z)


@app.route("/lift_state")
def lift_state():
    return jsonify(ok=True, lifts=G.get("lifts", {}))


@app.route("/interactables")
def interactables():
    """The full vertex -> [items] record (same as the /interactable_items topic)."""
    return jsonify(ok=True, interactables=G.get("interactables", {}))


@app.route("/inventory")
def inventory():
    """Items the robot is currently in inventory (same as the /robot_inventory topic)."""
    return jsonify(ok=True, inventory=G.get("inventory", []))


@app.route("/pick", methods=["POST"])
def pick():
    """Body: {"item": "apple", "vertex": "apex_lab"}. Pick up an item IFF it is interactable at
    that vertex: the item MOVES from the vertex into the inventory (both /interactable_items and
    /robot_inventory republished). `drop: true` just discards it from the inventory."""
    body = request.get_json(force=True, silent=True) or {}
    item, vertex, drop = body.get("item"), body.get("vertex"), bool(body.get("drop"))
    if not item:
        return jsonify(ok=False, error="need item"), 400
    inv = G.setdefault("inventory", [])
    inter = G.setdefault("interactables", {})
    if drop:
        if item in inv:
            inv.remove(item)
        publish_inventory()
        return jsonify(ok=True, inventory=inv, action="drop")
    here = inter.get(vertex or "", [])
    if item not in here:
        return jsonify(ok=False, error=f"'{item}' is not interactable at {vertex!r}",
                       interactable_here=here), 409
    here.remove(item)                                  # item leaves the vertex ...
    if item not in inv:
        inv.append(item)                              # ... and enters the inventory
    publish_items(); publish_inventory()
    return jsonify(ok=True, inventory=inv, interactable_here=here, action="pick")


@app.route("/place", methods=["POST"])
def place():
    """Body: {"item": "apple", "vertex": "apex_lab"}. Reverse of pick: an item in the inventory is
    placed at the current vertex — it leaves the inventory and becomes interactable there."""
    body = request.get_json(force=True, silent=True) or {}
    item, vertex = body.get("item"), body.get("vertex")
    if not item:
        return jsonify(ok=False, error="need item"), 400
    inv = G.setdefault("inventory", [])
    if item not in inv:
        return jsonify(ok=False, error=f"'{item}' is not in the inventory", inventory=inv), 409
    inv.remove(item)                                  # item leaves the inventory ...
    inter = G.setdefault("interactables", {})
    here = inter.setdefault(vertex or "", [])
    if item not in here:
        here.append(item)                              # ... and becomes interactable here
    publish_items(); publish_inventory()
    return jsonify(ok=True, inventory=inv, interactable_here=here, action="place")


@app.route("/call_lift", methods=["POST"])
def call_lift():
    """Body: {"lift": "lift1", "floor": "L11", "ride": bool}. Bring the cabin to `floor`
    (doors closed). `ride` = the robot is inside, so it travels with the cabin."""
    body = request.get_json(force=True, silent=True) or {}
    lift, floor = body.get("lift"), body.get("floor")
    if not lift or not floor:
        return jsonify(ok=False, error="need lift + floor"), 400
    # stop holding this lift's shaft doors open, else the keeper keeps re-summoning it to
    # the old floor (DOOR_OPEN there) and it bounces back.
    held = G.get("held_doors") or set()
    for d in [x for x in held if (_lift_of(x) or (None,))[0] == lift]:
        held.discard(d)
    threading.Thread(target=call_lift_worker, args=(lift, floor, bool(body.get("ride"))),
                     daemon=True).start()
    return jsonify(ok=True, lift=lift, floor=floor)


@app.route("/turn", methods=["POST"])
def turn():
    """Body: {"yaw": <radians>}. Spin r1 in place to face `yaw` (mirrors a dream turn)."""
    body = request.get_json(force=True, silent=True) or {}
    try:
        yaw = float(body["yaw"])
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, error="need yaw (radians)"), 400
    start_spin(yaw, G["z"])
    return jsonify(ok=True, yaw=yaw)


@app.route("/goto", methods=["POST"])
def goto():
    """Body: {"waypoints": [[x,y], ...], "level": "L1", "task_id": "..."}.
    Drives r1 along the given nav-graph waypoints (the dream walker's own path)."""
    body = request.get_json(force=True, silent=True) or {}
    wps = body.get("waypoints") or []
    level = body.get("level") or G["args"].level
    if len(wps) < 1:
        return jsonify(ok=False, error="no waypoints"), 400
    task_id = body.get("task_id") or f"dream_{int(time.time() * 1000)}"
    pts = [(float(w[0]), float(w[1])) for w in wps]
    try:
        # Movement does NOT open doors — doors are obstacles, opened only by `open door`
        # (held_doors). The robot only ever walks a leg whose door was already opened.
        start_drive(pts, G["z"])
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
    return jsonify(ok=True, task_id=task_id, n=len(wps))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--nav", required=True)
    ap.add_argument("--building", required=True, help="building.yaml (for level elevation)")
    ap.add_argument("--level", required=True)
    ap.add_argument("--start", default="0", help="spawn waypoint (name or index)")
    ap.add_argument("--world", default="sim_world")
    ap.add_argument("--world-file", default="", help="the .world file (to read door poses)")
    ap.add_argument("--sdf", required=True)
    ap.add_argument("--robot", default="r1")
    ap.add_argument("--fleet", default="GalaxeaR1")
    ap.add_argument("--interactables", default="", help="JSON: {vertex_name: [item, ...]}")
    ap.add_argument("--port", type=int, default=8090)
    a = ap.parse_args()
    G["args"] = a
    G["inventory"] = []
    G["interactables"] = {}
    if a.interactables and os.path.isfile(a.interactables):
        try:
            G["interactables"] = json.load(open(a.interactables))
            print(f"[galaxea] interactable items at {len(G['interactables'])} vertex(es)", flush=True)
        except (OSError, ValueError) as e:
            print(f"[galaxea] could not read {a.interactables}: {e}", flush=True)

    verts, adj = load_level(a.nav, a.level)
    si = find_start(verts, a.start)
    _, sx, sy = verts[si]
    z = level_elevation(a.building, a.level)
    # Face the first neighbour so the robot spawns looking down a corridor.
    yaw = 0.0
    if adj.get(si):
        _, nx, ny = verts[adj[si][0]]
        yaw = math.atan2(ny - sy, nx - sx)
    G["z"], G["cur_yaw"] = z, yaw

    # Spawn happens in a thread (it blocks up to 2 min waiting for the world); the
    # ROS node + HTTP server come up immediately so /health is answerable meanwhile.
    def _spawn():
        G["spawned"] = spawn_robot(a.world, a.sdf, a.robot, sx, sy, z, yaw)
    threading.Thread(target=_spawn, daemon=True).start()

    if a.world_file:
        G["doors"] = parse_doors(a.world_file, z)
        print(f"[galaxea] {len(G['doors'])} doors on level {a.level}", flush=True)

    rclpy.init()
    node = Node("galaxea_bridge")
    G["node"] = node
    G["door_pub"] = node.create_publisher(DoorRequest, "/door_requests", 10)
    G["lift_pub"] = node.create_publisher(LiftRequest, "/lift_requests", 10)
    G["pose_cli"] = node.create_client(SetEntityPose, "/world/sim_world/set_pose")
    # latched (transient-local) so late subscribers get the current record/inventory immediately
    latched = QoSProfile(depth=1); latched.durability = QoSDurabilityPolicy.TRANSIENT_LOCAL
    G["items_pub"] = node.create_publisher(String, "/interactable_items", latched)
    G["inventory_pub"] = node.create_publisher(String, "/robot_inventory", latched)
    node.create_subscription(RobotState, "/robot_state", _state_cb, 10)
    node.create_subscription(LiftState, "/lift_states", _lift_cb, 10)
    node.create_subscription(DoorState, "/door_states", _door_cb, 10)
    publish_items(); publish_inventory()                    # seed the latched topics
    threading.Thread(target=lambda: rclpy.spin(node), daemon=True).start()
    threading.Thread(target=door_keeper, daemon=True).start()
    threading.Thread(target=close_all_startup, daemon=True).start()

    print(f"[galaxea] bridge on :{a.port} — spawn={a.robot}@{a.start} "
          f"level={a.level} z={z}", flush=True)
    app.run(host="0.0.0.0", port=a.port, threaded=True)


if __name__ == "__main__":
    main()
