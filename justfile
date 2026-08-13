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
# the interactive tool surface (docker/interactive)
iport   := env_var_or_default("DW_INTERACTIVE_PORT", "8086")

# compose runs containers as you, not root, so outputs stay writable.
# Also written to .env so plain `docker compose ...` behaves the same.
export DW_UID := `id -u`
export DW_GID := `id -g`

_default:
    @just --list

# Everything needed to run offline: model weights + the images (~550GB).
#
# Both halves are idempotent and cheap to re-run: a model already in the cache is
# recognised without a network call, so re-running this on a complete box is
# about three seconds and a docker build with everything layered. They
# are one recipe because they answer one question — can this box run the pipeline
# — but they stay separable, because the answer is usually yes for the weights
# and no for the images: a code change needs `images`, and re-checking eight
# HuggingFace repos to find that out is a slow way to learn nothing.
#
#   just setup           weights + images
#   just setup images    after a code change
#   just setup models    weights only (the list is scripts/models.txt)
#
# Everything needed to run offline: weights + images (~550GB).
setup what="all": _env
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{what}}" in
        all|models|images) ;;
        *) echo "unknown: {{what}} — want all, models or images" >&2; exit 1 ;;
    esac
    if [ "{{what}}" != images ]; then
        HF_HOME={{assets}}/hf uv run --with huggingface_hub --with modelscope \
            --with safetensors --no-project \
            {{repo}}/scripts/fetch_assets.py {{assets}}
    fi
    if [ "{{what}}" != models ]; then
        docker compose build
    fi

_env:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{assets}}/prefect {{assets}}/projects
    # Keep whatever .env already says the active project is. `project` may be a
    # one-command override (DW_PROJECT=x just ...), and an override must not
    # quietly become the new default — only `just use` switches.
    active=$(sed -n 's/^DW_PROJECT=//p' {{repo}}/.env 2>/dev/null | tail -1)
    # And keep every OTHER line. .env is also where deployment settings and
    # secrets live — DW_VLM_KEY, DW_VLM_URL, port overrides — and this recipe
    # owns three variables, not the file. It used to rewrite the whole thing,
    # which silently deleted an API key on the next `just up`.
    keep=$(grep -v '^DW_UID=\|^DW_GID=\|^DW_PROJECT=' {{repo}}/.env 2>/dev/null || true)
    { printf 'DW_UID=%s\nDW_GID=%s\nDW_PROJECT=%s\n' \
          "$(id -u)" "$(id -g)" "${active:-{{project}}}"
      if [ -n "$keep" ]; then printf '%s\n' "$keep"; fi
    } > {{repo}}/.env
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

# Job server, VLM, generator and both viewers come up together and stay up;
# `just generate` depends on this recipe, so nothing needs launching by hand.
#
#   just up            the full stack
#   just up minimal    the runtime four (compose.minimal.yaml): viewer, sim,
#                      robot bridge, harness — for a box that only walks a
#                      project someone else generated
#
# Start everything and print the URLs.
up what="all": _env
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{what}}" in
        all)     compose=(docker compose) ;;
        minimal) compose=(docker compose -f {{repo}}/compose.minimal.yaml) ;;
        *) echo "unknown: {{what}} — want all or minimal" >&2; exit 1 ;;
    esac
    # Serialised. Every recipe that submits a job depends on this, so several
    # called together each call it too — and two `docker compose up` racing on
    # the same container leave one with "removal already in progress" and a
    # failed job. It is a no-op once everything is running, which is why this
    # only bites after a restart.
    exec 9>>{{repo}}/.up.lock
    flock 9
    # --remove-orphans: both files are one compose project, so switching shape
    # stops what the other shape started instead of leaving it running beside.
    "${compose[@]}" up -d --wait --remove-orphans
    python3 {{repo}}/scripts/summary.py {{assets}} {{project}} --urls

# Everything about the project in one screen: where each waypoint is, how good
# its world came out, how far along it all is, and what to open.
#
# The report lives in scripts/summary.py, which is also where its four sections
# come from — this is a wrapper so the recipe never becomes a second place any of
# it is written down.
#
#   just summary        every level
#   just summary L11    one level
#
# The whole project in one screen.
summary level="" proj=project:
    @python3 {{repo}}/scripts/summary.py {{assets}} {{proj}} {{level}}

# Drive the building by tool call — the splat viewer walks it and the Galaxea R1
# walks it in Gazebo, edge for edge.
#
# The tool surface is the one ported from dreamworld/docker/dream_interactive:
# go_to, turn, face, open_door, close_door, take a lift, pick, place, plan_route,
# where. What changed is what a call rolls out onto. The dream stitched
# pre-rendered clips into a video pane; there is no such video here, so a walk is
# the splat viewer riding that waypoint's marked corridor and handing over at the
# vertex, live.
#
#   curl localhost:8086/tools
#   curl -X POST localhost:8086/tool -H 'Content-Type: application/json' \
#        -d '{"tool":"go_to","args":{"vertex":"apex_lab"}}'
#   curl -X POST localhost:8086/command -H 'Content-Type: application/json' \
#        -d '{"text":"go to apex_lab"}'
#
# Open the printed viewer URL in a browser: the viewer connects to the
# dashboard on its own host by itself (?agent=off opts out, ?agent=<url>
# points elsewhere), and until one connects the walking tools say so rather
# than reporting a move that never happened.
#
# Drive the building by tool call, in the splat viewer and in Gazebo.
interactive: up
    #!/usr/bin/env bash
    set -euo pipefail
    scene=$(curl -s localhost:{{iport}}/state | python3 -c "import json,sys; print(json.load(sys.stdin)['scene'])")
    echo
    echo "  tools        http://localhost:{{iport}}/tools"
    echo "  robot        http://localhost:8090/state"
    echo "  viewer       http://localhost:8081/?url=files/{{project}}/splats/$scene/world.ply"
    echo
    echo "  open that viewer URL, then drive it:"
    echo "    curl -X POST localhost:{{iport}}/command -H 'Content-Type: application/json' -d '{\"text\":\"go to apex_lab\"}'"

# Are the splat camera and the Gazebo robot walking the corridor together?
#
# They are in different coordinate systems, so their positions cannot be
# compared — but how far along the shared edge each one is can be, and that is
# the number that has to match. Issues the walk and samples both several times a
# second, reporting the worst gap as a fraction of the corridor and in metres.
#
# Needs a splat viewer open (just urls prints the link; it connects itself).
#
#   just sync lift_lobby lift_lobby_north
#
# Check the camera and the robot walk a corridor in step.
sync from to level="":
    @python3 {{repo}}/scripts/check_sync.py {{from}} {{to}} {{level}}

# Put the robot at a waypoint, shut every door and lift, and start again.
#
# Both move together or neither does: resetting the bridge alone would leave the
# dashboard describing a place the robot had left, which is the desync every
# other part of this is built to avoid.
#
# The level is optional and defaults to where the dashboard already is. Naming
# one switches the whole model over — different vertices, lanes, doors and lift
# cabins — exactly as arriving there by lift would.
#
# Not just a move: a run leaves doors held open behind it, and starting the next
# one in a building someone has already walked through is how a test passes for
# the wrong reason.
#
#   just reset cafe            on the current level
#   just reset lift_lobby L1   and switch levels
#   just reset v18             a lift cabin, by index
#
# Put the robot at a waypoint, shut every door, and start again.
reset waypoint level="":
    #!/usr/bin/env bash
    set -euo pipefail
    body="{\"waypoint\":\"{{waypoint}}\",\"level\":\"{{level}}\"}"
    curl -sS -X POST localhost:{{iport}}/reset \
        -H 'Content-Type: application/json' -d "$body" \
      | python3 -c "import json,sys; d=json.load(sys.stdin); print('  ' + (d['message'] if d.get('ok') else '! ' + str(d.get('error'))))"

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
    uv run --with numpy --with pillow --with pyyaml --no-project \
        {{repo}}/scripts/align_panos.py --project {{proj}} --level {{level}}

# What travels is the map, the generated Gazebo world, the panoramas, and each
# splat's deliverables — world.ply, world.usdz, world.cam.json,
# world.paths.json and the panorama it was generated from.
#
# What does not is everything HY-World produced on the way to those — gs_data,
# render_results, navmesh, gs_result — nor anything else that happens to be
# sitting in the project directory. Carrying the lot meant a 103 GB tarball to
# move 1.2 GB of usable output, which is why nobody ever ran this.
#
# Model weights are not included either — hundreds of GB, and `just setup`
# fetches them on the far side.
#
# Paths are stored as assets/projects/<name>/..., so `just unbundle` puts them
# back exactly where the stack expects them.
#
#   just bundle                    the active project -> dist/
#   just bundle htx /tmp           a named project, somewhere else
#
# Package a project's deliverables into one tarball, to carry to another node.
bundle proj=project dest="dist":
    @python3 {{repo}}/scripts/bundle.py pack {{assets}} {{proj}} {{dest}}

# Run it on a fresh clone of this repo, then `just up` — the project lands in
# assets/projects/ and the stack finds it.
#
# Restore a bundled project, in place.
unbundle FILE:
    @python3 {{repo}}/scripts/bundle.py unpack {{repo}} {{FILE}}
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

# What this project's map says exists, and how much of it you have.
#
# build-world writes worlds/<map>/capture_plan.json: every waypoint of the nav
# graph. This reads it back against what is on disk, so the gap between the
# building and the worlds built from it is a list rather than a guess.
#
#   just plan            every waypoint
#   just plan missing    only the unfinished ones
#

# What's in assets/projects, and what each project has.
projects:
    @python3 {{repo}}/scripts/projects.py {{assets}} {{project}}

# Recent job runs and their state.
jobs:
    docker compose exec -T generator prefect flow-run ls --limit 15

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
