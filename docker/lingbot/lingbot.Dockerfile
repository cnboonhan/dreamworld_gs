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
WORKDIR /srv
COPY lingbot/server.py /srv/server.py
ENV PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 UV_CACHE_DIR=/tmp/uv
EXPOSE 8000
CMD ["/opt/flashdreams/.venv/bin/python", "/srv/server.py"]
