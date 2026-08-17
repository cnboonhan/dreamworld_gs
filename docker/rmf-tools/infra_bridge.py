#!/usr/bin/env python3
"""infra bridge — the building's levers over HTTP, and nothing else.

robot_bridge.py distilled for the v2 split: RMF owns the building's
INFRASTRUCTURE — doors, lifts, interactable items — while the robot's
walkthrough lives in the dreamworld viewer, driven through dreamworld_core.
So everything robot-shaped is gone (no spawn, no goto, no set_pose) and
everything else is kept verbatim: the same door-on-edge geometry, the same
lift session discipline, the same held-doors keeper and the same HTTP
routes, so the harness ported from main speaks to this exactly as it spoke
to the galaxea bridge.

    GET  /health /door_state /lift_state /interactables /inventory
    POST /door /call_lift /close_all /reset /pick /place

Runs inside the sim container, where the ROS graph lives.
"""
import argparse
import json
import math
import os
import re
import threading
import time

import rclpy
import yaml
from flask import Flask, jsonify, request
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile
from rmf_door_msgs.msg import DoorMode, DoorRequest, DoorState
from rmf_lift_msgs.msg import LiftRequest, LiftState
from std_msgs.msg import String

app = Flask(__name__)
G = {}


# ── doors from the world file (robot_bridge's, verbatim) ──────────────────────
def parse_doors(world_file, elevation, ztol=1.0):
    """[(name, (x1,y1), (x2,y2))] for doors near this level's elevation."""
    import xml.etree.ElementTree as ET
    doors = []
    try:
        root = ET.parse(world_file).getroot()
    except (OSError, ET.ParseError):
        return doors
    for model in root.iter("model"):
        name = model.get("name") or ""
        pose = model.find("pose")
        if pose is None or not ("door" in name.lower() or "Door" in name):
            continue
        vals = [float(v) for v in (pose.text or "").split()]
        if len(vals) < 6 or abs(vals[2] - elevation) > ztol + 3.0:
            continue
        x, y, yaw = vals[0], vals[1], vals[5]
        half = 0.9
        dx, dy = math.cos(yaw) * half, math.sin(yaw) * half
        doors.append((name, (x - dx, y - dy), (x + dx, y + dy)))
    return doors


def _seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    L2 = vx * vx + vy * vy
    if L2 < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / L2))
    return math.hypot(px - ax - t * vx, py - ay - t * vy)


def door_on_edge(ax, ay, bx, by, thresh=2.0):
    """Names of the doors whose segment sits on the edge (a,b)."""
    hits = []
    for name, (x1, y1), (x2, y2) in G.get("doors", []):
        mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        if _seg_dist(mx, my, ax, ay, bx, by) <= thresh:
            hits.append(name)
    return hits


# ── lifts (robot_bridge's, verbatim) ──────────────────────────────────────────
def _lift_of(door_name):
    """'ShaftDoor_lift1_L11_name' -> ('lift1', 'L11'); None for a regular door."""
    m = re.match(r"(?:Shaft|Cabin)Door_(lift\d+)_([A-Za-z0-9]+)_", door_name)
    return (m.group(1), m.group(2)) if m else None


def _lift_session(lift, fresh=False):
    s = G.setdefault("lift_sessions", {})
    if fresh or lift not in s:
        s[lift] = f"infra_{lift}_{int(time.time() * 1000)}"
    return s[lift]


def _lift_request(lift, floor, door_open, session=None):
    req = LiftRequest()
    req.lift_name = lift
    req.request_time = G["node"].get_clock().now().to_msg()
    req.session_id = session or _lift_session(lift)
    req.request_type = LiftRequest.REQUEST_AGV_MODE
    req.destination_floor = floor
    req.door_state = (LiftRequest.DOOR_OPEN if door_open
                      else LiftRequest.DOOR_CLOSED)
    G["lift_pub"].publish(req)


def _door_req(name, mode):
    lift = _lift_of(name)
    if lift:
        _lift_request(lift[0], lift[1], mode == DoorMode.MODE_OPEN)
    else:
        req = DoorRequest()
        req.request_time = G["node"].get_clock().now().to_msg()
        req.requester_id = "infra_bridge"
        req.door_name = name
        req.requested_mode.value = mode
        G["door_pub"].publish(req)


def open_doors(names):
    for name in names:
        _door_req(name, DoorMode.MODE_OPEN)


def close_doors(names):
    for name in names:
        try:
            _door_req(name, DoorMode.MODE_CLOSED)
        except Exception as e:                        # noqa: BLE001
            print(f"[infra] close {name}: {e}", flush=True)


def close_all_startup():
    time.sleep(5)
    names = [n for n, _, _ in G.get("doors", [])]
    if not names:
        return
    for _ in range(24):
        if G.get("held_doors"):
            break
        close_doors(names)
        time.sleep(0.3)
    print(f"[infra] closed {len(names)} doors/lifts at startup", flush=True)


def door_keeper():
    while True:
        time.sleep(0.3)
        held = G.get("held_doors") or set()
        if held:
            open_doors(list(held))


def call_lift_worker(lift, floor):
    """Re-assert a LiftRequest until the cabin arrives at `floor` and stops."""
    session = _lift_session(lift, fresh=True)
    t0 = time.time()
    for _ in range(200):
        _lift_request(lift, floor, door_open=False, session=session)
        st = G.get("lifts", {}).get(lift, {})
        if st.get("floor") == floor and st.get("motion") == 0 \
                and time.time() - t0 > 2:
            print(f"[infra] {lift} arrived at {floor}", flush=True)
            return
        time.sleep(0.3)


def level_elevation(building_yaml, level):
    try:
        doc = yaml.safe_load(open(building_yaml))
        return float(doc["levels"][level].get("elevation", 0.0))
    except (OSError, KeyError, ValueError, TypeError):
        return 0.0


# ── ROS callbacks + latched publications ──────────────────────────────────────
def _door_cb(msg: DoorState):
    G.setdefault("door_state", {})[msg.door_name] = int(msg.current_mode.value)


def _lift_cb(msg: LiftState):
    G.setdefault("lifts", {})[msg.lift_name] = {
        "floor": msg.current_floor, "door": int(msg.door_state),
        "motion": int(msg.motion_state)}


def publish_items():
    if G.get("items_pub"):
        G["items_pub"].publish(String(data=json.dumps(
            G.get("interactables", {}))))


def publish_inventory():
    if G.get("inventory_pub"):
        G["inventory_pub"].publish(String(data=json.dumps(
            G.get("inventory", []))))


# ── HTTP ───────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify(ok=True, doors=len(G.get("doors", [])),
                   level=G["args"].level)


@app.route("/state")
def state():
    """Kept for callers of the old bridge: doors only — the walker's state
    lives at dreamworld_core, not here."""
    return jsonify(ok=True, state={}, doors=G.get("door_state", {}))


@app.route("/door_state")
def door_state():
    return jsonify(ok=True, doors=G.get("door_state", {}))


@app.route("/lift_state")
def lift_state():
    return jsonify(ok=True, lifts=G.get("lifts", {}))


@app.route("/door", methods=["POST"])
def door():
    """Body: {"waypoints": [[x,y],[x,y]], "mode": "open"|"close"} — the gz
    doors on that edge. An opened door is HELD open until an explicit close."""
    body = request.get_json(force=True, silent=True) or {}
    wps = body.get("waypoints") or []
    mode = body.get("mode", "open")
    if len(wps) < 2:
        return jsonify(ok=False, error="need 2 waypoints"), 400
    (ax, ay), (bx, by) = ((float(wps[0][0]), float(wps[0][1])),
                          (float(wps[1][0]), float(wps[1][1])))
    names = door_on_edge(ax, ay, bx, by)
    for nm in names:
        lo = _lift_of(nm)
        if lo:
            _lift_session(lo[0], fresh=True)
    held = G.setdefault("held_doors", set())
    if mode == "close":
        held.difference_update(names)
        close_doors(names)
    else:
        for name in names:
            lift = _lift_of(name)
            if lift:
                floor = G.get("lifts", {}).get(lift[0], {}).get("floor")
                if floor and floor != G["args"].level:
                    return jsonify(ok=False, doors=names,
                                   error=f"{lift[0]} is at {floor}, not "
                                         f"{G['args'].level}"), 409
        held.update(names)
        open_doors(names)
    return jsonify(ok=True, doors=names, mode=mode)


@app.route("/close_all", methods=["POST"])
def close_all():
    G.setdefault("held_doors", set()).clear()
    open_now = [n for n, m in (G.get("door_state") or {}).items()
                if m != 0 and not _lift_of(n)]

    def _shut():
        for _ in range(10):
            still = [n for n in open_now
                     if (G.get("door_state") or {}).get(n, 0) != 0]
            if not still:
                print(f"[infra] closed {len(open_now)} door(s)", flush=True)
                return
            for name in still:
                try:
                    _door_req(name, DoorMode.MODE_CLOSED)
                except Exception as e:                # noqa: BLE001
                    print(f"[infra] close {name}: {e}", flush=True)
                time.sleep(0.4)
            time.sleep(1.0)

    threading.Thread(target=_shut, daemon=True).start()
    return jsonify(ok=True, closed=open_now, n=len(open_now))


@app.route("/call_lift", methods=["POST"])
def call_lift():
    """Body: {"lift": "lift1", "floor": "L11"} — bring the cabin to `floor`
    (doors closed while travelling). There is no robot to ride it; the
    walker rides in the dreamworld viewer."""
    body = request.get_json(force=True, silent=True) or {}
    lift, floor = body.get("lift"), body.get("floor")
    if not lift or not floor:
        return jsonify(ok=False, error="need lift + floor"), 400
    held = G.get("held_doors") or set()
    for d in [x for x in held if (_lift_of(x) or (None,))[0] == lift]:
        held.discard(d)
    threading.Thread(target=call_lift_worker, args=(lift, floor),
                     daemon=True).start()
    return jsonify(ok=True, lift=lift, floor=floor)


@app.route("/reset", methods=["POST"])
def reset():
    """Body: {"level"} — re-level the bridge (which doors it knows) after a
    lift ride. Nothing to place: there is no robot here."""
    body = request.get_json(force=True, silent=True) or {}
    level = body.get("level")
    if not level:
        return jsonify(ok=False, error="need level"), 400
    G["args"].level = level
    z = level_elevation(G["args"].building, level)
    if G["args"].world_file:
        G["doors"] = parse_doors(G["args"].world_file, z)
    return jsonify(ok=True, level=level, doors=len(G.get("doors", [])))


@app.route("/interactables")
def interactables():
    return jsonify(ok=True, interactables=G.get("interactables", {}))


@app.route("/inventory")
def inventory():
    return jsonify(ok=True, inventory=G.get("inventory", []))


@app.route("/pick", methods=["POST"])
def pick():
    body = request.get_json(force=True, silent=True) or {}
    item, vertex = body.get("item"), body.get("vertex")
    drop = bool(body.get("drop"))
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
        return jsonify(ok=False,
                       error=f"'{item}' is not interactable at {vertex!r}",
                       interactable_here=here), 409
    here.remove(item)
    if item not in inv:
        inv.append(item)
    publish_items()
    publish_inventory()
    return jsonify(ok=True, inventory=inv, interactable_here=here,
                   action="pick")


@app.route("/place", methods=["POST"])
def place():
    body = request.get_json(force=True, silent=True) or {}
    item, vertex = body.get("item"), body.get("vertex")
    if not item:
        return jsonify(ok=False, error="need item"), 400
    inv = G.setdefault("inventory", [])
    if item not in inv:
        return jsonify(ok=False, error=f"'{item}' is not in the inventory",
                       inventory=inv), 409
    inv.remove(item)
    inter = G.setdefault("interactables", {})
    here = inter.setdefault(vertex or "", [])
    if item not in here:
        here.append(item)
    publish_items()
    publish_inventory()
    return jsonify(ok=True, inventory=inv, interactable_here=here,
                   action="place")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--building", required=True)
    ap.add_argument("--level", required=True)
    ap.add_argument("--world-file", default="")
    ap.add_argument("--interactables", default="")
    ap.add_argument("--port", type=int, default=8090)
    a = ap.parse_args()
    G["args"] = a
    G["inventory"] = []
    G["interactables"] = {}
    if a.interactables and os.path.isfile(a.interactables):
        try:
            G["interactables"] = json.load(open(a.interactables))
            print(f"[infra] interactable items at "
                  f"{len(G['interactables'])} vertex(es)", flush=True)
        except (OSError, ValueError) as e:
            print(f"[infra] could not read {a.interactables}: {e}",
                  flush=True)
    z = level_elevation(a.building, a.level)
    if a.world_file:
        G["doors"] = parse_doors(a.world_file, z)
        print(f"[infra] {len(G['doors'])} doors on level {a.level}",
              flush=True)

    rclpy.init()
    node = Node("infra_bridge")
    G["node"] = node
    G["door_pub"] = node.create_publisher(DoorRequest, "/door_requests", 10)
    G["lift_pub"] = node.create_publisher(LiftRequest, "/lift_requests", 10)
    latched = QoSProfile(depth=1)
    latched.durability = QoSDurabilityPolicy.TRANSIENT_LOCAL
    G["items_pub"] = node.create_publisher(String, "/interactable_items",
                                           latched)
    G["inventory_pub"] = node.create_publisher(String, "/robot_inventory",
                                               latched)
    node.create_subscription(LiftState, "/lift_states", _lift_cb, 10)
    node.create_subscription(DoorState, "/door_states", _door_cb, 10)
    publish_items()
    publish_inventory()
    threading.Thread(target=lambda: rclpy.spin(node), daemon=True).start()
    threading.Thread(target=door_keeper, daemon=True).start()
    threading.Thread(target=close_all_startup, daemon=True).start()

    print(f"[infra] bridge on :{a.port} — level={a.level}", flush=True)
    app.run(host="0.0.0.0", port=a.port, threaded=True)


if __name__ == "__main__":
    main()
