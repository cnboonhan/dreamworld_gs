# qwen — Qwen-Image-Edit-2509, the model behind panorama variants.
# Ported verbatim from main's pano-editor stack; the server is unchanged.
#
# Apache-2.0 model; bf16 needs ~45GB VRAM, so it wants a card of its own —
# see DW_EDIT_GPU in compose.yaml.
#
# LIGHTWEIGHT image: deps and the server, no weights. Weights resolve out of
# the mounted HF cache at runtime; nothing reaches the network.
#
# NOT on pytorch/pytorch: that repository publishes amd64 only — every tag,
# the 2.5.1-cuda12.4 one this used to sit on included — so on aarch64 there
# is nothing to pull. NVIDIA's CUDA image is multi-arch, and torch comes
# from the wheel index on top of it.
FROM nvidia/cuda:13.0.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 python3-venv \
        libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

# Ubuntu 24.04's python is externally managed (PEP 668) and pip refuses to
# install into it. A venv first on PATH keeps bare `pip` and `python` meaning
# what they meant on the pytorch base — wangen builds FROM this image and
# calls both unqualified.
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# cu130 to match the driver (CUDA 13.0) and GB300's sm_103, which the old
# cu124 build shipped no kernels for. Versions are the earliest coherent
# pair with aarch64 wheels: torchvision 0.24, the partner to the old
# torch 2.9.1, was never built for aarch64 in any CUDA index. Same
# version as the streamer, so the stack tells one torch story.
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cu130 \
        torch==2.11.0 torchvision==0.26.0

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
