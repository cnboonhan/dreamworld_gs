# streamer — the viewer's live layer: a streaming image-to-video model
# behind the same three endpoints every generator in this stack wears.
#
# Two families are installed and DW_STREAMER_RUNNER picks between them:
# LingBot-World (camera-controllable, 14B, and the v2 causal-fast
# checkpoint is distilled specifically to suppress long-horizon drift)
# and Causal-Forcing Wan 2.1 1.3B I2V (no camera to steer, an eleventh
# the size). The server asks the pipeline which one it is by signature.
# ONE file, both boxes: the H200 host (CUDA 12.8 driver, sm_90) and the
# GB300 host (CUDA 13.0 driver, sm_103) — arm64 here IS the GB300, so the
# toolkit and the torch stack key off TARGETARCH. Each branch is exactly
# what ran on its box before the files merged.
ARG TARGETARCH
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04 AS cuda-amd64
FROM nvidia/cuda:13.0.1-cudnn-devel-ubuntu24.04 AS cuda-arm64
FROM cuda-${TARGETARCH:-amd64}
ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates ffmpeg python3 python3-venv curl \
        python3-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# PINNED, no longer `main`: upstream #557 moved integrations/lingbot into
# a v2 tree on 2026-09-03, and an unpinned clone stopped building that
# same day. This sha is the rev the working image was built from —
# migrating to the v2 layout is its own task, not a side effect of a
# cache miss.
ARG FLASHDREAMS_REF=04558b14473ff082460a9a9ed88ec0d140faf4f6
RUN git clone https://github.com/NVIDIA/flashdreams /opt/flashdreams \
    && git -C /opt/flashdreams checkout --quiet ${FLASHDREAMS_REF}
WORKDIR /opt/flashdreams

RUN uv sync --project integrations/lingbot
RUN uv sync --package flashdreams-causal-forcing --inexact
# LAST, and after every sync: uv's own resolution has to be overridden so
# the three stay a matched set — a mismatch surfaces as "driver too old",
# or worse as a torchaudio import error that empties the runner registry.
#   amd64: cu128, the H200 driver's CUDA — torch 2.9.1 and its partners.
#   arm64: cu130 — the GB300 driver IS 13.0 and sm_103 has no cu128
#          kernels; 2.11.0 because torchvision 0.24.1 / torchaudio 2.9.1
#          were never built for aarch64 in any CUDA index, and it is the
#          version flashdreams' own lock targets, so transformer-engine —
#          built from source against the resolved torch — keeps its ABI.
RUN case "${TARGETARCH:-amd64}" in \
      arm64) uv pip install --python .venv/bin/python \
               --index-url https://download.pytorch.org/whl/cu130 \
               torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0 ;; \
      *)     uv pip install --python .venv/bin/python \
               --index-url https://download.pytorch.org/whl/cu128 \
               torch==2.9.1 torchvision==0.24.1 torchaudio==2.9.1 ;; \
    esac

# The weights bake in, so the image is self-contained on a box with no
# assets tree — 22GB all told, against 70GB for a camera model. Baked at
# the path the compose mount uses, so a present mount simply shadows it
# and either way the runtime never reaches the network.
RUN HF_HOME=/assets/hf HF_HUB_OFFLINE=0 /opt/flashdreams/.venv/bin/python -c "\
from huggingface_hub import snapshot_download as s, hf_hub_download as f; \
f('zhuhz22/Causal-Forcing', 'framewise/causal_forcing.pt'); \
s('Wan-AI/Wan2.1-T2V-1.3B'); \
f('lightx2v/Autoencoders', 'Wan2.1_VAE.pth')" \
    && du -sh /assets/hf

# NOT beside /opt/flashdreams: the workspace is installed EDITABLE, and a
# sibling directory named flashdreams shadows it as a namespace package.
WORKDIR /srv
COPY streamer/server.py /srv/server.py
# Inductor compiles a CUDA helper on the first generate(): it needs
# Python.h (python3-dev, above) and links -lcuda, whose link-time stub
# gcc does not search for by default.
ENV LIBRARY_PATH=/usr/local/cuda/lib64/stubs
# Those compiles cost ~75s on the first generate() and land in the
# container's writable layer, so every `up --force-recreate` paid for them
# again. Point them at /cache, which compose backs with a host directory
# that outlives the container. Created world-writable so the image still
# runs unmounted (as any uid), just without the saving.
RUN mkdir -p /cache/triton /cache/inductor && chmod -R 777 /cache
ENV PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 UV_CACHE_DIR=/tmp/uv \
    TRITON_CACHE_DIR=/cache/triton TORCHINDUCTOR_CACHE_DIR=/cache/inductor
EXPOSE 8000
CMD ["/opt/flashdreams/.venv/bin/python", "/srv/server.py"]
