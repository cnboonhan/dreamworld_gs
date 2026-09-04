# qwen — Qwen-Image-Edit-2509 behind a tiny HTTP server, for panorama
# variants. LIGHTWEIGHT image: deps and the server, no weights; weights
# resolve out of the mounted HF cache at runtime and nothing reaches the
# network.
#
# ONE file, two bases, keyed by TARGETARCH:
#   amd64  pytorch/pytorch 2.5.1-cu124 — the H200 box, unchanged.
#   arm64  nvidia/cuda 13.0 + torch cu130 wheels — pytorch/pytorch
#          publishes amd64 only, and a GB300 (sm_103) has no cu124
#          kernels anyway. A venv keeps bare `pip`/`python` meaning what
#          they meant on the pytorch base (Ubuntu 24.04 is PEP 668).
ARG TARGETARCH

FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime AS qwen-amd64
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

FROM nvidia/cuda:13.0.1-cudnn-runtime-ubuntu24.04 AS qwen-arm64
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 python3-venv \
        libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# cu130 to match the driver and GB300's sm_103; same torch story as the
# streamer's arm64 branch
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cu130 \
        torch==2.11.0 torchvision==0.26.0

FROM qwen-${TARGETARCH:-amd64}
RUN pip install --no-cache-dir \
    "diffusers>=0.36.0" \
    "transformers>=4.56.0,<5" \
    "accelerate>=1.2.0" \
    "bitsandbytes>=0.45.0" \
    "sentencepiece" \
    "protobuf" \
    "opencv-python-headless" \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.32.0" \
    "python-multipart" \
    "Pillow" \
    "numpy"

ENV HF_HUB_OFFLINE=1
ENV PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
# torch >=2.6 resolves its cache dir through getpass.getuser() at IMPORT,
# and compose runs this as the calling uid — which on this cluster comes
# from SSSD, not /etc/passwd, so there is no passwd entry to find and no
# file to mount that would supply one. Naming the dirs skips the lookup
# entirely; the streamer image already does exactly this.
ENV TORCHINDUCTOR_CACHE_DIR=/tmp/inductor TRITON_CACHE_DIR=/tmp/triton

WORKDIR /app
COPY qwen/qwen_server.py /app/server.py

EXPOSE 8000

CMD ["python", "server.py"]
