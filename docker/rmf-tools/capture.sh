#!/bin/bash
# Boot a headless sim on the project's world and photograph one corridor.
#
#   capture.sh <project> <map> <edge-id> [standpoints]
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
STANDPOINTS="${4:-5}"

world_dir="/projects/${PROJECT}/worlds/${MAP}"
world="${world_dir}/${MAP}.world"
plan="${world_dir}/capture_plan.json"
out="/projects/${PROJECT}/panos/${EDGE}"

for f in "$world" "$plan"; do
    [ -f "$f" ] || { echo "missing $f — run: just world" >&2; exit 1; }
done

export GZ_SIM_RESOURCE_PATH="/projects/${PROJECT}:/projects/${PROJECT}/maps:${world_dir}:${world_dir}/models"
export GZ_PARTITION="dreamworld_capture"
export GZ_IP=127.0.0.1

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
ceil_world="/tmp/${MAP}_ceil.world"
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

# Both of these get their own log rather than this script's stdout. `gz sim` is
# a ruby wrapper that execs a *child* gz, so killing the pid we know about
# leaves the real one alive holding the pipe — and a caller reading our output
# then waits forever for an EOF that never comes. Redirecting means the pipe
# closes when this script does, whatever survives.
gz sim -s -r --headless-rendering "$ceil_world" >/tmp/gz.log 2>&1 &
GZ_PID=$!
ros2 run ros_gz_bridge parameter_bridge \
    "/rec_cam@sensor_msgs/msg/Image[gz.msgs.Image" \
    "/world/sim_world/set_pose@ros_gz_interfaces/srv/SetEntityPose" \
    >/tmp/bridge.log 2>&1 &
BRIDGE_PID=$!

cleanup() {
    # the whole tree, not just the pids we spawned: the wrapper's child would
    # otherwise outlive us and hold a partition open for the next capture
    kill "${BRIDGE_PID}" "${GZ_PID}" 2>/dev/null || true
    pkill -f "gz sim -s -r --headless-rendering ${ceil_world}" 2>/dev/null || true
    pkill -f "ruby.*gz sim" 2>/dev/null || true
}
trap cleanup EXIT

cam_z=$(awk "BEGIN{print ${elevation}+1.6}")
python3 /app/capture.py --plan "$plan" --edge "$EDGE" --out-dir "$out" \
    --standpoints "$STANDPOINTS" --height "$cam_z" --fov 2.2
