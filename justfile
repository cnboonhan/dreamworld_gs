set shell := ["bash", "-euo", "pipefail", "-c"]
set dotenv-load := true

repo    := justfile_directory()
assets  := repo / "assets"
project := env_var_or_default("DW_PROJECT", "multilevel_office")

_default:
    @just --list

# Idempotent and cheap to re-run: a repo already in the cache is recognised
# without touching the network. The list is scripts/models.txt.
#
# Download every model the pipeline needs (~550GB, needs network).
fetch:
    HF_HOME={{assets}}/hf uv run --with huggingface_hub --with modelscope \
        --with safetensors --no-project \
        {{repo}}/scripts/fetch_assets.py {{assets}}

# Pack the WHOLE assets/projects/<project> into dist/ — maps, worlds, panos,
# splats, training intermediates, everything. Weights are separate (just fetch).
#
# Carry a project to another machine, entire.
pack proj=project dest="dist":
    @python3 {{repo}}/scripts/pack.py pack {{assets}} {{proj}} {{dest}}

# Restore a packed project, in place (merges into an existing one, with a note).
unpack FILE:
    @python3 {{repo}}/scripts/pack.py unpack {{repo}} {{FILE}}

_env:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{assets}}/projects
    grep -q "^DW_PROJECT=" {{repo}}/.env 2>/dev/null || \
        echo "DW_PROJECT={{project}}" >> {{repo}}/.env
    grep -q "^DW_UID=" {{repo}}/.env 2>/dev/null || \
        printf 'DW_UID=%s\nDW_GID=%s\n' "$(id -u)" "$(id -g)" >> {{repo}}/.env

# Rebuild the Gazebo world + nav graph from building.yaml (after map edits).
world: _env
    docker compose -f compose.full.yaml run --rm --no-deps rmfsim world {{project}}

# FULL: everything — authoring, simulation, and all four generators (8 GPUs).
up: _env
    docker compose -f compose.full.yaml up -d --build
    @echo "  http://localhost:8080                     every surface, one port"
    @echo "  http://localhost:8080/sim_editor          trace the building map"
    @echo "  http://localhost:8080/dreamworld_editor   grow the dreamworld"
    @echo "  http://localhost:8080/dreamworld_viewer   walk the dreamworld"
    @echo "  http://localhost:8080/rmfsim              the building under simulation"
    @echo "  http://localhost:8080/harness             drive it by tool call"

# MINIMAL: walk + simulate + command an already-generated dreamworld
# (1 GPU, for the mission agent's VLM). No generators, no traffic editor.
minimal: _env
    docker compose -f compose.minimal.yaml up -d --build
    @echo "  http://localhost:8080/dreamworld_viewer   walk the dreamworld"
    @echo "  http://localhost:8080/rmfsim              the building under simulation"
    @echo "  http://localhost:8080/harness             drive it by tool call"

# Pack the walkable demo into assets/projects/<project>/bundle as a
# SELF-CONTAINED static site: viewer + graph + every world's records +
# every crossing video, relative paths throughout — any static file host
# serves it (the panoramas never leave the dreamworld tree).
bundle proj=project:
    #!/usr/bin/env bash
    set -euo pipefail
    docker compose -f compose.full.yaml run --rm --no-deps dreamworld_editor \
        python bundle.py /projects/{{proj}}/bundle
    dest={{assets}}/projects/{{proj}}/bundle
    cp -r {{repo}}/docker/dreamworld_viewer/www/. "$dest/"
    sed -i 's|<script src="viewer.js|<script>window.DW_BASE={files:"files",graph:"graph.json"}</script>\n  <script src="viewer.js|' "$dest/index.html"
    echo "self-contained site at $dest — any static file server can host it"

# DEMO: the bundle behind one nginx — browser only, no backend, no GPU.
demo:
    docker compose -f compose.demo.yaml up -d --build
    @echo "  http://localhost:${DW_DEMO_PORT:-8080}/dreamworld_viewer   walk the bundle"
