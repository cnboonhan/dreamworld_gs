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
# What you hand it decides the pipeline:
#
#   a folder of panoramas   several viewpoints of a real place, so geometry is
#                           measured — reproject, COLMAP, gaussian splatting.
#                           This is the path for real 360 captures.
#   a single image file     one viewpoint, so the rest is imagined — the
#                           generative HY-World path, which is the only thing
#                           in this repo that uses the VLM.
#
# spacing: metres between consecutive standpoints, default 0.5. SfM is
# scale-free, so this is what puts the world in metres, which a simulator
# needs; pass 0 to leave it unitless. ^C stops following, the job keeps
# running (watch it at :4200).
generate id spacing="0.25" proj=project: up
    #!/usr/bin/env bash
    set -euo pipefail
    dir={{assets}}/projects/{{proj}}
    if [ ! -d "$dir" ]; then
        echo "no such project: {{proj}}" >&2
        just projects >&2
        exit 1
    fi
    src=$(ls -d "$dir"/panos/{{id}} "$dir"/panos/{{id}}.* 2>/dev/null | head -1 || true)
    if [ -z "$src" ]; then
        echo "nothing to reconstruct for '{{id}}' in {{proj}}" >&2
        echo "captured so far:" >&2
        find "$dir/panos" -mindepth 1 -maxdepth 1 2>/dev/null \
            | sed "s|$dir/panos/|  |" >&2
        echo "run 'just plan' for the ids this project's map defines" >&2
        exit 1
    fi
    # ids contain dots (L11.cafe), so strip an extension only from a file
    if [ -d "$src" ]; then id=$(basename "$src"); else id=$(basename "${src%.*}"); fi
    out="$dir/splats/$id"
    if [ -f "$out/world.ply" ]; then
        echo "{{proj}} $id: already built, skipping"
        echo "   (delete assets/projects/{{proj}}/splats/$id to rebuild)"
        exit 0
    fi
    mkdir -p "$out"
    win=/workspace/projects/{{proj}}

    if [ -d "$src" ]; then
        # `|| true`: an unmatched glob makes ls fail, and pipefail turns that
        # into an exit before anything is submitted
        n=$(find "$src" -maxdepth 1 -type f \( -iname '*.png' -o -iname '*.jpg' \) 2>/dev/null | wc -l || true)
        # Two different jobs, because they are two different pieces of work: a
        # simulated capture recorded where it stood, a real one cannot. The
        # choice is made here, at submission, so the queue names which ran.
        if [ -f "$src/poses.json" ]; then
            echo "reconstructing {{proj}} $id from $n simulated panoramas (poses recorded)"
            docker compose exec -T generator python submit.py \
                reconstruct-simulated/dreamworld \
                scene="$win/splats/$id" \
                panos="$win/panos/$id"
        else
            echo "reconstructing {{proj}} $id from $n panoramas"
            docker compose exec -T generator python submit.py \
                reconstruct-world/dreamworld \
                scene="$win/splats/$id" \
                panos="$win/panos/$id" \
                spacing={{spacing}}
        fi
    else
        cp "$src" "$out/_input.${src##*.}"
        # The generative pipeline reads panorama.png. One line, because a
        # recipe body must be indented for `just` to parse it and that same
        # indentation reaches `python -c` as an IndentationError — which is
        # what this branch did, before anything else could run.
        docker compose exec -T generator python -c "from pathlib import Path; import PIL.Image as I; I.MAX_IMAGE_PIXELS=None; d=Path('$win/splats/$id'); s=next(p for p in d.iterdir() if p.stem=='_input'); I.open(s).convert('RGB').save(d/'panorama.png'); s.unlink()"
        docker compose exec -T generator python submit.py \
            generate-world/dreamworld \
            scene="$win/splats/$id" \
            gpus={{gpus}} steps={{steps}}
    fi
    echo "-> assets/projects/{{proj}}/splats/$id/world.ply (+ .usdz, .cam.json, .path.json)"
    echo "   view: http://localhost:8081/?url=files/{{proj}}/splats/$id/world.ply"

# Render a walkthrough of one splat, riding the walk it was built from.
#
# Paced at walking speed from the length the splat's sidecar records, so a one
# metre corridor and a six metre one are watched at the same speed.
#
#   just video L11.cafe--v7@world
video id proj=project: up
    #!/usr/bin/env bash
    set -euo pipefail
    dir={{assets}}/projects/{{proj}}
    if [ ! -f "$dir/splats/{{id}}/world.ply" ]; then
        echo "no splat built for '{{id}}' in {{proj}} — run: just generate {{id}}" >&2
        exit 1
    fi
    docker compose exec -T generator python submit.py \
        render-video/dreamworld \
        scene=/workspace/projects/{{proj}}/splats/{{id}}
    echo "-> assets/projects/{{proj}}/splats/{{id}}/walkthrough.mp4"

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

# Photograph one corridor of the simulated building.
#
# Stands a camera at points along the lane and captures a 360 panorama at each,
# writing panos/<edge>/000.png ... — a folder of numbered images, and nothing
# else. That is exactly what someone hands over after walking a corridor, so
# everything downstream is exercised on the same input it will get for real.
#
# spacing is how far apart you stop, as when walking — the number of stops
# follows from the corridor's length. Half a metre is plenty for the generative
# path, which takes one panorama of a corridor and imagines the rest.
#
# It matters far more if you reconstruct a corridor instead of generating one,
# where it is the strongest lever on how the splat looks: nine standpoints
# along a four-metre corridor fit the views they were trained on and fall apart
# between them, which is exactly where a walkthrough goes. Halving it to 0.25 m
# took a held-out viewpoint from 29.65 to 45.27 dB. It is also what makes a
# reconstruction metric, so pass the same value to `generate`.
#
#   just capture L11.cafe--v7        stop every half metre
#   just capture L11.cafe--v7 0.25   denser, for reconstructing rather than generating
capture edge spacing="0.5" proj=project: up
    docker compose exec -T generator python submit.py \
        capture-edge/dreamworld \
        project={{proj}} edge={{edge}} spacing={{spacing}}
    @echo "-> assets/projects/{{proj}}/panos/{{edge}}/"

# One generated world per corridor, from a single panorama of it.
#
# HY-World takes one vantage point and imagines the rest, so a corridor needs
# one panorama, not a walk: this photographs it if that has not been done, then
# hands one standpoint to generate-world — the one that can see the most of the
# corridor, measured from the range map the capture wrote (see
# tools/pick_panorama.py).
#
#   just world-edge L11.cafe--v7
world-edge edge proj=project: up
    #!/usr/bin/env bash
    set -euo pipefail
    dir={{assets}}/projects/{{proj}}
    # Any panorama, not just a simulated one. This globbed *.png, and a real
    # 360 camera writes .JPG — so a corridor already photographed by hand read
    # as unphotographed, and the simulator was sent to shoot over it.
    if ! compgen -G "$dir/panos/{{edge}}/*.[pPjJ]*[gG]" >/dev/null; then
        just capture {{edge}} 0.5 {{proj}}
    fi
    pick=$(docker compose exec -T generator python /opt/tools/pick_panorama.py \
        /workspace/projects/{{proj}}/panos/{{edge}} | tr -d '\r')
    # keep the extension: a real capture arrives as .jpg, and naming a JPEG
    # .png works only because PIL sniffs the content rather than the name
    cp "$dir/panos/{{edge}}/$pick" "$dir/panos/{{edge}}@world.${pick##*.}"
    echo "generating a world for {{edge}} from $pick"
    # spacing is inert on this branch — one panorama, so nothing is walked
    just generate "{{edge}}@world" 0.5 {{proj}}

# Photograph every corridor that has no panoramas yet — one job each, so a bad
# one is a single re-run rather than a lost batch.
capture-all spacing="0.5" proj=project: up
    #!/usr/bin/env bash
    set -euo pipefail
    plan=$(find {{assets}}/projects/{{proj}}/worlds -name capture_plan.json | head -1)
    [ -n "$plan" ] || { echo "no capture plan — run: just world" >&2; exit 1; }
    todo=$(python3 -c "
    import json, sys
    from pathlib import Path
    root = Path('{{assets}}/projects/{{proj}}')
    doc = json.load(open('$plan'))
    for c in doc['capture']:
        if not any((root / c['panos']).glob('*')):
            print(c['id'])
    ")
    n=$(printf '%s' "$todo" | grep -c . || true)
    echo "$n corridor(s) to photograph"
    for e in $todo; do just capture "$e" {{spacing}} {{proj}}; done

# Plan the walk between two waypoints, as a route the viewer streams along.
#
# Writes traversals/<from>__<to>.route.json: the path through the building in
# metres, and which splat covers which stretch of it. Nothing is rendered — the
# viewer follows the polyline with its tour parameter and loads each splat as it
# reaches it.
#
#   just route cafe playpen
#   just route L11.cafe L11.apex_lab
route start goal proj=project: up
    docker compose exec -T generator python submit.py \
        plan-route/dreamworld project={{proj}} start={{start}} goal={{goal}}
    @echo "-> assets/projects/{{proj}}/traversals/"
    @echo "   walk it: http://localhost:8081/?route={{proj}}/<from>__<to>"

# Render a planned route to an mp4, for showing someone not at this machine.
#
# The viewer walks a route live and needs no render — this is the same walk
# written to a file. It is also the check that the corridors meet without a
# step at each vertex, since every frame is rasterised from the union of their
# gaussians.
#
#   just route-video L11.cafe L11.v3
route-video start goal proj=project: up
    #!/usr/bin/env bash
    set -euo pipefail
    f={{assets}}/projects/{{proj}}/traversals/{{start}}__{{goal}}.route.json
    if [ ! -f "$f" ]; then
        echo "no route {{start}} -> {{goal}} — run: just route {{start}} {{goal}}" >&2
        exit 1
    fi
    docker compose exec -T generator python submit.py \
        render-route/dreamworld \
        route=/workspace/projects/{{proj}}/traversals/{{start}}__{{goal}}.route.json
    echo "-> assets/projects/{{proj}}/traversals/{{start}}__{{goal}}.mp4"

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
            "$(count "$p/panos" '-type d')" \
            "$(count "$p/splats" '-name world.ply' 2 2)"
    done
    [ "$found" = 1 ] || echo "  none yet — run: just _env   (seeds samples/)"
    echo
    echo "  * = active (just use <name> to switch)"

# Recent job runs and their state.
jobs:
    docker compose exec -T generator prefect flow-run ls --limit 15

