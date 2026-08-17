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
