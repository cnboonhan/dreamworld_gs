#!/bin/bash
# rmf-tools — one image, three roles.
#
#   entrypoint.sh world  <map>     building.yaml -> world + models + nav graph
#   entrypoint.sh sim    <map>     run it headless under RMF, view over noVNC
#   entrypoint.sh editor <map>     the traffic editor, over noVNC
#
# Mounts: /maps (inputs, read-only for sim/editor is NOT possible — the editor
# writes the map it edits), /worlds (generated output).
#
# No `set -u`: the ROS workspace's setup.bash reads COLCON_TRACE unguarded, so
# nounset makes sourcing it fail before anything here runs.
set -eo pipefail

ROLE="${1:?usage: entrypoint.sh <world|sim|editor> [map]}"
MAP="${2:-${DW_MAP:-office}}"

. /rmf_demos_ws/install/setup.bash

world_dir="/worlds/${MAP}"
building="/maps/${MAP}/${MAP}.building.yaml"

ensure_world() {
    # Generate on demand so `docker compose up` works from a clean checkout
    # rather than failing on a world nobody has built yet.
    if [ ! -f "${world_dir}/${MAP}.world" ]; then
        echo "no world for '${MAP}' yet — generating it first"
        /app/generate_world.sh "$MAP"
    fi
}

case "$ROLE" in

world)
    exec /app/generate_world.sh "$MAP"
    ;;

sim)
    ensure_world
    export GZ_SIM_RESOURCE_PATH="/maps/${MAP}:${world_dir}:${world_dir}/models"
    # Software GL: there is no host X server to borrow a GPU from, and these
    # are floorplan-scale scenes.
    export LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe
    export DW_FIT_WINDOW="Gazebo GUI"
    # The GUI is only a viewer onto the headless sim, so it is started after the
    # server and its death does not take the simulation with it.
    exec /app/with_display.sh bash -c '
        ros2 launch "'"${world_dir}"'/sim.launch.xml" &
        LAUNCH=$!
        trap "kill $LAUNCH 2>/dev/null || true" EXIT
        # let the server advertise before the client looks for it
        for _ in $(seq 1 60); do
            gz topic -l 2>/dev/null | grep -q . && break
            sleep 1
        done
        gz sim -g 2>&1 | sed "s/^/[gui] /" &
        wait $LAUNCH
    '
    ;;

editor)
    if [ ! -f "$building" ]; then
        echo "no such map: $building" >&2
        echo "available in assets/maps:" >&2
        ls /maps 2>/dev/null | sed 's/^/  /' >&2
        exit 1
    fi
    export LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe
    export DW_FIT_WINDOW="traffic"
    # traffic-editor resolves the floorplan image relative to its own cwd
    cd "/maps/${MAP}"
    exec /app/with_display.sh traffic-editor "$building"
    ;;

*)
    echo "unknown role '$ROLE' (want: world, sim, editor)" >&2
    exit 1
    ;;
esac
