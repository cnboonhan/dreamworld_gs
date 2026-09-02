# lingbot — LingBot-World (camera-controllable I2V) served through NVIDIA's
# FlashDreams runtime, behind the same three endpoints every generator in
# this stack wears.
#
# The torch pin is load-bearing and was paid for once already: FlashDreams'
# own lockfile resolves torch cu130, this box's driver is CUDA 12.8, and the
# mismatch fails as "driver too old" — or worse, as an unrelated torchaudio
# import error that makes every runner vanish from the registry.
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

# the integration's own workspace sync, then the driver-matched torch trio
RUN uv sync --project integrations/lingbot
RUN uv pip install --python .venv/bin/python \
        --index-url https://download.pytorch.org/whl/cu128 \
        torch==2.9.1 torchvision==0.24.1 torchaudio==2.9.1

# NOT beside /opt/flashdreams: the workspace is installed EDITABLE, and a
# sibling directory named flashdreams shadows it as a namespace package —
# whichever of CWD or the script's own directory lands on sys.path first
# wins, and `import flashdreams` then finds a directory with no modules in
# it. /srv has no such neighbour.
# The checkpoint (~70GB) is baked IN, so the image is self-contained on a
# box with no assets tree. Baked at the same path the compose mount uses:
# where assets/hf IS mounted it simply shadows this copy, and either way
# the runtime never reaches the network.
ARG LINGBOT_REPO=robbyant/lingbot-world-fast
RUN HF_HOME=/assets/hf HF_HUB_OFFLINE=0 \
    /opt/flashdreams/.venv/bin/python -c \
    "from huggingface_hub import snapshot_download as d; d('${LINGBOT_REPO}')" \
    && du -sh /assets/hf

WORKDIR /srv
COPY lingbot/server.py /srv/server.py
# Inductor compiles a small CUDA helper at first run: it needs Python.h
# (python3-dev, above — without it the first generate() dies in a gcc
# subprocess rather than anywhere near the model) and it links -lcuda,
# whose real .so.1 the NVIDIA runtime injects but whose LINK-time stub
# lives in the toolkit's stubs dir that gcc does not search by default.
ENV LIBRARY_PATH=/usr/local/cuda/lib64/stubs
ENV PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 UV_CACHE_DIR=/tmp/uv \
    TRITON_CACHE_DIR=/tmp/triton TORCHINDUCTOR_CACHE_DIR=/tmp/inductor
EXPOSE 8000
CMD ["/opt/flashdreams/.venv/bin/python", "/srv/server.py"]
