#!/bin/bash
# rmf-tools — one image, three roles, one mount.
#
#   entrypoint.sh jobs                     serve build-world to the job queue
#   entrypoint.sh world  <project> [map]   building.yaml -> world + nav graph
#   entrypoint.sh sim    <project> [map]   run it headless under RMF, over noVNC
#   entrypoint.sh editor <project> [map]   the traffic editor, over noVNC
#   entrypoint.sh galaxea <project> [map]  spawn the Galaxea R1 and serve its bridge
#
# assets/projects is mounted at /projects, writable — the editor's whole job is
# to write the map it opens, and the world is generated beside it.
#
# `map` defaults to the project's own name, or to the only map in its maps/.
#
# No `set -u`: the ROS workspace's setup.bash reads COLCON_TRACE unguarded, so
# nounset makes sourcing it fail before anything here runs.
set -eo pipefail

ROLE="${1:?usage: entrypoint.sh <world|sim|editor> [project] [map]}"
PROJECT="${2:-${DW_PROJECT:-multilevel_office}}"
MAP="${3:-${DW_MAP:-}}"

. /rmf_demos_ws/install/setup.bash

maps="/projects/${PROJECT}/maps"

# One map per project is the common case, so name it only when there is a
# choice. `|| true` on every substitution: under `set -e` a failing glob inside
# $(...) kills the shell outright, and the diagnostics below never print.
resolve_map() {
    [ -n "$MAP" ] && return 0
    if [ -f "${maps}/${PROJECT}.building.yaml" ]; then
        MAP="$PROJECT"
        return 0
    fi
    local found
    found=$(find "$maps" -maxdepth 1 -name '*.building.yaml' 2>/dev/null | sort | head -1 || true)
    if [ -z "$found" ]; then
        MAP=""
        return 0
    fi
    MAP=$(basename "$found" .building.yaml)
}

# A project with no map is a normal state (captures first, floorplan later), not
# a crash. Say so once and idle, rather than restart-looping the message away.
idle_without_map() {
    echo
    echo "project '${PROJECT}' has no map yet, so there is nothing to simulate."
    echo "Add one at assets/projects/${PROJECT}/maps/<name>.building.yaml,"
    echo "or point the stack at a project that has one:"
    echo
    for p in /projects/*/; do
        [ -d "$p" ] || continue
        n=$(basename "$p")
        if find "$p/maps" -maxdepth 1 -name '*.building.yaml' 2>/dev/null | grep -q .; then
            echo "    just use $n"
        fi
    done
    echo
    exec sleep infinity
}

case "$ROLE" in

jobs)
    # The normal way world generation happens: submitted, logged and retryable
    # at :4200 like every other operation. `world` below is the same work run
    # directly, for when the job server itself is what's broken.
    cd /app
    exec /opt/prefect/bin/python /app/world_flow.py
    ;;

world)
    exec /app/generate_world.sh "$PROJECT" "$MAP"
    ;;

galaxea)
    # Its own Gazebo on its own transport partition, so it never collides with the
    # `sim` role running the same world.
    resolve_map
    [ -n "$MAP" ] || idle_without_map
    exec /app/galaxea.sh "$PROJECT" "$MAP" "${DW_LEVEL:-L11}" "${DW_START:-lift_lobby}"
    ;;

sim)
    resolve_map
    [ -n "$MAP" ] || idle_without_map
    world_dir="/projects/${PROJECT}/worlds/${MAP}"
    # Generate on demand so `docker compose up` works from a clean checkout
    # rather than failing on a world nobody has built yet.
    if [ ! -f "${world_dir}/${MAP}.world" ]; then
        echo "no world for ${PROJECT}/${MAP} yet — generating it first"
        /app/generate_world.sh "$PROJECT" "$MAP"
    fi
    # sim speed: dial the world's real_time_factor at LAUNCH, so the knob
    # survives every regeneration of the world file. Doors and lifts then
    # answer that much faster in wall-clock.
    sed -i "s|<real_time_factor>[^<]*</real_time_factor>|<real_time_factor>${DW_SIM_RTF:-3}</real_time_factor>|" \
        "${world_dir}/${MAP}.world"
    export GZ_SIM_RESOURCE_PATH="/projects/${PROJECT}:${maps}:${world_dir}:${world_dir}/models"
    # Software GL: there is no host X server to borrow a GPU from, and these
    # are floorplan-scale scenes.
    export LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe
    export DW_FIT_WINDOW="Gazebo"
    # The GUI is only a viewer onto the headless sim, so it is started after the
    # server and its death does not take the simulation with it.
    exec /app/with_display.sh bash -c '
        ros2 launch "'"${world_dir}"'/sim.launch.xml" &
        LAUNCH=$!
        # doors, lifts and items over HTTP for the harness, beside the sim
        # so the ROS graph is the same one. NOTE: this whole block is one
        # single-quoted string, so no apostrophes in these comments.
        python3 /app/infra_bridge.py \
            --building "'"${maps}/${MAP}.building.yaml"'" \
            --level "${DW_LEVEL:-L11}" \
            --world-file "'"${world_dir}/${MAP}.world"'" \
            --interactables "'"/projects/${PROJECT}/interactable_items.json"'" \
            --port 8090 2>&1 | sed "s/^/[infra] /" &
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
    resolve_map
    [ -n "$MAP" ] || idle_without_map
    building="${maps}/${MAP}.building.yaml"
    if [ ! -f "$building" ]; then
        echo "no such map: $building" >&2
        exit 1
    fi
    export LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe
    export DW_FIT_WINDOW="traffic"
    # traffic-editor resolves the floorplan image relative to its own cwd
    cd "$maps"
    exec /app/with_display.sh traffic-editor "$building"
    ;;

*)
    echo "unknown role '$ROLE' (want: jobs, world, sim, editor, galaxea)" >&2
    exit 1
    ;;
esac
