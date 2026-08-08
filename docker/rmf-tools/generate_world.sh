#!/bin/bash
# A traffic-editor building.yaml -> Gazebo world + models + nav graph.
#
#   generate_world.sh <map>
#     reads  /maps/<map>/<map>.building.yaml
#     writes /worlds/<map>/{<map>.world, models/, nav_graphs/, sim.launch.xml}
#
# nav_graphs/0.yaml is the artifact that matters beyond the simulation: it is
# the building's traversal semantics — named waypoints, the lanes between them,
# and which lanes cross a door — which is what the splat side renders against.
#
# No `set -u`: sourcing the ROS setup.bash below trips on an unbound COLCON_TRACE.
set -eo pipefail

. /rmf_demos_ws/install/setup.bash

MAP="${1:?usage: generate_world.sh <map>}"
IN="/maps/${MAP}/${MAP}.building.yaml"
OUT="/worlds/${MAP}"

if [ ! -f "$IN" ]; then
    echo "no such map: $IN" >&2
    echo "available in assets/maps:" >&2
    ls /maps 2>/dev/null | sed 's/^/  /' >&2
    exit 1
fi

# the generator only ever adds files, so a stale wall mesh from a previous
# edit would linger; start clean
rm -rf "$OUT"
mkdir -p "$OUT/models" "$OUT/nav_graphs"

ros2 run rmf_building_map_tools building_map_generator gazebo \
    "$IN" "$OUT/${MAP}.world" "$OUT/models"

python3 /app/postprocess_world.py "$OUT/${MAP}.world" "$IN"

ros2 run rmf_building_map_tools building_map_generator nav "$IN" "$OUT/nav_graphs"

sed -e "s|__MAP__|${MAP}|g" /app/sim.launch.xml.template > "$OUT/sim.launch.xml"

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
