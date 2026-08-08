# dreamworld_gs — panorama -> navigable 3DGS world (+ Isaac Sim USDZ)
#
# Everything about one building lives in assets/projects/<project>/:
#
#   maps/    the authored floorplan (<map>.building.yaml + its images)
#   worlds/  generated from it: Gazebo world, models, nav graph
#   panos/   360 captures of the real place, one folder per scene
#   splats/  what those captures reconstruct into
#
# One project is active at a time. DW_PROJECT in .env selects it, the whole
# stack reads it, and every recipe below defaults to it:
#
#   just use htx                     switch the stack to assets/projects/htx
# Every operation is a job: submitted to Prefect, logged and retryable at
# :4200. These recipes only submit and follow — ^C stops following, the job
# keeps running.
#
#   just setup                       one-time: fetch weights + build images
#   just up                          start everything, print the URLs
#   just projects                    what's there, and which one is active
#   just world                       maps/ -> a Gazebo world + nav graph
#   just generate h2rc               panos/h2rc/ -> splats/h2rc/
#   just video h2rc                  walkthrough mp4 along the capture path
#
# Override for a single command without switching:  DW_PROJECT=htx just world
#
# Services live in compose.yaml; these recipes drive them.

set shell := ["bash", "-euo", "pipefail", "-c"]
# .env carries DW_PROJECT, so `just` and a bare `docker compose` agree on which
# project is active
set dotenv-load := true

repo   := justfile_directory()
assets := repo / "assets"
gpus   := "4"
steps  := "2000"

# The active project. A real environment variable wins over .env, so a single
# command can target another project without switching the stack.
project := env_var_or_default("DW_PROJECT", "multilevel_office")

# compose runs containers as you, not root, so outputs stay writable.
# Also written to .env so plain `docker compose ...` behaves the same.
export DW_UID := `id -u`
export DW_GID := `id -g`

_default:
    @just --list

# Everything needed to run offline: model weights + both images (~500GB).
setup: _env fetch-assets build

_env:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{assets}}/prefect {{assets}}/projects
    # Keep whatever .env already says the active project is. `project` may be a
    # one-command override (DW_PROJECT=x just ...), and an override must not
    # quietly become the new default — only `just use` switches.
    active=$(sed -n 's/^DW_PROJECT=//p' {{repo}}/.env 2>/dev/null | tail -1)
    printf 'DW_UID=%s\nDW_GID=%s\nDW_PROJECT=%s\n' \
        "$(id -u)" "$(id -g)" "${active:-{{project}}}" > {{repo}}/.env
    # Seed the sample project, so the RMF sim has a building to open. Copies
    # only what is missing (-n), so an existing project — or an edited map — is
    # never overwritten. Every project gets the same four drawers.
    for m in {{repo}}/samples/*/; do
        n=$(basename "$m")
        cp -rn "$m." {{assets}}/projects/"$n"/ 2>/dev/null || true
    done
    for p in {{assets}}/projects/*/; do
        [ -d "$p" ] || continue
        mkdir -p "$p"maps "$p"worlds \
                 "$p"panos/vertices "$p"panos/edges \
                 "$p"splats/vertices "$p"splats/edges
    done

# Download all models into assets/ (idempotent; list in scripts/models.txt).
fetch-assets:
    HF_HOME={{assets}}/hf uv run --with huggingface_hub --with modelscope \
        --with safetensors --no-project \
        {{repo}}/scripts/fetch_assets.py {{assets}}

# Build the generator and viewer images.
build:
    docker compose build

# Job server, VLM, generator and both viewers come up together and stay up;
# `just generate` depends on this recipe, so nothing needs launching by hand.
#
# Start everything and print the URLs.
up: _env
    docker compose up -d --wait
    @echo
    @echo "  project       {{project}}   (just use <name> to switch)"
    @echo "  jobs + logs   http://localhost:4200"
    @echo "  worlds        http://localhost:8081/?url=files/<scene>/world.ply"
    @echo "  panoramas     http://localhost:8082"
    @echo "  rmf sim       http://localhost:8083"
    @echo "  traffic ed    http://localhost:8084"
    @echo
    @echo "  remote? ssh -L 4200:localhost:4200 -L 8081:localhost:8081 \\"
    @echo "              -L 8082:localhost:8082 -L 8083:localhost:8083 \\"
    @echo "              -L 8084:localhost:8084 <this-host>"

# Point the whole stack at another project and restart the services that care.
#
#   just use htx
#
# Writes DW_PROJECT into .env, so a bare `docker compose up` agrees with `just`.
use PROJECT:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -d {{assets}}/projects/{{PROJECT}} ]; then
        echo "no such project: {{PROJECT}}" >&2
        just projects >&2
        exit 1
    fi
    printf 'DW_UID=%s\nDW_GID=%s\nDW_PROJECT=%s\n' \
        "$(id -u)" "$(id -g)" "{{PROJECT}}" > {{repo}}/.env
    echo "active project: {{PROJECT}}"
    if docker compose ps --status running -q rmfsim >/dev/null 2>&1; then
        docker compose up -d --force-recreate rmfsim editor
    fi

# Stop everything.
down:
    docker compose down

# Reconstruct one part of the building from its panoramas.
#
# Panoramas live under the place they photograph:
#
#   panos/vertices/<id>/   a standpoint — one room, one junction, one waypoint
#   panos/edges/<a>--<b>/  the corridor between two of them
#
# and the splat lands in splats/vertices/<id>/ or splats/edges/<a>--<b>/.
# `just plan` lists the ids this project's map defines.
#
# A folder of panoramas is reconstructed together (reproject -> SfM -> gaussian
# splatting). A single image file takes the generative HY-World path instead:
# one vantage point, the rest imagined.
#
# spacing: metres between consecutive standpoints, default 0.5. SfM is
# scale-free, so this is what puts the world in metres, which a simulator
# needs; pass 0 to leave it unitless. ^C stops following, the job keeps
# running (watch it at :4200).
generate id spacing="0.5" proj=project: up
    #!/usr/bin/env bash
    set -euo pipefail
    dir={{assets}}/projects/{{proj}}
    if [ ! -d "$dir" ]; then
        echo "no such project: {{proj}}" >&2
        just projects >&2
        exit 1
    fi
    # an id names a vertex or an edge; find which
    src=""; kind=""
    for k in vertices edges; do
        for c in "$dir/panos/$k/{{id}}" "$dir"/panos/$k/{{id}}.*; do
            [ -e "$c" ] || continue
            if [ -n "$src" ] && [ "$kind" != "$k" ]; then
                echo "'{{id}}' exists under both vertices/ and edges/ — rename one" >&2
                exit 1
            fi
            src="$c"; kind="$k"
        done
    done
    if [ -z "$src" ]; then
        echo "nothing to reconstruct for '{{id}}' in {{proj}}" >&2
        echo "captured so far:" >&2
        for k in vertices edges; do
            find "$dir/panos/$k" -mindepth 1 -maxdepth 1 2>/dev/null \
                | sed "s|$dir/panos/|  |" >&2
        done
        echo "run 'just plan' for the ids this project's map defines" >&2
        exit 1
    fi
    # ids contain dots (L11.cafe), so strip an extension only from a file
    if [ -d "$src" ]; then id=$(basename "$src"); else id=$(basename "${src%.*}"); fi
    out="$dir/splats/$kind/$id"
    if [ -f "$out/world.ply" ]; then
        echo "{{proj}} $kind/$id: already built, skipping"
        echo "   (delete assets/projects/{{proj}}/splats/$kind/$id to rebuild)"
        exit 0
    fi
    mkdir -p "$out"
    win=/workspace/projects/{{proj}}

    if [ -d "$src" ]; then
        n=$(ls "$src" | wc -l)
        echo "reconstructing {{proj}} $kind/$id from $n panoramas"
        docker compose exec -T generator python submit.py \
            reconstruct-world/dreamworld \
            scene="$win/splats/$kind/$id" \
            panos="$win/panos/$kind/$id" \
            spacing={{spacing}}
    else
        cp "$src" "$out/_input.${src##*.}"
        # the generative pipeline reads panorama.png
        docker compose exec -T generator python -c "
        from pathlib import Path
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        d = Path('$win/splats/$kind/$id')
        src = next(p for p in d.iterdir() if p.stem == '_input')
        Image.open(src).convert('RGB').save(d / 'panorama.png')
        src.unlink()"
        docker compose exec -T generator python submit.py \
            generate-world/dreamworld \
            scene="$win/splats/$kind/$id" \
            gpus={{gpus}} steps={{steps}}
    fi
    echo "-> assets/projects/{{proj}}/splats/$kind/$id/world.ply (+ .usdz, .cam.json, .path.json)"
    echo "   view: http://localhost:8081/?url=files/{{proj}}/splats/$kind/$id/world.ply"

#   just video cafe 40           longer
#   just video cafe 20 spline    weave through each standpoint exactly
#   just video cafe 20 orbit     circle the centre (expect artifacts)
#
# Render a walkthrough of one vertex or edge, along its capture path.
video id seconds="20" path="line" proj=project: up
    #!/usr/bin/env bash
    set -euo pipefail
    dir={{assets}}/projects/{{proj}}
    kind=""
    for k in vertices edges; do
        [ -f "$dir/splats/$k/{{id}}/world.ply" ] && kind="$k"
    done
    if [ -z "$kind" ]; then
        echo "no splat built for '{{id}}' in {{proj}} — run: just generate {{id}}" >&2
        exit 1
    fi
    docker compose exec -T generator python submit.py \
        render-video/dreamworld \
        scene=/workspace/projects/{{proj}}/splats/$kind/{{id}} \
        seconds={{seconds}} path={{path}}
    echo "-> assets/projects/{{proj}}/splats/$kind/{{id}}/walkthrough.mp4"

# Build the Gazebo world + nav graph from a project's building map.
#
# Submitted as the build-world job. The nav graph it produces
# (worlds/<map>/nav_graphs/0.yaml) is the output that outlives the simulation:
# named waypoints, the lanes between them, and which lanes cross a door — the
# building's traversal semantics, and what a capture is indexed against. The
# run publishes that as a table in the Prefect UI.
#
# map defaults to the project's own name, or to the only map in its maps/.
# proj defaults to the active project (just use <name>).
world map="" proj=project: up
    docker compose exec -T generator python submit.py \
        build-world/dreamworld project={{proj}} map="{{map}}"
    docker compose restart rmfsim 2>/dev/null || true

# Package a project into one tarball, to carry to another node.
#
# A project is already self-contained — its map, its generated world, its
# panoramas and its splats — so a tarball of it is everything that machine
# needs. Model weights are not included: they are hundreds of GB and come from
# `just setup` on the far side.
#
# Paths are stored as assets/projects/<name>/..., so `just unbundle` puts them
# back exactly where the stack expects them.
#
#   just bundle                    the active project -> dist/
#   just bundle htx /tmp           a named project, somewhere else
bundle proj=project dest="dist":
    #!/usr/bin/env bash
    set -euo pipefail
    src={{assets}}/projects/{{proj}}
    if [ ! -d "$src" ]; then
        echo "no such project: {{proj}}" >&2
        just projects >&2
        exit 1
    fi
    mkdir -p {{dest}}
    out="$(cd {{dest}} && pwd)/{{proj}}-$(date +%Y%m%d-%H%M%S).tar.gz"
    tar czf "$out" -C {{repo}} "assets/projects/{{proj}}"
    n=$(tar tzf "$out" | wc -l)
    echo "bundled {{proj}} -> $out"
    echo "  $n entries, $(du -h "$out" | cut -f1)"
    echo "  restore with: just unbundle $out"

# Restore a bundle made by `just bundle`, in place.
#
# Run it on a fresh clone of this repo, then `just up` — the project lands in
# assets/projects/ and the stack finds it.
unbundle FILE:
    #!/usr/bin/env bash
    set -euo pipefail
    f="{{FILE}}"; [ -f "$f" ] || f="{{repo}}/{{FILE}}"
    if [ ! -f "$f" ]; then
        echo "no such bundle: {{FILE}}" >&2
        exit 1
    fi
    # say what it will land on before it lands on it
    names=$(tar tzf "$f" | sed -n 's|^assets/projects/\([^/]*\)/.*|\1|p' | sort -u)
    for n in $names; do
        [ -d {{assets}}/projects/"$n" ] \
            && echo "note: assets/projects/$n exists and will be merged into"
    done
    tar xzf "$f" -C {{repo}}
    echo "unbundled into {{repo}}/assets/projects: $(echo $names)"
    just projects

# What this project's map says exists, and how much of it you have.
#
# build-world writes worlds/<map>/capture_plan.json: one entry per vertex and
# per edge, with the id its panoramas belong under. This reads it back against
# what is on disk, so the gap between the building and the captures is a list
# rather than a guess.
#
#   just plan            everything
#   just plan missing    only what still needs photographing
plan filter="" proj=project:
    @python3 {{repo}}/scripts/plan_report.py {{assets}}/projects/{{proj}} {{filter}}

# What's in assets/projects, and what each project has.
projects:
    #!/usr/bin/env bash
    # find, not ls+glob: an unmatched glob leaves `ls` with no argument, and it
    # then cheerfully counts the current directory instead of reporting zero.
    count() { find "$1" -mindepth "${3:-1}" -maxdepth "${4:-1}" $2 2>/dev/null | wc -l; }
    found=0
    for p in {{assets}}/projects/*/; do
        [ -d "$p" ] || continue
        found=1
        n=$(basename "$p")
        mark=" "; [ "$n" = "{{project}}" ] && mark="*"
        printf ' %s %-22s %s map(s)  %s world(s)  %s pano set(s)  %s splat(s)\n' \
            "$mark" "$n" \
            "$(count "$p/maps" '-name *.building.yaml')" \
            "$(count "$p/worlds" '-type d')" \
            "$(count "$p/panos" '-type d' 2 2)" \
            "$(count "$p/splats" '-name world.ply' 3 3)"
    done
    [ "$found" = 1 ] || echo "  none yet — run: just _env   (seeds samples/)"
    echo
    echo "  * = active (just use <name> to switch)"

# Recent job runs and their state.
jobs:
    docker compose exec -T generator prefect flow-run ls --limit 15

