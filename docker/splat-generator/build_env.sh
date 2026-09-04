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
# available for execution on the device" at runtime.
#
# 100 alongside 90 for Blackwell. A GB300 is sm_103, which CUDA 12.8's
# nvcc does not know -- it stops at sm_101 -- but sm_103 runs sm_100 code,
# so the 10.0 cubin serves it, the same way torch's own sm_100 kernels do.
export CUDA_ARCHITECTURES="${CUDA_ARCHITECTURES:-90;100}"
# newer than the commit requirements_git.txt pins; see below
FUSED_SSIM_REF=a7c48d6dd7ac6dc39a7958c7c4452e0b10418f38

# uv's managed interpreters default to /root (mode 0700); the container runs
# as the invoking user, so put them somewhere world-readable
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
uv venv /opt/venv --python 3.11
UVP="uv pip install --python /opt/venv/bin/python"

$UVP ninja "setuptools<81" wheel packaging
# torch MUST come from the cu128 index: PyPI's aarch64 torch gates every
# nvidia-* dependency on platform_machine == "x86_64", so it is CPU-only
# there and everything downstream would silently build without CUDA.
#
# torchvision and torchaudio are the other way round. The pytorch indexes
# publish NO aarch64 wheel for 0.22.1 / 2.7.1 (the earliest they carry is
# 0.25.0, which pairs with torch 2.10), while PyPI has both for cp311.
# Taking them from PyPI keeps torch at the version the pinned HY-World
# expects instead of dragging it forward three releases. What is lost is
# torchvision's compiled CUDA ops, which this pipeline does not call:
# it uses torchvision.transforms alone, and torchaudio only ever reaches
# _is_package_available.
$UVP torch==2.7.1 --index-url https://download.pytorch.org/whl/cu128
if [ "$(uname -m)" = "aarch64" ]; then
    $UVP torchvision==0.22.1 torchaudio==2.7.1
else
    $UVP torchvision==0.22.1 torchaudio==2.7.1 \
         --index-url https://download.pytorch.org/whl/cu128
fi

# Three requirements have no aarch64 distribution at all -- no wheel and
# no sdist, at any version -- so on this arch they come out of the file.
# Only decord needed replacing:
#
#   pycolmap    used only by worldrecon/hyworldmirror, behind a
#               save_colmap flag. The six stages this image runs all
#               live in hyworld2/worldgen, which never imports it.
#   pymeshlab   stage 5 does import it, but lazily and inside a
#               try/except (ImportError, OSError) whose fallback is the
#               same quadric decimation through Open3D. Upstream wrote
#               that path; dropping the package just takes it.
#   decord      a TOP-LEVEL import in worldgen/src/general_utils, so it
#               cannot simply go: the module would not load. It is used
#               for one function though -- the final frame of a video, by
#               negative index -- so the shim below answers it with
#               OpenCV, which that same file already uses for everything
#               else. eva-decord does not help: its arm64 wheels are
#               macOS.
DROP=""
if [ "$(uname -m)" = "aarch64" ]; then
    DROP="-e /^pycolmap==/d -e /^pymeshlab==/d -e /^decord/d"
fi
sed -e 's/^cupy==/cupy-cuda12x==/' -e '/^torch==/d' -e '/^torchvision==/d' \
    $DROP requirements.txt > /tmp/requirements_docker.txt
$UVP -r /tmp/requirements_docker.txt
# undeclared upstream: peft (pano LoRA), rtree (trimesh spatial index used by
# the navmesh/trajectory stage)
$UVP "tokenizers==0.22.1" peft==0.18.1 rtree

if [ "$(uname -m)" = "aarch64" ]; then
    SITE=$(/opt/venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
    cp /tmp/decord_shim.py "$SITE/decord.py"
    /opt/venv/bin/python -c "import decord; print('decord shim in place:', decord.VideoReader)"
fi

# pycolmap 3.12 for rigs and frames. Upstream pins 3.10, which models every
# image as an independent camera — but the views reprojected from one panorama
# share an optical centre by construction, and 3.10 gives no way to say so. The
# solver then slides them along their own viewing axes: measured, 0.3-0.9 m of
# drift within a single standpoint, more than the distance between standpoints,
# so the recovered walk was noise. Rigs landed in 3.12.
# ...on x86. There is no aarch64 build of pycolmap at any version -- no
# wheel and no sdist -- and it cannot be shimmed the way decord was: it is
# a binding onto COLMAP's C++ solver, not a thin reader. Nothing here
# reaches it, though. It is imported in one function,
# _save_colmap_lightweight, which runs only when --save_colmap is passed;
# that flag defaults to False and the World Mirror command retrieval_wm.py
# builds does not pass it. If it ever does, this arch will say so with an
# ImportError naming the module rather than failing obscurely.
if [ "$(uname -m)" != "aarch64" ]; then
    $UVP "pycolmap==3.12.6"
fi

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
# usd-core (the pxr module) publishes x86_64, macOS and Windows wheels
# and no sdist -- this repo's own export tests already say so, skipping
# on "usd-core (pxr) is only available on linux x86_64". Building OpenUSD
# from source for one optional artifact is not worth it: stage 6 writes
# world.ply, world.usdz and a spawn camera, and only the ply is consumed
# -- splatgen's server judges a job by world.ply existing. So on aarch64
# the USD export is skipped and flow.py says so at the point it happens.
# msgpack has aarch64 wheels and nvidia-ncore is pure python.
$UVP msgpack "nvidia-ncore>=19.0.0"
if [ "$(uname -m)" != "aarch64" ]; then
    $UVP usd-core
fi

# readable by any UID (the container runs as the invoking user)
chmod -R a+rX /opt/venv /opt/uv-python
