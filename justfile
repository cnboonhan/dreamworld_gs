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
    docker compose run --rm --no-deps rmfsim world {{project}}

# Start everything behind the one forwarded port.
up: _env
    docker compose up -d --build --remove-orphans
    @echo "  http://localhost:8080                     every surface, one port"
    @echo "  http://localhost:8080/sim_editor          trace the building map"
    @echo "  http://localhost:8080/dreamworld_editor   grow the dreamworld"
    @echo "  http://localhost:8080/dreamworld_viewer   walk the dreamworld"
    @echo "  http://localhost:8080/rmfsim              the building under simulation"
    @echo "  http://localhost:8080/harness             drive it by tool call"
