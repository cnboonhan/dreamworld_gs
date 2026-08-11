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
    # Serialised. Every recipe that submits a job depends on this, so several
    # called together each call it too — and two `docker compose up` racing on
    # the same container leave one with "removal already in progress" and a
    # failed job. It is a no-op once everything is running, which is why this
    # only bites after a restart.
    exec 9>>{{repo}}/.up.lock
    flock 9
    docker compose up -d --wait
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

