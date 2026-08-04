# dreamworld_gs

Turn a 360° panorama into a navigable 3D Gaussian Splatting world, exported
for both **web viewing** (`.ply`) and **NVIDIA Isaac Sim** (`.usdz`, NuRec).

Built on [HY-World 2.0](https://github.com/Tencent-Hunyuan/HY-World-2.0)
(panorama → WorldNav trajectory planning → WorldStereo expansion → 3DGS) with
Isaac export via [3DGRUT](https://github.com/nv-tlabs/3dgrut)'s NuRec exporter.

## Quick start

```bash
just setup                              # one-time: ~300GB of weights + 2 images
just generate my_panorama.png office    # panorama -> world (offline)
just view                               # http://localhost:8081
```

Everything after `just setup` runs **fully offline** — containers mount model
weights from `assets/` and never reach the network.

## Requirements

- NVIDIA GPUs (4+ recommended; ~60GB VRAM for the expansion stage), driver
  supporting **CUDA 12.8**
- Docker with the NVIDIA container runtime
- [just](https://github.com/casey/just), [uv](https://docs.astral.sh/uv/)
  (weight downloads only)
- ~350GB disk

## Layout

Each image's build context is its own `docker/<name>/` directory — everything
copied into an image lives there, nothing is pulled from the repo root.

```
justfile                       all workflows (just --list)
docker/
  splat-generator/             HY-World pipeline image (GPU) — build context
    splat-generator.Dockerfile clones HY-World at a pinned commit + patches it
    hyworld.patch              our changes to upstream (offline, SAM3 paths)
    build_env.sh               env build, with upstream fixes documented
    generate.sh                the 6-stage pipeline
    tools/
      ply_to_isaac.py          3DGS PLY -> Isaac Sim NuRec USDZ
      threedgrut/              vendored 3dgrut export subtree (Apache 2.0)
  splat-viewer/                nginx + WebGL viewer image (CPU) — build context
    splat-viewer.Dockerfile
    nginx.conf
    www/                       vendored antimatter15/splat viewer
scripts/                       host-side only (see scripts/README.md)
  extract_sam3_image.py        derive SAM3 image model from video packaging
assets/                        gitignored, all large files
  models/                      SAM 3 weights (image + video packaging)
  hf/                          HuggingFace cache (HY-World, Qwen, WorldStereo)
  scenes/<name>/               panorama.png -> world.ply + world.usdz
```

The HY-World commit is pinned as `HYWORLD_REF` in the generator Dockerfile;
override it at build time with
`docker build --build-arg HYWORLD_REF=<sha> ...`.

## Usage

```bash
# generate: panorama must be 1920x960 equirectangular
just generate path/to/panorama.png office
#   -> assets/scenes/office/world.ply    (3DGS, ~50MB)
#   -> assets/scenes/office/world.usdz   (Isaac Sim NuRec, ~25MB)

just generate pano.png office --gpus 2   # fewer GPUs
just view                                # browse at :8081
just to-isaac some_other.ply             # convert any 3DGS PLY for Isaac
just stop                                # stop viewer + VLM
```

Viewing remotely: `ssh -L 8081:localhost:8081 <host>`, then open
`http://localhost:8081/?url=files/office/world.ply` in a **real browser tab**
(embedded IDE browsers abort large downloads). Drag to look, WASD to move.

The trajectory planner needs a VLM; `just generate` starts one automatically
(vLLM serving Qwen3-VL-8B on GPU 0) and leaves it running for subsequent
generations. `just stop` shuts it down.

## Notes on the pipeline

Six stages, all inside the generator container:

| Stage | What |
| --- | --- |
| 1. WorldNav | VLM picks targets, SAM 3 segments, navmesh plans obstacle-aware camera paths |
| 2. Render | point-cloud renders along those paths (multi-GPU) |
| 3. WorldStereo 2.0 | 17B diffusion generates consistent keyframes, expanding the world |
| 4. GS data | frames + aligned depth + normals + cameras |
| 5. 3DGS | gaussian training with MaskGaussian pruning |
| 6. Isaac export | PLY → NuRec USDZ |

**Why classic (not antialiased) 3DGS training**: `--antialiased` bakes
mip-splatting opacity compensation into the gaussians; they then render
correctly *only* in renderers applying the same compensation. Classic mode
keeps the PLY portable across SuperSplat, web viewers, and Isaac.

**SAM 3 weights**: `facebook/sam3` on HuggingFace is gated. `just fetch-sam3`
pulls the same weights from ModelScope (ungated), then derives the *image*
model variant the planner needs — upstream distributes only the video
packaging (`scripts/extract_sam3_image.py`). If you have HF access, you can
point `SAM3_IMAGE_DIR` / `SAM3_VIDEO_DIR` at official copies instead.

**Upstream patches** are recorded in `scripts/hyworld.patch` against the
commit in `scripts/hyworld.ref`; the environment fixes (glm vendoring,
tokenizers pin, cupy variant, setuptools constraint, undeclared `peft`) are
documented inline in `docker/splat-generator/build_env.sh`.
