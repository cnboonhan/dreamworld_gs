# dreamworld_gs — panorama -> navigable 3DGS world (+ Isaac Sim USDZ)
#
#   just setup                       one-time: fetch weights + build images
#   just up                          start everything, print the URLs
#   just generate office             assets/panos/office/ -> a world
#   just video office                walkthrough mp4 along the capture path
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
    @mkdir -p {{assets}}/prefect {{assets}}/scenes {{assets}}/panos
    @printf 'DW_UID=%s\nDW_GID=%s\n' "$(id -u)" "$(id -g)" > {{repo}}/.env

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
    @echo "  jobs + logs   http://localhost:4200"
    @echo "  worlds        http://localhost:8081/?url=files/<scene>/world.ply"
    @echo "  panoramas     http://localhost:8082"
    @echo
    @echo "  remote? ssh -L 4200:localhost:4200 -L 8081:localhost:8081 \\"
    @echo "              -L 8082:localhost:8082 <this-host>"

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
# Build a world from assets/panos/<name>.
generate pano scene="" spacing="0.5": up
    #!/usr/bin/env bash
    set -euo pipefail
    src=$(ls -d {{assets}}/panos/{{pano}} {{assets}}/panos/{{pano}}.* 2>/dev/null | head -1 || true)
    if [ -z "$src" ]; then
        echo "no such panorama or folder: {{pano}}" >&2
        echo "available in assets/panos:" >&2
        ls {{assets}}/panos 2>/dev/null | sed 's/^/  /' >&2
        exit 1
    fi
    scene="{{scene}}"
    [ -n "$scene" ] || scene=$(basename "${src%.*}")
    if [ -f {{assets}}/scenes/"$scene"/world.ply ]; then
        echo "$scene: already built, skipping"
        echo "   (delete assets/scenes/$scene to rebuild)"
        exit 0
    fi
    mkdir -p {{assets}}/scenes/"$scene"

    if [ -d "$src" ]; then
        n=$(ls "$src" | wc -l)
        echo "reconstructing $scene from $n panoramas"
        docker compose exec -T generator python submit.py \
            reconstruct-world/dreamworld \
            scene=/workspace/scenes/"$scene" \
            panos=/workspace/panos/"$(basename "$src")" \
            spacing={{spacing}}
    else
        cp "$src" {{assets}}/scenes/"$scene"/_input.${src##*.}
        # the generative pipeline reads panorama.png
        docker compose exec -T generator python -c "
        from pathlib import Path
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        d = Path('/workspace/scenes/$scene')
        src = next(p for p in d.iterdir() if p.stem == '_input')
        Image.open(src).convert('RGB').save(d / 'panorama.png')
        src.unlink()"
        docker compose exec -T generator python submit.py \
            generate-world/dreamworld \
            scene=/workspace/scenes/"$scene" \
            gpus={{gpus}} steps={{steps}}
    fi
    echo "-> assets/scenes/$scene/world.ply (+ .usdz, .cam.json)"

# Render a walkthrough video following the capture path.
#
# The camera splines through the standpoints the panoramas were shot from,
# because that is where the scene was actually observed. `just video h2rc 40`
# for a longer one.
#
# Render a walkthrough video following the capture path.
video scene seconds="20":
    docker compose exec -T generator python tools/render_video.py \
        /workspace/scenes/{{scene}} --seconds {{seconds}}
    @echo "-> assets/scenes/{{scene}}/walkthrough.mp4"

# List the panoramas available to generate from.
panos:
    @ls {{assets}}/panos 2>/dev/null || echo "none yet — drop equirectangular images in assets/panos/"

# Recent job runs and their state.
jobs:
    docker compose exec -T generator prefect flow-run ls --limit 15

