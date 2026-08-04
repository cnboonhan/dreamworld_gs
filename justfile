# dreamworld_gs — panorama -> navigable 3DGS world (+ Isaac Sim USDZ)
#
#   just setup                       one-time: fetch weights + build images
#   just up                          start the stack (prefect, vlm, generator, viewer)
#   just generate mypano.png office  submit a job (offline, ~12 min)
#   just ui                          Prefect UI: run history, per-stage logs
#   just view                        browse generated worlds
#
# Services live in compose.yaml; these recipes drive them.

set shell := ["bash", "-euo", "pipefail", "-c"]

repo   := justfile_directory()
assets := repo / "assets"
gpus   := "4"
steps  := "2000"

# compose runs containers as you, not root, so outputs stay writable.
# Also written to .env so plain `docker compose ...` behaves the same.
export DW_UID := `id -u`
export DW_GID := `id -g`

_default:
    @just --list

# Everything needed to run offline: model weights + both images (~500GB).
setup: _env fetch-assets build

_env:
    @mkdir -p {{assets}}/prefect {{assets}}/scenes
    @printf 'DW_UID=%s\nDW_GID=%s\n' "$(id -u)" "$(id -g)" > {{repo}}/.env

# Download all models into assets/ (idempotent; list in scripts/models.txt).
fetch-assets:
    HF_HOME={{assets}}/hf uv run --with huggingface_hub --with modelscope \
        --with safetensors --no-project \
        {{repo}}/scripts/fetch_assets.py {{assets}}

# Build the generator and viewer images.
build:
    docker compose build

# Start the whole stack. The VLM and generator take a few minutes to load.
up: _env
    docker compose up -d --wait
    @echo "prefect ui : http://localhost:4200"
    @echo "viewer     : http://localhost:8081"

# Stop everything.
down:
    docker compose down

# Submit a job: 1920x960 equirectangular panorama in, world out. Streams
# progress; ^C detaches without cancelling (watch it in the UI instead).
generate pano scene: up
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{assets}}/scenes/{{scene}}
    cp {{pano}} {{assets}}/scenes/{{scene}}/panorama.png
    docker compose exec -T generator prefect deployment run \
        generate-world/dreamworld --watch \
        -p scene=/workspace/scenes/{{scene}} \
        -p gpus={{gpus}} -p steps={{steps}}
    @echo "-> {{assets}}/scenes/{{scene}}/world.ply (+ .usdz, .cam.json)"

# Recent job runs and their state.
jobs:
    docker compose exec -T generator prefect flow-run ls --limit 15

# Prefect UI (run history, per-stage logs, retries).
ui port="4200":
    @echo "http://localhost:{{port}}  (remote: ssh -L {{port}}:localhost:{{port}} <this-host>)"

# Browse generated worlds (each opens at its tagged spawn camera).
view port="8081":
    @echo "http://localhost:{{port}}/?url=files/<scene>/world.ply"
    @echo "  (remote: ssh -L {{port}}:localhost:{{port}} <this-host>)"
