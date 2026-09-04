#!/bin/bash
# Build the HY-World python env inside the image.
#
# Ordering and pins here encode fixes for upstream issues:
#  - torch cu128 first, and stripped from requirements.txt, so nothing
#    replaces it with a PyPI default build
#  - cupy -> cupy-cuda12x (plain cupy is a source build)
#  - setuptools<81 as a build constraint (pkg_resources removed in 82+)
#  - tokenizers==0.22.1 (transformers 5.2 breaks CLIP tokenizer load)
#  - glm vendored for the gsplat_maskgaussian CUDA build (upstream omits it)
#  - peft needed by the pano LoRA path but undeclared upstream
set -exo pipefail

echo "setuptools<81" > /tmp/build-constraints.txt
export UV_BUILD_CONSTRAINT=/tmp/build-constraints.txt

# No GPU is visible during `docker build`, so extensions that probe
# torch.cuda.is_available() (notably pytorch3d) would silently build CPU-only
# and fail at runtime with "Not compiled with GPU support".
export CUDA_HOME=/usr/local/cuda
export FORCE_CUDA=1
# fused-ssim reads CUDA_ARCHITECTURES, not TORCH_CUDA_ARCH_LIST, and its
# no-GPU fallback list (75/80/89) omits Hopper -> "no kernel image is
# available for execution on the device" at runtime
export CUDA_ARCHITECTURES=90
# newer than the commit requirements_git.txt pins; see below
FUSED_SSIM_REF=a7c48d6dd7ac6dc39a7958c7c4452e0b10418f38

# uv's managed interpreters default to /root (mode 0700); the container runs
# as the invoking user, so put them somewhere world-readable
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
uv venv /opt/venv --python 3.11
UVP="uv pip install --python /opt/venv/bin/python"

$UVP ninja "setuptools<81" wheel packaging
$UVP torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1 \
     --index-url https://download.pytorch.org/whl/cu128

sed -e 's/^cupy==/cupy-cuda12x==/' -e '/^torch==/d' -e '/^torchvision==/d' \
    requirements.txt > /tmp/requirements_docker.txt
$UVP -r /tmp/requirements_docker.txt
# undeclared upstream: peft (pano LoRA), rtree (trimesh spatial index used by
# the navmesh/trajectory stage)
$UVP "tokenizers==0.22.1" peft==0.18.1 rtree

# pycolmap 3.12 for rigs and frames. Upstream pins 3.10, which models every
# image as an independent camera — but the views reprojected from one panorama
# share an optical centre by construction, and 3.10 gives no way to say so. The
# solver then slides them along their own viewing axes: measured, 0.3-0.9 m of
# drift within a single standpoint, more than the distance between standpoints,
# so the recovered walk was noise. Rigs landed in 3.12.
$UVP "pycolmap==3.12.6"

# CUDA extension: gsplat with MaskGaussian pruning (needs glm headers)
GLM_DIR=hyworld2/worldgen/third_party/gsplat_maskgaussian/gsplat/cuda/csrc/third_party
mkdir -p "$GLM_DIR"
if [ ! -f "$GLM_DIR/glm/glm/glm.hpp" ]; then
    rm -rf "$GLM_DIR/glm"
    git clone --depth 1 --branch 1.0.1 https://github.com/g-truc/glm "$GLM_DIR/glm"
fi
$UVP --no-build-isolation ./hyworld2/worldgen/third_party/gsplat_maskgaussian

$UVP --no-build-isolation flash-attn==2.7.4.post1

# Two entries are handled outside requirements_git.txt:
#  - spz (niantic .spz codec): its CMake fetches a pinned zlib tarball whose
#    URL now 404s. Imported lazily and only under --convert_to_spz, which
#    this pipeline never passes.
#  - fused-ssim: the pinned commit picks its CUDA arch by probing for a GPU
#    (absent during docker build) and falls back to sm_75/80/89, so Hopper
#    kernels are missing at runtime. A newer commit honours
#    CUDA_ARCHITECTURES; same one-function API.
grep -vE "spz\.git|fused-ssim" requirements_git.txt > /tmp/requirements_git_docker.txt
$UVP --no-build-isolation -r /tmp/requirements_git_docker.txt
$UVP --no-build-isolation \
    "git+https://github.com/rahul-goel/fused-ssim@${FUSED_SSIM_REF}"
$UVP --no-build-isolation ./hyworld2/worldgen/third_party/navmesh

# Isaac Sim (NuRec USDZ) export path
$UVP usd-core msgpack "nvidia-ncore>=19.0.0"

# readable by any UID (the container runs as the invoking user)
chmod -R a+rX /opt/venv /opt/uv-python
