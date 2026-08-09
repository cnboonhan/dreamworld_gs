#!/bin/bash
# Build the reconstruction env inside the image.
#
# This used to be built on top of HY-World's requirements, because the image
# carried HY-World for the generative path. Reconstruction never used any of
# it — except gsplat, which was installed from HY-World's vendored
# `gsplat_maskgaussian` fork and so held the whole tree in place. That fork is
# upstream gsplat 1.5.3 plus a `gauss_masks` argument nothing here passes, so
# the dependency was packaging, not function.
#
# What remains is what the pipeline actually imports, measured rather than
# assumed: torch, gsplat, pycolmap, opencv, torchmetrics for SSIM, and the
# handful of packages the vendored 3DGRUT exporter loads on its way to a USDZ.
#
# Ordering and pins here encode fixes for real problems:
#  - torch cu128 first, so nothing replaces it with a PyPI default build
#  - setuptools<81 as a build constraint (pkg_resources removed in 82+)
#  - gsplat cloned recursively: its CUDA sources need the glm submodule
set -exo pipefail

echo "setuptools<81" > /tmp/build-constraints.txt
export UV_BUILD_CONSTRAINT=/tmp/build-constraints.txt

# No GPU is visible during `docker build`, so an extension that probes
# torch.cuda.is_available() would silently build CPU-only and fail at runtime.
export CUDA_HOME=/usr/local/cuda
export FORCE_CUDA=1
export CUDA_ARCHITECTURES=90

# uv's managed interpreters default to /root (mode 0700); the container runs
# as the invoking user, so put them somewhere world-readable
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
uv venv /opt/venv --python 3.11
UVP="uv pip install --python /opt/venv/bin/python"

$UVP ninja "setuptools<81" wheel packaging
$UVP torch==2.7.1 --index-url https://download.pytorch.org/whl/cu128

# pycolmap 3.12 for rigs and frames. 3.10 models every image as an independent
# camera — but the views reprojected from one panorama share an optical centre
# by construction, and 3.10 gives no way to say so. The solver then slides them
# along their own viewing axes: measured, 0.3-0.9 m of drift within a single
# standpoint, more than the distance between standpoints, so the recovered walk
# was noise. Rigs landed in 3.12.
$UVP "pycolmap==3.12.6"

# The rasteriser. Cloned with submodules because the CUDA sources include glm,
# which a plain `pip install git+...` would leave empty, and built here rather
# than JIT-compiled on first use so a job does not pay for it.
git clone --depth 1 --branch v1.5.3 --recursive \
    https://github.com/nerfstudio-project/gsplat /opt/gsplat
$UVP --no-build-isolation /opt/gsplat

# reconstruction and video
$UVP numpy scipy opencv-python-headless pillow plyfile pyyaml \
     torchmetrics imageio-ffmpeg

# the vendored 3DGRUT exporter's path to an Isaac Sim USDZ
$UVP usd-core msgpack "nvidia-ncore>=19.0.0" \
     omegaconf rich tqdm universal-pathlib dataclasses-json zstandard

# readable by any UID (the container runs as the invoking user)
chmod -R a+rX /opt/venv /opt/uv-python
