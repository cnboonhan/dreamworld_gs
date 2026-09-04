# HY-World 2.0 world generation: panorama -> navigable 3DGS + Isaac Sim USDZ,
# behind the one-job HTTP queue in server.py.
#
# ONE self-contained build: this used to be two images — a splat-generator
# base built from a directory the repo later deleted, and a thin wrapper
# FROM it. A fresh machine could not build the wrapper because nothing
# could build the base. The base's build now lives here, verbatim; the
# wrapper is the last two instructions.
#
# HY-World is cloned at a pinned commit and patched (hyworld.patch: offline
# loading, SAM3 paths). At runtime everything is mounted from assets/ and
# no network is used: model weights in /assets, scenes under /projects.
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
COPY splat-generator/hyworld.patch /tmp/hyworld.patch
RUN git clone "$HYWORLD_REPO" /opt/hyworld \
    && cd /opt/hyworld \
    && git checkout --quiet "$HYWORLD_REF" \
    && git submodule update --init --recursive --depth 1 \
    && patch -p1 < /tmp/hyworld.patch \
    && rm -rf .git

WORKDIR /opt/hyworld
COPY splat-generator/build_env.sh /tmp/build_env.sh
RUN bash /tmp/build_env.sh

COPY splat-generator/tools/ /opt/tools/
COPY splat-generator/flow.py splat-generator/serve.py splat-generator/submit.py /opt/

# Prefect orchestrates the six stages: per-stage logs/timing in its UI, and a
# failed run can be retried from the stage that broke.
RUN uv pip install --python /opt/venv/bin/python "prefect==3.8.1" \
    && chmod -R a+rX /opt/venv


COPY splatgen/server.py /opt/server.py

EXPOSE 8000
WORKDIR /opt
ENTRYPOINT ["python"]
CMD ["server.py"]
