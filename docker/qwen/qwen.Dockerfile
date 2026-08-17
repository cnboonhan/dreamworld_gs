# qwen — Qwen-Image-Edit-2509, the model behind panorama variants.
# Ported verbatim from main's pano-editor stack; the server is unchanged.
#
# Apache-2.0 model; bf16 needs ~45GB VRAM, so it wants a card of its own —
# see DW_EDIT_GPU in compose.yaml.
#
# LIGHTWEIGHT image: deps and the server, no weights. Weights resolve out of
# the mounted HF cache at runtime; nothing reaches the network.
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

RUN apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

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

WORKDIR /app
COPY qwen/qwen_server.py /app/server.py

EXPOSE 8000

CMD ["python", "server.py"]
