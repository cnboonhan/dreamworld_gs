# streamer — the viewer's live layer: a streaming image-to-video model
# behind the same three endpoints every generator in this stack wears.
#
# Two families are installed and DW_STREAMER_RUNNER picks between them:
# LingBot-World (camera-controllable, 14B, and the v2 causal-fast
# checkpoint is distilled specifically to suppress long-horizon drift)
# and Causal-Forcing Wan 2.1 1.3B I2V (no camera to steer, an eleventh
# the size). The server asks the pipeline which one it is by signature.
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates ffmpeg python3 python3-venv curl \
        python3-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

ARG FLASHDREAMS_REF=main
RUN git clone --depth 1 --branch ${FLASHDREAMS_REF} \
        https://github.com/NVIDIA/flashdreams /opt/flashdreams
WORKDIR /opt/flashdreams

RUN uv sync --project integrations/lingbot
RUN uv sync --package flashdreams-causal-forcing --inexact
# LAST, and after every sync: uv resolves torch cu130 and this box's
# driver is CUDA 12.8. The mismatch surfaces as "driver too old", or
# worse as a torchaudio import error that empties the runner registry.
RUN uv pip install --python .venv/bin/python \
        --index-url https://download.pytorch.org/whl/cu128 \
        torch==2.9.1 torchvision==0.24.1 torchaudio==2.9.1

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
ENV PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 UV_CACHE_DIR=/tmp/uv \
    TRITON_CACHE_DIR=/tmp/triton TORCHINDUCTOR_CACHE_DIR=/tmp/inductor
EXPOSE 8000
CMD ["/opt/flashdreams/.venv/bin/python", "/srv/server.py"]
