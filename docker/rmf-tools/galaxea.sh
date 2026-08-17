#!/bin/bash
# galaxea — spawn the Galaxea R1 into the sim you can see, and serve the bridge
# that drives it (robot_bridge.py on :8090).
#
# Ported from dreamworld/docker/dream_interactive/run.sh, which booted a Gazebo of
# its own on its own transport partition. That made sense there — the dream
# pipeline's renders boot their own `sim_world` and must never see the robot — but
# here it meant two Gazebos of one building, and the robot standing in the one
# nobody was looking at.
#
# So this runs in the rmfsim container's network namespace and joins ITS gazebo:
# the same partition, the same loopback, the robot in the window at :8083. What
# rmfsim already provides — the world, /clock, building_map_server — is not
# started again. What it does not is the set_pose service bridge, which is how the
# robot is driven, so that is started here.
#
# The robot is spawned at RUNTIME (gz `create` service) rather than baked into the
# .world, so anything else that boots this world still never sees it.
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
SDF="${proj}/sim_assets/GalaxeaR1/model_static.sdf"

for f in "${gen}/${WORLD}" "${gen}/nav_graphs/0.yaml" "${BUILDING}"; do
    if [ ! -f "$f" ]; then
        echo "missing ${f} — run: just world" >&2
        exit 1
    fi
done
if [ ! -f "$SDF" ]; then
    echo "missing ${SDF}" >&2
    echo "the Galaxea R1 model is not in this project — copy GalaxeaR1/ into" >&2
    echo "assets/projects/${PROJECT}/sim_assets/ (~32 MB of meshes, not in git)" >&2
    exit 1
fi

# sim_assets/ holds the GalaxeaR1 model dir so `model://GalaxeaR1/...` mesh
# URIs resolve; the world's own models sit in the generated worlds/ tree.
# The model dir has to be on the path of whoever SPAWNS it — that is this
# process, calling gz service create.
export GZ_SIM_RESOURCE_PATH="${proj}/sim_assets:${proj}:${gen}:${gen}/models"
# rmfsim's partition, not one of our own: we are joining its gazebo, not running
# one. robot_bridge's `gz service` calls inherit this, so they reach it.
export GZ_PARTITION="${GZ_PARTITION:-dreamworld_rmf}"

# Wait for rmfsim's world rather than assuming it: this container starts beside
# it, not after it, and a create call into nothing fails silently enough to be
# confusing.
echo "waiting for the ${WORLD%.world} sim to come up..."
for _ in $(seq 1 120); do
    gz service -s "/world/sim_world/create" --info >/dev/null 2>&1 && break
    sleep 2
done

# Only the piece rmfsim does not already run. It bridges /clock and runs
# building_map_server; it does not bridge set_pose, which is how the robot is
# driven along a nav path.
ros2 run ros_gz_bridge parameter_bridge \
    "/world/sim_world/set_pose@ros_gz_interfaces/srv/SetEntityPose" &
BRIDGE_PID=$!

trap 'kill ${BRIDGE_PID} 2>/dev/null || true' EXIT

exec python3 /app/robot_bridge.py \
    --nav "${gen}/nav_graphs/0.yaml" --building "${BUILDING}" \
    --level "${LEVEL}" --start "${START}" --world sim_world \
    --world-file "${gen}/${WORLD}" \
    --interactables "${proj}/interactable_items.json" \
    --sdf "${SDF}" --port "${PORT}"
