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
python3 - "$world" "$ceil_world" "$ceil_z" <<'PY'
import sys
src, dst, cz = sys.argv[1], sys.argv[2], float(sys.argv[3])
xml = open(src).read()
ceiling = (f'<model name="injected_ceiling"><static>true</static>'
           f'<pose>0 0 {cz} 0 0 0</pose><link name="link"><visual name="visual">'
           '<geometry><box><size>300 300 0.1</size></box></geometry><material>'
           '<ambient>0.5 0.5 0.5 1</ambient><diffuse>0.5 0.5 0.5 1</diffuse>'
           '<specular>0.1 0.1 0.1 1</specular></material></visual></link></model>')
open(dst, "w").write(xml.replace("</world>", ceiling + "\n</world>", 1))
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
