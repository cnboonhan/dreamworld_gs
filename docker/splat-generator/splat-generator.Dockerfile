# Panoramas of a real place -> a measured 3DGS world + Isaac Sim USDZ.
#
# Build context is this directory — everything the image needs is here. No
# pretrained weights of any kind: this pipeline measures geometry from the
# photographs rather than imagining it, so nothing is downloaded at runtime and
# nothing is mounted from assets/ but the projects themselves.
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TORCH_CUDA_ARCH_LIST=9.0 \
    MAX_JOBS=32 \
    OPENCV_IO_ENABLE_OPENEXR=1 \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential ninja-build cmake patch \
        libgl1 libglib2.0-0 libegl1 libgles2 libosmesa6 \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY build_env.sh /tmp/build_env.sh
RUN bash /tmp/build_env.sh

COPY tools/ /opt/tools/
COPY reconstruct.py video.py route.py serve.py submit.py /opt/

# Prefect orchestrates the stages: per-stage logs and timing in its UI, and a
# failed run can be retried from the stage that broke.
RUN uv pip install --python /opt/venv/bin/python "prefect==3.8.1" \
    && chmod -R a+rX /opt/venv


WORKDIR /opt
ENTRYPOINT ["python"]
CMD ["serve.py"]
