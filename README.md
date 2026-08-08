# dreamworld_gs

Turn 360° panoramas into a navigable 3D Gaussian Splatting world, exported for
both **web viewing** (`.ply`) and **NVIDIA Isaac Sim** (`.usdz`, NuRec) — and
simulate the same building under [Open-RMF](https://www.open-rmf.org/), so the
splats can be indexed against the building's own traversal semantics.

Two halves that meet at the nav graph:

| | Built from | Gives you |
| --- | --- | --- |
| **RMF side** | an annotated floorplan (`maps/`) | the building simulated — doors, lifts, waypoints, the lanes between them |
| **splat side** | 360 photos of the real place (`panos/`) | what those places actually look like, as gaussians |

The nav graph is the contract between them: RMF says *where you can go and what
you have to open to get there*; the splats say *what it looks like when you do*.

## Projects

Everything about one building lives in `assets/projects/<project>/`:

```
maps/      the authored floorplan — <map>.building.yaml + its images
worlds/    generated from it: Gazebo world, models, nav_graphs/0.yaml
panos/     360 captures of the real place, one folder per scene
splats/    what those captures reconstruct into
```

So a project is one directory to copy, back up or hand over, and every service
mounts the same tree. One project is **active** at a time — `DW_PROJECT` in
`.env` selects it, and both `just` and a bare `docker compose up` read it:

```bash
just projects              # what's there; * marks the active one
just use multilevel_office # switch the stack
DW_PROJECT=htx just world  # or override for a single command
```

`just setup` seeds `multilevel_office` from `samples/` — a two-level building
with lifts — so there is something to open on a fresh checkout.

## The splat side

Two ways in, chosen by what you put in the project's `panos/`:

| Input | Pipeline | Geometry |
| --- | --- | --- |
| `panos/<scene>/` — a folder of panoramas of one space | **reconstruct**: reproject → SfM → gaussian splatting | **measured** from the parallax between standpoints |
| `panos/<scene>.jpg` — a single panorama | **generate**: [HY-World 2.0](https://github.com/Tencent-Hunyuan/HY-World-2.0) (WorldNav → WorldStereo → 3DGS) | **imagined** beyond the one vantage point |

Reconstruction is faithful to the real room but only knows what the cameras
saw — its quality tracks how many standpoints you capture. Generation fills a
whole walkable world from one photo, but everything outside that photo is
plausible invention. Isaac export in both cases uses
[3DGRUT](https://github.com/nv-tlabs/3dgrut)'s NuRec exporter.

## Quick start

```bash
just setup                # one-time: model weights + images (~500GB, needs network)
just up                   # start the stack, prints the URLs

# the RMF side: the sample project already has a map
just world                # -> worlds/multilevel_office/ (world + nav graph)

# the splat side: drop captures in, reconstruct them
mkdir -p assets/projects/multilevel_office/panos/cafe
cp *.jpg assets/projects/multilevel_office/panos/cafe/
just generate cafe        # -> splats/cafe/{world.ply,world.usdz}
just video cafe           # -> splats/cafe/walkthrough.mp4
```

Then open
`http://localhost:8081/?url=files/multilevel_office/splats/cafe/world.ply`,
and the simulated building at `http://localhost:8083`.

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
compose.yaml                   the seven services; reads DW_PROJECT
docker/
  splat-generator/             pipeline image (GPU) — build context
    splat-generator.Dockerfile clones HY-World at a pinned commit + patches it
    hyworld.patch              our changes to upstream (offline, SAM3 paths)
    build_env.sh               env build, with upstream fixes documented
    serve.py                   serves both pipelines as Prefect deployments
    flow.py                    generate: the 6-stage HY-World pipeline
    reconstruct.py             reconstruct: reproject -> SfM -> 3DGS -> export
    submit.py                  job client: submits and follows by polling
    tools/
      ply_to_isaac.py          3DGS PLY -> Isaac Sim NuRec USDZ
      threedgrut/              vendored 3dgrut export subtree (Apache 2.0)
      make_spawn_cam.py        writes world.cam.json + world.path.json
      render_video.py          walkthrough along the capture path
  splat-viewer/                nginx + WebGL splat viewer (CPU) — build context
    splat-viewer.Dockerfile
    nginx.conf
    www/                       vendored antimatter15/splat viewer, plus the tour
  pano-viewer/                 nginx + WebGL 360 viewer (CPU) — build context
    pano-viewer.Dockerfile
    nginx.conf
    www/                       equirect viewer for the input panoramas
  rmf-tools/                   RMF + Gazebo + traffic editor — build context
    rmf-tools.Dockerfile       FROM open-rmf/rmf_demos, plus a virtual display
    entrypoint.sh              one image, three roles: world / sim / editor
    with_display.sh            Xvfb -> x11vnc -> websockify -> noVNC
    generate_world.sh          building.yaml -> world + models + nav graph
    postprocess_world.py       SDF fixups the generator leaves behind
    sim.launch.xml.template    Gazebo (headless) + the RMF core nodes
samples/                       in-repo starter projects, seeded into assets/
  multilevel_office/maps/      two levels, two lifts, many doors
scripts/                       host-side only (see scripts/README.md)
  fetch_assets.py              downloads everything in models.txt
  extract_sam3_image.py        derive SAM3 image model from video packaging
assets/                        gitignored, all large files
  models/                      SAM 3 weights (image + video packaging)
  hf/                          HuggingFace cache (HY-World, Qwen, WorldStereo)
  prefect/                     job history database
  projects/<project>/          one building, everything about it
    maps/                      <map>.building.yaml + its floorplan images
    worlds/<map>/              <map>.world, models/, nav_graphs/, sim.launch.xml
    panos/<scene>/             input: the panoramas of one space
    splats/<scene>/            output: world.ply, world.usdz, world.cam.json,
                               world.path.json, walkthrough.mp4
```

The HY-World commit is pinned as `HYWORLD_REF` in the generator Dockerfile;
override it at build time with
`docker build --build-arg HYWORLD_REF=<sha> ...`.

## Usage

`just up` starts seven services (see `compose.yaml`) and leaves them running —
nothing else needs launching by hand:

| Service | Role |
| --- | --- |
| `prefect` | job queue + UI on :4200 — run history, per-stage logs, retries |
| `vlm` | Qwen3-VL on GPU 0: picks navigation targets, captions rendered views |
| `generator` | waits for jobs, runs the pipeline on the remaining GPUs |
| `viewer` | WebGL splat viewer, every world at once, on :8081 |
| `panoviewer` | 360 viewer for every project's input panoramas, on :8082 |
| `rmfsim` | the active project's building simulated under RMF, on :8083 |
| `editor` | the traffic editor — author that map — on :8084 |

```bash
just projects                  # what's there; * marks the active project
just use htx                   # point the whole stack at another project
just world                     # maps/ -> worlds/ (world + nav graph)
just generate cafe             # panos/cafe/ -> splats/cafe/
just generate cafe 1.5         # ...with 1.5m between standpoints
just video cafe                # walkthrough mp4 along the capture path
just jobs                      # recent runs and their state
just down                      # stop everything
```

Every recipe defaults to the active project; pass `proj=<name>` (or set
`DW_PROJECT`) to target another one for a single command.

`generate` skips a scene that already has a `world.ply`; delete the scene
directory to rebuild it.

A generate job takes ~12 minutes on 4x H200; reconstruct is faster and scales
with panorama count. `just generate` streams progress, but the work happens in
the generator service — Ctrl-C detaches without cancelling, and you can follow
or retry the run in the Prefect UI.

Viewing remotely:

```bash
ssh -L 4200:localhost:4200 -L 8081:localhost:8081 -L 8082:localhost:8082 \
    -L 8083:localhost:8083 -L 8084:localhost:8084 <host>
```

Then open
`http://localhost:8081/?url=files/<project>/splats/<scene>/world.ply` in a
**real browser tab** (embedded IDE browsers abort large downloads).

## The tour

Each world carries two sidecars written at export time, and the viewer picks
both up automatically:

- `world.cam.json` — a spawn pose taken from one of the scene's own training
  cameras, so the scene opens upright at eye level rather than at an arbitrary
  angle.
- `world.path.json` — the capture path: the straight line fitted through the
  standpoints the panoramas were shot from.

When a scene has a path, the viewer **opens in tour mode and starts playing**:
the camera glides along that line and back, and drag looks around from it like
a 360 viewer — yaw and pitch about the scene's own up vector, so the horizon
never rolls however far you swing. The bar at the bottom pauses, scrubs, and
sets the speed.

This is the default because it is the honest view. The capture line is the only
part of the world any camera actually observed; off it nothing constrained the
geometry, and that is where a splat looks its worst. So you leave the path
deliberately: any of WASD/arrows, a right-drag pan, or the scroll wheel hands
control back to free flight, as does the **free** button.

The tour follows exactly the line `just video` renders, to within rounding — so
what you see gliding is what the walkthrough shows.

## The RMF side

`maps/<map>.building.yaml` is an [RMF building
map](https://github.com/open-rmf/rmf_traffic_editor): a floorplan image with
walls, doors, lifts and a nav graph drawn on top of it.

```bash
just world                     # -> worlds/<map>/
```

That produces the Gazebo world, its models, `sim.launch.xml`, and —
the piece that matters beyond the simulation — `nav_graphs/0.yaml`.

**Simulate it** at `http://localhost:8083`. The simulation itself runs
headless; what you see is the Gazebo GUI attached to it as a viewer, so closing
the tab costs nothing. Alongside Gazebo it runs the RMF core: the traffic
schedule, blockade and mutex-group supervisors, the task dispatcher, and the
building map server. That is what makes doors and lifts *addressable* rather
than merely present:

```bash
docker compose exec rmfsim bash -lc '. /rmf_demos_ws/install/setup.bash
  ros2 topic pub --once /door_requests rmf_door_msgs/msg/DoorRequest \
    "{requester_id: me, door_name: main_door, requested_mode: {value: 2}}"
  ros2 topic echo /door_states'
```

On the sample map that includes the lifts:

```bash
docker compose exec rmfsim bash -lc '. /rmf_demos_ws/install/setup.bash
  ros2 topic pub --once /lift_requests rmf_lift_msgs/msg/LiftRequest \
    "{lift_name: lift1, session_id: me, request_type: 1,
      destination_floor: L11, door_state: 2}"
  ros2 topic echo /lift_states'
```

**Edit it** at `http://localhost:8084` — the traffic editor, on the same map
directory, so a save there is picked up by the next `just world`.

Both are Qt applications with no headless mode, so each runs on a private X
server inside its container and publishes that screen as a web page
(Xvfb → x11vnc → websockify → noVNC). Rendering is software: passing a GPU to a
GLX application in a container needs a real X server on the host, and these are
floorplan-scale scenes.

### Why both halves

A gaussian splat knows what a corridor looks like but nothing about it — where
one room ends, which opening is a door, what is on the other side. The nav graph
knows exactly that and nothing about appearance. Capturing panoramas at the nav
graph's own waypoints, and walking its lanes, gives every splat a place in the
building rather than a private coordinate frame.

## Notes on the pipeline

**reconstruct** — four stages. Each panorama is reprojected into 12 pinhole
views (an 8-yaw ring at eye level plus 4 tilted up and down, 100° FOV so
neighbours share features), COLMAP recovers poses across all of them, gsplat
optimises against the posed views, then the export step runs. Needs two or more
overlapping panoramas.

SfM is scale-free, so the world is rescaled to metres from the known distance
between consecutive standpoints — the `spacing` argument, default 0.5 m. Pass
`0` to leave it unitless. A simulator needs the metric scale and nothing else
recovers it.

**generate** — six stages:

| Stage | What |
| --- | --- |
| 1. WorldNav | VLM picks targets, SAM 3 segments, navmesh plans obstacle-aware camera paths |
| 2. Render | point-cloud renders along those paths (multi-GPU) |
| 3. WorldStereo 2.0 | 17B diffusion generates consistent keyframes, expanding the world |
| 4. GS data | frames + aligned depth + normals + cameras |
| 5. 3DGS | gaussian training with MaskGaussian pruning |
| 6. Export | PLY → NuRec USDZ, plus the spawn camera and tour path |

**Capture coverage dominates everything.** On our own rooms, going from 3 to 7
standpoints took one scene from 16.6 to 28.8 dB PSNR — a far larger effect than
any training knob we tried. Two rules of thumb:

- Scale spacing to the room. 0.5 m works for a small room; a large atrium needs
  2–3 m, because parallax is what SfM measures, and a 0.5 m baseline against
  10–20 m walls is only about 3% of the depth.
- Bare, glossy, or glass surfaces give SfM nothing to match. Expect
  fragmentation and low PSNR in spaces that are mostly white wall and window,
  and capture more densely there.

Watch the registered-image count the reconstruct job logs: well under the number
of reprojected views means the model fragmented, and only the largest fragment
gets trained.

**Why classic (not antialiased) 3DGS training**: `--antialiased` bakes
mip-splatting opacity compensation into the gaussians; they then render
correctly *only* in renderers applying the same compensation. Classic mode keeps
the PLY portable across SuperSplat, web viewers, and Isaac.

**SAM 3 weights**: `facebook/sam3` on HuggingFace is gated. `just fetch-assets`
pulls the same weights from ModelScope (ungated), then derives the *image* model
variant the planner needs — upstream distributes only the video packaging
(`scripts/extract_sam3_image.py`). If you have HF access, you can point
`SAM3_IMAGE_DIR` / `SAM3_VIDEO_DIR` at official copies instead.

**Upstream patches** live in `docker/splat-generator/hyworld.patch`, applied to
the commit pinned as `HYWORLD_REF`; see `scripts/README.md` for what each one
fixes. Environment fixes (glm vendoring, tokenizers pin, cupy variant,
setuptools constraint, undeclared `peft`/`rtree`, and the CUDA-arch settings
that `docker build` needs because it exposes no GPU) are documented inline in
`docker/splat-generator/build_env.sh`.

Third-party code and model licenses: see `NOTICE.md`.
