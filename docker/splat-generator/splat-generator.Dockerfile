# HY-World 2.0 world generation: panorama -> navigable 3DGS + Isaac Sim USDZ.
#
# Build context is this directory — everything the image needs is here.
# HY-World itself is cloned at a pinned commit and patched (see hyworld.patch,
# documented in ../../scripts/README.md).
#
# At runtime everything is mounted from assets/ and no network is used:
# model weights in /assets, scenes in /workspace/scenes.
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04

ARG HYWORLD_REPO=https://github.com/Tencent-Hunyuan/HY-World-2.0
ARG HYWORLD_REF=7f668e67c74338d50684e57be46a438459b6bbe1

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TORCH_CUDA_ARCH_LIST=9.0 \
    MAX_JOBS=32 \
    # headless rendering (pyrender/pyglet have no display in a container)
    PYOPENGL_PLATFORM=egl \
    PYGLET_HEADLESS=true \
    OPENCV_IO_ENABLE_OPENEXR=1 \
    # everything offline: weights are mounted, never fetched
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HOME=/assets/hf \
    SAM3_IMAGE_DIR=/assets/models/sam3_image \
    SAM3_VIDEO_DIR=/assets/models/sam3_video \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential ninja-build cmake patch \
        libgl1 libglib2.0-0 libegl1 libgles2 libosmesa6 \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Upstream at a pinned commit, plus our patches (offline loading, SAM3 paths)
COPY hyworld.patch /tmp/hyworld.patch
RUN git clone "$HYWORLD_REPO" /opt/hyworld \
    && cd /opt/hyworld \
    && git checkout --quiet "$HYWORLD_REF" \
    && git submodule update --init --recursive --depth 1 \
    && patch -p1 < /tmp/hyworld.patch \
    && rm -rf .git

WORKDIR /opt/hyworld
COPY build_env.sh /tmp/build_env.sh
RUN bash /tmp/build_env.sh

COPY tools/ /opt/tools/
COPY flow.py /opt/flow.py

# Prefect orchestrates the six stages: per-stage logs/timing in its UI, and a
# failed run can be retried from the stage that broke.
RUN uv pip install --python /opt/venv/bin/python "prefect==3.8.1" \
    && chmod -R a+rX /opt/venv

WORKDIR /workspace
ENTRYPOINT ["python", "/opt/flow.py"]
CMD ["serve"]
