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
    @mkdir -p {{assets}}/prefect {{assets}}/projects
    @printf 'DW_UID=%s\nDW_GID=%s\nDW_PROJECT=%s\n' \
        "$(id -u)" "$(id -g)" "{{project}}" > {{repo}}/.env
    # Seed the sample project, so the RMF sim has a building to open. Copies
    # only what is missing (-n), so an existing project — or an edited map — is
    # never overwritten.
    @for m in {{repo}}/samples/*/; do \
        n=$(basename "$m"); \
        mkdir -p {{assets}}/projects/"$n"/maps {{assets}}/projects/"$n"/worlds \
                 {{assets}}/projects/"$n"/panos {{assets}}/projects/"$n"/splats; \
        cp -rn "$m." {{assets}}/projects/"$n"/ 2>/dev/null || true; \
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

# A folder of panoramas of one space is reconstructed together (reproject ->
# SfM -> gaussian splatting). A single image file takes the generative
# HY-World path instead: one vantage point, the rest imagined.
#
# spacing: metres between consecutive standpoints, default 0.5. SfM is
# scale-free, so this is what puts the world in metres, which a simulator
# needs; pass 0 to leave it unitless. ^C stops following, the job keeps
# running (watch it at :4200).
#
# Build assets/projects/<project>/splats/<scene> from its panos/<scene>.
generate scene spacing="0.5" proj=project: up
    #!/usr/bin/env bash
    set -euo pipefail
    dir={{assets}}/projects/{{proj}}
    if [ ! -d "$dir" ]; then
        echo "no such project: {{proj}}" >&2
        ls {{assets}}/projects 2>/dev/null | sed 's/^/  /' >&2
        exit 1
    fi
    src=$(ls -d "$dir"/panos/{{scene}} "$dir"/panos/{{scene}}.* 2>/dev/null | head -1 || true)
    if [ -z "$src" ]; then
        echo "no such panorama or folder: {{proj}}/{{scene}}" >&2
        echo "available in {{proj}}/panos:" >&2
        ls "$dir"/panos 2>/dev/null | sed 's/^/  /' >&2
        exit 1
    fi
    scene=$(basename "${src%.*}")
    if [ -f "$dir"/splats/"$scene"/world.ply ]; then
        echo "{{proj}}/$scene: already built, skipping"
        echo "   (delete assets/projects/{{proj}}/splats/$scene to rebuild)"
        exit 0
    fi
    mkdir -p "$dir"/splats/"$scene"
    win=/workspace/projects/{{proj}}

    if [ -d "$src" ]; then
        n=$(ls "$src" | wc -l)
        echo "reconstructing {{proj}}/$scene from $n panoramas"
        docker compose exec -T generator python submit.py \
            reconstruct-world/dreamworld \
            scene="$win"/splats/"$scene" \
            panos="$win"/panos/"$(basename "$src")" \
            spacing={{spacing}}
    else
        cp "$src" "$dir"/splats/"$scene"/_input.${src##*.}
        # the generative pipeline reads panorama.png
        docker compose exec -T generator python -c "
        from pathlib import Path
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        d = Path('$win/splats/$scene')
        src = next(p for p in d.iterdir() if p.stem == '_input')
        Image.open(src).convert('RGB').save(d / 'panorama.png')
        src.unlink()"
        docker compose exec -T generator python submit.py \
            generate-world/dreamworld \
            scene="$win"/splats/"$scene" \
            gpus={{gpus}} steps={{steps}}
    fi
    echo "-> assets/projects/{{proj}}/splats/$scene/world.ply (+ .usdz, .cam.json, .path.json)"
    echo "   view: http://localhost:8081/?url=files/{{proj}}/splats/$scene/world.ply"

# Render a walkthrough video following the capture path.
#
# The camera travels the straight line fitted through the standpoints the
# panoramas were shot from, because that is where the scene was observed.
# The viewer's tour uses the same path.
#
#   just video h2rc 40           longer
#   just video h2rc 20 spline    weave through each standpoint exactly
#   just video h2rc 20 orbit     circle the centre (expect artifacts)
#
# Render a walkthrough video following the capture path.
video scene seconds="20" path="line" proj=project: up
    docker compose exec -T generator python submit.py \
        render-video/dreamworld \
        scene=/workspace/projects/{{proj}}/splats/{{scene}} \
        seconds={{seconds}} path={{path}}
    @echo "-> assets/projects/{{proj}}/splats/{{scene}}/walkthrough.mp4"

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
            "$(count "$p/panos" '-type d')" \
            "$(count "$p/splats" '-name world.ply' 2 2)"
    done
    [ "$found" = 1 ] || echo "  none yet — run: just _env   (seeds samples/)"
    echo
    echo "  * = active (just use <name> to switch)"

# Recent job runs and their state.
jobs:
    docker compose exec -T generator prefect flow-run ls --limit 15

