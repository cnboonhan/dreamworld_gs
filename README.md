# dreamworld_gs

Turn 360° panoramas into a navigable 3D Gaussian Splatting world, exported for
both **web viewing** (`.ply`) and **NVIDIA Isaac Sim** (`.usdz`, NuRec).

Two ways in, chosen by what you put in `assets/panos/`:

| Input | Pipeline | Geometry |
| --- | --- | --- |
| `<name>/` — a folder of panoramas of one space | **reconstruct**: reproject → SfM → gaussian splatting | **measured** from the parallax between standpoints |
| `<name>.jpg` — a single panorama | **generate**: [HY-World 2.0](https://github.com/Tencent-Hunyuan/HY-World-2.0) (WorldNav → WorldStereo → 3DGS) | **imagined** beyond the one vantage point |

Reconstruction is faithful to the real room but only knows what the cameras
saw — its quality tracks how many standpoints you capture. Generation fills a
whole walkable world from one photo, but everything outside that photo is
plausible invention. Isaac export in both cases uses
[3DGRUT](https://github.com/nv-tlabs/3dgrut)'s NuRec exporter.

## Quick start

```bash
just setup                # one-time: model weights + images (~500GB, needs network)
just up                   # start the stack: prefect, vlm, generator, viewer

cp my_pano.png assets/panos/
just generate my_pano     # -> assets/scenes/my_pano/{world.ply,world.usdz}
just serve my_pano        # inspect it faithfully at http://localhost:8081
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
compose.yaml                   the four services
docker/
  splat-generator/             HY-World pipeline image (GPU) — build context
    splat-generator.Dockerfile clones HY-World at a pinned commit + patches it
    hyworld.patch              our changes to upstream (offline, SAM3 paths)
    build_env.sh               env build, with upstream fixes documented
    flow.py                    the 6-stage pipeline, as a Prefect flow
    tools/
      ply_to_isaac.py          3DGS PLY -> Isaac Sim NuRec USDZ
      threedgrut/              vendored 3dgrut export subtree (Apache 2.0)
      serve_splat.py           gsplat rendering streamed to a browser
      render_video.py          walkthrough along the capture path
  splat-viewer/                nginx + WebGL splat viewer (CPU) — build context
    splat-viewer.Dockerfile
    nginx.conf
    www/                       vendored antimatter15/splat viewer
  pano-viewer/                 nginx + WebGL 360 viewer (CPU) — build context
    pano-viewer.Dockerfile
    nginx.conf
    www/                       equirect viewer for generation inputs
scripts/                       host-side only (see scripts/README.md)
  fetch_assets.py              downloads everything in models.txt
  extract_sam3_image.py        derive SAM3 image model from video packaging
assets/                        gitignored, all large files
  models/                      SAM 3 weights (image + video packaging)
  hf/                          HuggingFace cache (HY-World, Qwen, WorldStereo)
  panos/                       input: equirectangular panoramas you drop here
  scenes/<name>/               output: world.ply + world.usdz + world.cam.json
  prefect/                     job history database
```

The HY-World commit is pinned as `HYWORLD_REF` in the generator Dockerfile;
override it at build time with
`docker build --build-arg HYWORLD_REF=<sha> ...`.

## Usage

`just up` starts four services (see `compose.yaml`):

| Service | Role |
| --- | --- |
| `prefect` | job queue + UI on :4200 — run history, per-stage logs, retries |
| `vlm` | Qwen3-VL on GPU 0: picks navigation targets, captions rendered views |
| `generator` | waits for jobs, runs the pipeline on the remaining GPUs |
| `viewer` | WebGL splat viewer, every world at once, on :8080 |
| `panoviewer` | 360 viewer for the input panoramas in `assets/panos/` on :8082 |

Two ways to look at a world, and they answer different questions:

| | `viewer` (:8080) | `just serve <scene>` (:8081) |
| --- | --- | --- |
| Where it draws | your browser, from the ply | this GPU, frames streamed |
| Motion | smooth — no round-trip | as smooth as the network |
| Fidelity | WebGL, approximate sorting | the rasteriser that trained it |
| Scenes | all of them | one, and it holds a GPU |
| Camera | free flight | rides the capture path, view free |

Use :8080 to move around, :8081 when you need to trust what you are seeing.
For motion with neither compromise, render a walkthrough: `just video <scene>`.

```bash
just panos                     # what's available to build from
just panoview                  # inspect the panoramas in 360 at :8082
just generate office           # assets/panos/office{,.jpg} -> assets/scenes/office
just generate office lobby_v2  # ...into a differently named scene
just jobs                      # recent runs and their state
just down                      # stop everything
```

`generate` skips a scene that already has a `world.ply`; delete the scene
directory to rebuild it.

A job takes ~12 minutes on 4x H200. `just generate` streams progress, but the
work happens in the generator service — Ctrl-C detaches without cancelling,
and you can follow or retry the run in the Prefect UI.

Viewing remotely: `ssh -L 8081:localhost:8081 <host>`, then open
`http://localhost:8081/?url=files/office/world.ply` in a **real browser tab**
(embedded IDE browsers abort large downloads). Drag to look, WASD to move.
Each world carries a `world.cam.json` spawn pose taken from one of its own
training cameras, so it opens upright rather than at an arbitrary angle.

## Notes on the pipeline

Six stages, all inside the generator container:

**reconstruct** — four stages: each panorama is reprojected into 24 pinhole
views (an 8-yaw ring plus tilts, 90° FOV so neighbours overlap), COLMAP
recovers poses across all of them, gsplat optimises against the posed views,
then the same export step runs. Needs two or more overlapping panoramas.

**generate** — six stages:

| Stage | What |
| --- | --- |
| 1. WorldNav | VLM picks targets, SAM 3 segments, navmesh plans obstacle-aware camera paths |
| 2. Render | point-cloud renders along those paths (multi-GPU) |
| 3. WorldStereo 2.0 | 17B diffusion generates consistent keyframes, expanding the world |
| 4. GS data | frames + aligned depth + normals + cameras |
| 5. 3DGS | gaussian training with MaskGaussian pruning |
| 6. Export | PLY → NuRec USDZ, plus the viewer spawn camera |

**Why classic (not antialiased) 3DGS training**: `--antialiased` bakes
mip-splatting opacity compensation into the gaussians; they then render
correctly *only* in renderers applying the same compensation. Classic mode
keeps the PLY portable across SuperSplat, web viewers, and Isaac.

**SAM 3 weights**: `facebook/sam3` on HuggingFace is gated. `just fetch-assets`
pulls the same weights from ModelScope (ungated), then derives the *image*
model variant the planner needs — upstream distributes only the video
packaging (`scripts/extract_sam3_image.py`). If you have HF access, you can
point `SAM3_IMAGE_DIR` / `SAM3_VIDEO_DIR` at official copies instead.

**Upstream patches** live in `docker/splat-generator/hyworld.patch`, applied
to the commit pinned as `HYWORLD_REF`; see `scripts/README.md` for what each
one fixes. Environment fixes (glm vendoring, tokenizers pin, cupy variant,
setuptools constraint, undeclared `peft`/`rtree`, and the CUDA-arch settings
that `docker build` needs because it exposes no GPU) are documented inline in
`docker/splat-generator/build_env.sh`.

Third-party code and model licenses: see `NOTICE.md`.
