#!/bin/bash
# A traffic-editor building.yaml -> Gazebo world + models + nav graph.
#
#   generate_world.sh <project> [map]
#     reads  /projects/<project>/maps/<map>.building.yaml
#     writes /projects/<project>/worlds/<map>/
#              <map>.world, models/, nav_graphs/, sim.launch.xml
#
#   map defaults to the project's own name, or to the only map in maps/.
#
# nav_graphs/0.yaml is the artifact that matters beyond the simulation: it is
# the building's traversal semantics — named waypoints, the lanes between them,
# and which lanes cross a door — which is what the splat side renders against.
#
# No `set -u`: sourcing the ROS setup.bash below trips on an unbound COLCON_TRACE.
set -eo pipefail

. /rmf_demos_ws/install/setup.bash

PROJECT="${1:?usage: generate_world.sh <project> [map]}"
MAP="${2:-}"
MAPS="/projects/${PROJECT}/maps"

if [ ! -d "$MAPS" ]; then
    echo "no maps/ in project '${PROJECT}'" >&2
    echo "projects available:" >&2
    ls /projects 2>/dev/null | sed 's/^/  /' >&2
    exit 1
fi

# one map per project is the common case, so name it only when there is a choice
if [ -z "$MAP" ]; then
    if [ -f "${MAPS}/${PROJECT}.building.yaml" ]; then
        MAP="$PROJECT"
    else
        # `|| true`: under `set -e` a failing glob inside $(...) exits the
        # shell before any of this can be reported
        found=$(find "$MAPS" -maxdepth 1 -name '*.building.yaml' 2>/dev/null | sort || true)
        n=$(printf '%s' "$found" | grep -c . || true)
        if [ "$n" = 0 ]; then
            echo "project '${PROJECT}' has no map: ${MAPS} is empty." >&2
            echo "Add a <name>.building.yaml there (author one at :8084), or" >&2
            echo "generate for a project that has one." >&2
            exit 1
        fi
        if [ "$n" -ne 1 ]; then
            echo "which map? ${MAPS} holds:" >&2
            printf '%s\n' "$found" | xargs -n1 basename 2>/dev/null \
                | sed 's/\.building\.yaml$//' | sed 's/^/  /' >&2
            exit 1
        fi
        MAP=$(basename "$(printf '%s' "$found" | head -1)" .building.yaml)
    fi
fi

IN="${MAPS}/${MAP}.building.yaml"
OUT="/projects/${PROJECT}/worlds/${MAP}"

if [ ! -f "$IN" ]; then
    echo "no such map: $IN" >&2
    exit 1
fi

# the generator only ever adds files, so a stale wall mesh from a previous
# edit would linger; start clean
rm -rf "$OUT"
mkdir -p "$OUT/models" "$OUT/nav_graphs"

ros2 run rmf_building_map_tools building_map_generator gazebo \
    "$IN" "$OUT/${MAP}.world" "$OUT/models"

python3 /app/postprocess_world.py "$OUT/${MAP}.world" "$IN"
# The generator bakes near-flat paint onto the walls, which no feature detector
# can match — and this world gets photographed, not just driven through.
# Opt-in, because the reason for it has gone.
#
# The quasiperiodic pattern exists so structure from motion has corners to
# match on blank sim walls — untextured, panoramas registered 2 of 60 views.
# A simulated capture no longer runs structure from motion: its poses are
# recorded, its geometry is seeded from the depth camera and supervised by it.
# What is left is a checkerboard mosaic on every surface, which no corridor
# has, and which dominates every render of the result.
#
# Kept for exercising the real path — reconstruct-world does still solve for
# structure — against a simulated capture. DW_TEXTURIZE=1 turns it back on.
if [ "${DW_TEXTURIZE:-0}" = "1" ]; then
    python3 /app/texturize.py "$OUT/models"
else
    echo "leaving the map's own surfaces alone (DW_TEXTURIZE=1 to pattern them)"
fi

ros2 run rmf_building_map_tools building_map_generator nav "$IN" "$OUT/nav_graphs"

sed -e "s|__PROJECT__|${PROJECT}|g" -e "s|__MAP__|${MAP}|g" \
    /app/sim.launch.xml.template > "$OUT/sim.launch.xml"

echo
echo "generated $OUT:"
echo "  world      ${MAP}.world"
echo "  models     $(find "$OUT/models" -maxdepth 1 -mindepth 1 | wc -l) model dir(s)"
for g in "$OUT"/nav_graphs/*.yaml; do
    [ -e "$g" ] || continue
    python3 - "$g" <<'PY'
import sys, yaml
g = yaml.safe_load(open(sys.argv[1]))
for name, lvl in (g.get("levels") or {}).items():
    v, l = lvl.get("vertices") or [], lvl.get("lanes") or []
    named = sum(1 for x in v if len(x) > 2 and isinstance(x[2], dict) and x[2].get("name"))
    print(f"  nav {sys.argv[1].split('/')[-1]} level {name}: "
          f"{len(v)} vertices ({named} named), {len(l)} lanes")
PY
done
