# qwen-server — Qwen-Image-Edit-2509, the model behind the panorama editor.
# Ported verbatim from dreamworld/docker/qwen_server; only the weight path
# differs, because this repo already keeps a HuggingFace cache at assets/hf and
# there is no reason to hold the 54 GB twice.
#
# Apache-2.0 model; bf16 needs ~45GB VRAM, so it wants a card of its own — see
# DW_EDIT_GPU in compose.yaml, which keeps it off the ones world generation uses.
#
# LIGHTWEIGHT image: deps and the server, no weights.
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

# Weights mounted read-only at /opt/model (NOT baked). Runs fully offline.
ENV QWEN_EDIT_MODEL=/opt/model
ENV HF_HUB_OFFLINE=1
ENV PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

WORKDIR /app
COPY pano-editor/qwen_server.py /app/server.py

EXPOSE 8000

CMD ["python", "server.py"]
