# dreamworld_gs — panorama -> navigable 3DGS world (+ Isaac Sim USDZ)
#
# Everything about one building lives in assets/projects/<project>/:
#
#   maps/    the authored floorplan (<map>.building.yaml + its images)
#   worlds/  generated from it: Gazebo world, models, nav graph
#   panos/   one 360 of the real place per waypoint, named for it
#   splats/  the world generated from each
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
#   just align                       turn each panorama to face the building
#   just generate L11.v6             panos/L11.v6.jpg -> splats/L11.v6/
#   just plan                        every waypoint, and how far along it is
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
# HY-World shards across every visible GPU, so the rank count it is launched
# with has to match what the container can see. Derived, not repeated — but
# four is also what upstream ships and what its FSDP sharding of a 14B video
# model is known to work with, so widening this is not free.
gpus   := `echo "${DW_GPU_IDS:-1,2,3,4}" | tr ',' '\n' | grep -c .`
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
        mkdir -p "$p"maps "$p"worlds "$p"panos "$p"splats
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
    #!/usr/bin/env bash
    set -euo pipefail
    # Serialised. Every recipe that submits a job depends on this, so a batch
    # running seven captures at once calls it seven times together — and two
    # `docker compose up` racing on the same container leave one of them with
    # "removal already in progress" and a failed capture. It is a no-op once
    # everything is running, which is why this only bites after a restart.
    exec 9>>{{repo}}/.up.lock
    flock 9
    docker compose up -d --wait
    echo
    echo "  project       {{project}}   (just use <name> to switch)"
    echo "  jobs + logs   http://localhost:4200"
    echo "  worlds        http://localhost:8081/?url=files/<scene>/world.ply"
    echo "  panoramas     http://localhost:8082"
    echo "  rmf sim       http://localhost:8083"
    echo "  traffic ed    http://localhost:8084"
    echo
    echo "  remote? ssh -L 4200:localhost:4200 -L 8081:localhost:8081 \\"
    echo "              -L 8082:localhost:8082 -L 8083:localhost:8083 \\"
    echo "              -L 8084:localhost:8084 <this-host>"

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

# Generate the world at one waypoint, from one panorama of it.
#
# panos/<id>.jpg is a single 360 of a place, aligned so its centre column faces
# the building's +X. HY-World imagines the rest: ~400 views out of the one, then
# a splat trained on them. `just plan` lists the ids this project's map defines,
# and `just vertices` the ones on a level.
#
# ^C stops following; the job keeps running (watch it at :4200).
#
# Generate the world at one waypoint, from its panorama.
generate id proj=project: up
    #!/usr/bin/env bash
    set -euo pipefail
    dir={{assets}}/projects/{{proj}}
    if [ ! -d "$dir" ]; then
        echo "no such project: {{proj}}" >&2
        just projects >&2
        exit 1
    fi
    src=$(ls -d "$dir"/panos/{{id}}.* 2>/dev/null | head -1 || true)
    if [ -z "$src" ]; then
        echo "no panorama for '{{id}}' in {{proj}}" >&2
        echo "have:" >&2
        find "$dir/panos" -mindepth 1 -maxdepth 1 2>/dev/null \
            | sed "s|$dir/panos/|  |" >&2
        exit 1
    fi
    # ids contain dots (L11.cafe), so strip the extension, not to the first dot
    id=$(basename "${src%.*}")
    out="$dir/splats/$id"
    if [ -f "$out/world.ply" ]; then
        echo "{{proj}} $id: already built, skipping"
        echo "   (delete assets/projects/{{proj}}/splats/$id to rebuild)"
        exit 0
    fi
    mkdir -p "$out"
    win=/workspace/projects/{{proj}}
    cp "$src" "$out/_input.${src##*.}"
    # HY-World reads panorama.png. One line, because a recipe body must be
    # indented for `just` to parse it and that same indentation reaches
    # `python -c` as an IndentationError — which is what this did, once.
    docker compose exec -T generator python -c "from pathlib import Path; import PIL.Image as I; I.MAX_IMAGE_PIXELS=None; d=Path('$win/splats/$id'); s=next(p for p in d.iterdir() if p.stem=='_input'); I.open(s).convert('RGB').save(d/'panorama.png'); s.unlink()"
    docker compose exec -T generator python submit.py \
        generate-world/dreamworld \
        scene="$win/splats/$id" \
        gpus={{gpus}} steps={{steps}}
    echo "-> assets/projects/{{proj}}/splats/$id/world.ply (+ .usdz, .cam.json, .paths.json)"
    echo "   view: http://localhost:8081/?url=files/{{proj}}/splats/$id/world.ply"

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
#
# Build the Gazebo world and nav graph from a project's map.
world map="" proj=project: up
    docker compose exec -T generator python submit.py \
        build-world/dreamworld project={{proj}} map="{{map}}"
    docker compose restart rmfsim 2>/dev/null || true

# The panorama alignment tool, on the host rather than in a container: it edits
# assets/projects/*/panos in place and wants a browser pointed at it.
#
# A 360 records no heading, so a panorama arrives turned whichever way the
# photographer happened to be standing. Rotate it until you are looking down a
# corridor you can name, save, and the file itself is rewritten facing the
# building's +X — so everything downstream loads it already right, and nothing
# has to carry a correction around. Do it before `just generate`: the world is
# built from the panorama.
#
# Turn each panorama to face the building, before generating from it.
align level="L11" proj=project:
    @echo "  http://localhost:8085"
    uv run {{repo}}/scripts/align_panos.py --project {{proj}} --level {{level}}

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
#
# Package a project into one tarball, to carry to another node.
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

# Run it on a fresh clone of this repo, then `just up` — the project lands in
# assets/projects/ and the stack finds it.
#
# Restore a bundled project, in place.
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

# Every waypoint's id, and where to find it in the traffic editor.
#
# Three numberings exist and none agree: building.yaml numbers all of a level's
# vertices including wall corners, the nav graph keeps only the traversable ones
# and renumbers from zero, and an id is a vertex's name or v<nav index>. So
# L11.v6 is nav vertex 6 and drawing vertex 216, and no offset relates them.
#
#   just vertices          every level
#   just vertices L11      one level
#
# The waypoints of a project's nav graph, with their positions.
vertices level="" proj=project:
    @python3 {{repo}}/scripts/vertices.py {{proj}} {{level}}

# What this project's map says exists, and how much of it you have.
#
# build-world writes worlds/<map>/capture_plan.json: every waypoint of the nav
# graph. This reads it back against what is on disk, so the gap between the
# building and the worlds built from it is a list rather than a guess.
#
#   just plan            every waypoint
#   just plan missing    only the unfinished ones
#
# Every waypoint, and how far along it is.
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
        printf ' %s %-22s %s map(s)  %s world(s)  %s panorama(s)  %s splat(s)\n' \
            "$mark" "$n" \
            "$(count "$p/maps" '-name *.building.yaml')" \
            "$(count "$p/worlds" '-type d')" \
            "$(count "$p/panos" '-name *.[jJpP]*[gG]')" \
            "$(count "$p/splats" '-name world.ply' 2 2)"
    done
    [ "$found" = 1 ] || echo "  none yet — run: just _env   (seeds samples/)"
    echo
    echo "  * = active (just use <name> to switch)"

# Recent job runs and their state.
jobs:
    docker compose exec -T generator prefect flow-run ls --limit 15

