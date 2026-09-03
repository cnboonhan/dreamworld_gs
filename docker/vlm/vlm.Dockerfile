# vlm — Qwen3-VL behind the harness at /harness, on stock vLLM with one
# part replaced.
#
# vLLM 0.11.0 vendors its own ptxas, built from CUDA 12.8, and that release
# predates this card: a GB300 is sm_103a, and 12.8's ptxas stops at sm_101a
# ("Value 'sm_103a' is not defined for option 'gpu-name'"). Triton detects
# the architecture correctly, asks for it, and the engine dies compiling its
# first kernel. --enforce-eager does not avoid it: the call comes from an
# attention backend, not from the inductor path eager mode skips.
#
# So: keep the vLLM image exactly as published and hand its Triton a newer
# assembler. TRITON_PTXAS_PATH is Triton's own supported override, so
# nothing about vLLM is patched.
#
# 12.9 specifically, not 13.0. Triton 3.4.0's ptx_get_version() maps CUDA
# majors 10, 11 and 12 and raises on anything else, so a 13.0 ptxas trades
# the sm_103a error for "Triton only support CUDA 10.0 or higher, but got
# CUDA version: 13.0". 12.9 is the release that added sm_103 while still
# being a major Triton will parse — the only version that satisfies both.
#
# Delete this image and go back to `image: vllm/vllm-openai:<tag>` in
# compose once a release ships a ptxas that knows sm_103.
FROM nvidia/cuda:12.9.1-cudnn-devel-ubuntu24.04 AS cudaptx

FROM vllm/vllm-openai:v0.11.0

COPY --from=cudaptx /usr/local/cuda/bin/ptxas /opt/cuda-ptxas/bin/ptxas

# Fail the build here rather than at first inference if the swap regresses.
RUN /opt/cuda-ptxas/bin/ptxas --version | grep -q "release 12.9" \
    && printf '.version 8.0\n.target sm_90\n.address_size 64\n' > /tmp/probe.ptx \
    && /opt/cuda-ptxas/bin/ptxas --gpu-name=sm_103a /tmp/probe.ptx -o /tmp/probe.o \
    && rm -f /tmp/probe.ptx /tmp/probe.o

ENV TRITON_PTXAS_PATH=/opt/cuda-ptxas/bin/ptxas
