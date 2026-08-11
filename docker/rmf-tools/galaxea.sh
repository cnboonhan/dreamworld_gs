#!/bin/bash
# galaxea — boot a Gazebo on the project's world, spawn the Galaxea R1 into it, and
# serve the bridge that drives it (robot_bridge.py on :8090).
#
# Ported from dreamworld/docker/dream_interactive/run.sh. The differences are paths
# and one image: this repo lays a project out as assets/projects/<p>/worlds/<map>/
# rather than <p>/outputs/generate_gz/, and rmf-tools already carries Gazebo, the
# ros_gz bridge, building_map_server and every RMF message package the bridge
# imports, so there is no second image to build.
#
# The robot is spawned at RUNTIME (gz `create` service) rather than baked into the
# .world, so nothing else that boots this world ever sees it.
#
# Args: galaxea.sh <project> <map> — both resolved by entrypoint.sh.
set -e

PROJECT="${1:?usage: galaxea.sh <project> <map> [level] [start]}"
MAP="${2:?usage: galaxea.sh <project> <map> [level] [start]}"
LEVEL="${3:-${DW_LEVEL:-L11}}"
START="${4:-${DW_START:-lift_lobby}}"
PORT="${DW_GALAXEA_PORT:-8090}"

proj="/projects/${PROJECT}"
gen="${proj}/worlds/${MAP}"
WORLD="${MAP}.world"
BUILDING="${proj}/maps/${MAP}.building.yaml"
SDF="${proj}/GalaxeaR1/model_static.sdf"

for f in "${gen}/${WORLD}" "${gen}/nav_graphs/0.yaml" "${BUILDING}"; do
    if [ ! -f "$f" ]; then
        echo "missing ${f} — run: just world" >&2
        exit 1
    fi
done
if [ ! -f "$SDF" ]; then
    echo "missing ${SDF}" >&2
    echo "the Galaxea R1 model is not in this project — copy GalaxeaR1/ into" >&2
    echo "assets/projects/${PROJECT}/ (it is ~32 MB of meshes, so it is not in git)" >&2
    exit 1
fi

# /projects holds the GalaxeaR1 model dir so `model://GalaxeaR1/...` mesh URIs
# resolve; the world's own models sit beside it.
export GZ_SIM_RESOURCE_PATH="${proj}:${gen}:${gen}/models"
# Isolate transport, so this sim never collides with the one the `sim` role runs on
# the same world. robot_bridge's `gz service` calls inherit this env, so they hit
# THIS gazebo.
export GZ_PARTITION="${GZ_PARTITION:-galaxea}"

# Server-only with offscreen rendering. The sim role already publishes a window over
# noVNC; this one exists to be driven, and headless is what makes the lift kinematics
# step reliably on a box with no usable GL display.
gz sim -s -r --headless-rendering "${gen}/${WORLD}" &
GZ_PID=$!

# Bridge sim /clock into ROS 2 (slotcar runs on sim time and publishes /robot_state)
# and the set_pose service the bridge drives the robot with.
ros2 run ros_gz_bridge parameter_bridge \
    "/clock@rosgraph_msgs/msg/Clock[gz.msgs.Clock" \
    "/world/sim_world/set_pose@ros_gz_interfaces/srv/SetEntityPose" &
BRIDGE_PID=$!

# building_map_server publishes the RMF building map; slotcar needs it to resolve the
# robot's level_name from its x,y,z before it will publish /robot_state.
ros2 run rmf_building_map_tools building_map_server "${BUILDING}" &
MAP_PID=$!

trap 'kill ${MAP_PID} ${BRIDGE_PID} ${GZ_PID} 2>/dev/null || true' EXIT

exec python3 /app/robot_bridge.py \
    --nav "${gen}/nav_graphs/0.yaml" --building "${BUILDING}" \
    --level "${LEVEL}" --start "${START}" --world sim_world \
    --world-file "${gen}/${WORLD}" \
    --interactables "${proj}/interactable_items.json" \
    --sdf "${SDF}" --port "${PORT}"
