#!/bin/bash
# Boot a headless sim on the project's world and photograph one corridor.
#
#   capture.sh <project> <map> <edge-id> [spacing-metres]
#     writes /projects/<project>/panos/<edge-id>/000.png ...
#
# Its own Gazebo, separate from the long-running rmfsim service: a capture
# teleports the camera around, which would be visible — and disruptive — in the
# simulation someone is watching. GZ_PARTITION keeps the two from discovering
# each other.
#
# No `set -u`: sourcing the ROS setup.bash trips on an unbound COLCON_TRACE.
set -eo pipefail

. /rmf_demos_ws/install/setup.bash

PROJECT="${1:?usage: capture.sh <project> <map> <edge-id> [standpoints]}"
MAP="${2:?need a map}"
EDGE="${3:?need an edge id}"
SPACING="${4:-0.5}"

world_dir="/projects/${PROJECT}/worlds/${MAP}"
world="${world_dir}/${MAP}.world"
plan="${world_dir}/capture_plan.json"
out="/projects/${PROJECT}/panos/${EDGE}"

for f in "$world" "$plan"; do
    [ -f "$f" ] || { echo "missing $f — run: just world" >&2; exit 1; }
done

# Isolate this capture completely, so several can run at once.
#
# A capture is a whole Gazebo plus a ROS bridge, and both used fixed names: one
# partition and one set of topics. Two at a time would have discovered each
# other and interleaved frames from different corridors into the same
# panorama. Keying both on the edge gives each run its own bus at both layers.
SLOT=$(printf '%s' "$EDGE" | cksum | cut -d' ' -f1)
export GZ_SIM_RESOURCE_PATH="/projects/${PROJECT}:/projects/${PROJECT}/maps:${world_dir}:${world_dir}/models"
export GZ_PARTITION="dreamworld_capture_${SLOT}"
export GZ_IP=127.0.0.1
# ROS_DOMAIN_ID is what keeps two bridges from publishing onto one /rec_cam.
# 1-101 is the range every DDS vendor supports.
export ROS_DOMAIN_ID=$(( SLOT % 101 + 1 ))

# A ceiling, because these worlds are open-topped: without one every panorama
# shows open sky overhead, which no indoor capture ever would. Sits at the
# level's floor plus 2.45 m.
elev=$(python3 - "$plan" "$EDGE" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1]))
for name, data in plan["levels"].items():
    if any(e["id"] == sys.argv[2] for e in data["edges"]):
        print(name)
        break
PY
)
elevation=$(python3 - "/projects/${PROJECT}/maps/${MAP}.building.yaml" "$elev" <<'PY'
import sys, yaml
try:
    d = yaml.safe_load(open(sys.argv[1]))
    print(float((d.get("levels") or {}).get(sys.argv[2], {}).get("elevation", 0)))
except Exception:
    print(0.0)
PY
)
ceil_z=$(awk "BEGIN{print ${elevation}+2.45}")
ceil_world="/tmp/${MAP}_${SLOT}_ceil.world"
# Whatever this lane passes through is opened by taking it out of the world.
# A closed door is a wall, and a corridor photographed from one side of one is
# a dead end: the generator has nothing to put behind it, so the walk goes
# through the leaf into nothing. RMF opens every one of them for anything
# traversing the lane — closed is the state no traversal ever sees. Only what
# this lane crosses is opened; doors elsewhere in shot stay shut, because they
# are.
#
# Two kinds. Five lanes cross an ordinary door, which the capture plan names.
# Four more end inside a lift car, which it does not: RMF models a lift apart
# from its doors, so those lanes carry no door at all and were photographed
# facing shut lift doors — 82 and 89 per cent blank wall. There the shaft door
# and the cabin door both open, and the car is parked at this level first,
# because an open shaft door on a floor the car has left is a hole.
python3 - "$world" "$ceil_world" "$ceil_z" "$plan" "$EDGE" "$elev" "$elevation" <<'PY'
import json
import math
import sys
import xml.etree.ElementTree as ET

src, dst, cz, plan_path, edge_id, level, elevation = sys.argv[1:8]
plan = json.load(open(plan_path))
door, ends = "", []
for data in plan["levels"].values():
    pos = {v["id"]: (v["x"], v["y"]) for v in data["vertices"]}
    for e in data["edges"]:
        if e["id"] == edge_id:
            door = e.get("door") or ""
            ends = [pos[e["a"]], pos[e["b"]]]

tree = ET.parse(src)
world = tree.getroot().find("world")


def open_leaves(parent, what):
    """Take out a door's two leaves, leaving its frame, ramp and car behind.

    A cabin door is a model nested inside its lift, a shaft door's leaves are
    links of its own model, so this looks for both rather than assuming.
    """
    gone = [el for el in parent.iter()
            if el.tag in ("link", "model") and el.get("name") in ("left_door", "right_door")]
    owners = {c: p for p in parent.iter() for c in p}
    for el in gone:
        owners[el].remove(el)
    print(f"opened {what}" if gone else f"nothing to open on {what}")


if door:
    dead = [m for m in world.findall("model") if m.get("name") == door]
    for m in dead:
        world.remove(m)
    print(f"opened {door}" if dead else f"no model named {door} to open")

# A lane that ends in a lift car is a lane into the lift. Lifts are the models
# that have a cabin to move, not the ones whose name happens to start "lift" —
# that matched lift_lobby_south_door, which is a door in a lobby and nothing to
# do with a lift.
for lift in [m for m in world.findall("model")
             if any(j.get("name") == "cabin_joint" for j in m.iter("joint"))]:
    pose = lift.find("pose")
    if pose is None:
        continue
    lx, ly, _ = (float(v) for v in pose.text.split()[:3])
    if not any(math.dist(end, (lx, ly)) < 1.0 for end in ends):
        continue
    rest = pose.text.split()[3:]
    pose.text = " ".join([f"{lx}", f"{ly}", elevation] + rest)
    print(f"brought {lift.get('name')} to {level} at z={elevation}")
    open_leaves(lift, f"{lift.get('name')} cabin door")
    shaft = f"ShaftDoor_{lift.get('name')}_{level}_name"
    for m in world.findall("model"):
        if m.get("name") == shaft:
            open_leaves(m, shaft)

ceiling = ET.fromstring(
    f'<model name="injected_ceiling"><static>true</static>'
    f'<pose>0 0 {float(cz)} 0 0 0</pose><link name="link"><visual name="visual">'
    '<geometry><box><size>300 300 0.1</size></box></geometry><material>'
    '<ambient>0.5 0.5 0.5 1</ambient><diffuse>0.5 0.5 0.5 1</diffuse>'
    '<specular>0.1 0.1 0.1 1</specular></material></visual></link></model>')
world.append(ceiling)
tree.write(dst)
PY

# Both of these get their own log, named for this capture, rather than this
# script's stdout. `gz sim` is a ruby wrapper that execs a *child* gz, so
# killing the pid we know about leaves the real one alive holding the pipe —
# and a caller reading our output then waits forever for an EOF that never
# comes. Redirecting means the pipe closes when this script does, whatever
# survives; naming them per slot means concurrent captures do not overwrite
# each other's evidence.
# Job control, so each child below lands in its own process group and can be
# killed as a tree. Both are launchers that exec a *child* — `gz sim` through
# ruby, `ros2 run` through the binary under lib/ — so killing the pid we know
# about leaves the real process running. Killing by name instead would now
# take out every other capture on the box, which is the whole point of the
# isolation above.
set -m

gz sim -s -r --headless-rendering "$ceil_world" >"/tmp/gz.${SLOT}.log" 2>&1 &
GZ_PID=$!
ros2 run ros_gz_bridge parameter_bridge \
    "/rec_cam@sensor_msgs/msg/Image[gz.msgs.Image" \
    "/rec_depth@sensor_msgs/msg/Image[gz.msgs.Image" \
    "/world/sim_world/set_pose@ros_gz_interfaces/srv/SetEntityPose" \
    >"/tmp/bridge.${SLOT}.log" 2>&1 &
BRIDGE_PID=$!

cleanup() {
    # Each process group, not each pid: that reaches the exec'd child without
    # touching anybody else's capture. A leaked bridge is not harmless — nine
    # of them once accumulated over a batch, each spinning on a CPU, and
    # starved the run that followed until it made two panoramas in six hours.
    kill -- "-${BRIDGE_PID}" "-${GZ_PID}" 2>/dev/null || true
    sleep 0.5
    kill -9 -- "-${BRIDGE_PID}" "-${GZ_PID}" 2>/dev/null || true
    rm -f "${ceil_world}" "/tmp/gz.${SLOT}.log" "/tmp/bridge.${SLOT}.log"
}
trap cleanup EXIT

cam_z=$(awk "BEGIN{print ${elevation}+1.6}")
python3 /app/capture.py --plan "$plan" --edge "$EDGE" --out-dir "$out" \
    --spacing "$SPACING" --height "$cam_z" --fov 2.2
