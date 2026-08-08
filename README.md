# dreamworld_gs

Two halves of one building. **RMF** simulates it from an annotated floorplan —
doors, lifts, waypoints, the lanes between them. **Gaussian splatting**
reconstructs what it actually looks like, from 360 photos of the real place.
They meet at the nav graph.

Everything runs on this box, offline, behind `docker compose`.

## Layout

One project is one building, and one directory:

```
assets/projects/<project>/
  maps/            <map>.building.yaml + floorplans             (you author)
  worlds/<map>/    <map>.world, models/, nav_graphs/0.yaml,
                   capture_plan.json                            (generated)
  panos/<id>/      360 captures of one corridor                 (you shoot)
  splats/<id>/     world.ply, world.usdz, world.cam.json,
                   world.path.json, walkthrough.mp4             (generated)
```

Panoramas live under the place they photograph, so a splat is addressed by
where it is in the building rather than by a name someone invented. See
[naming](#naming) for the ids.

The rest of the repo:

```
justfile                 every workflow — just --list
compose.yaml             the seven services; reads DW_PROJECT from .env
samples/                 starter projects, seeded into assets/ by `just setup`
docker/
  splat-generator/       reconstruct + generate + render-video flows (GPU)
  splat-viewer/          WebGL splat viewer
  pano-viewer/           360 viewer for the input panoramas
  rmf-tools/             RMF + Gazebo + traffic editor + the build-world flow
scripts/                 host-side model downloads
assets/                  gitignored: model weights, job history, projects/
```

## Running it

```bash
just setup      # one-time: model weights + images (~500GB, needs network)
just up         # start everything, print the URLs
```

Seven services come up and stay up:

| | |
| --- | --- |
| http://localhost:4200 | jobs, logs, retries (Prefect) |
| http://localhost:8081 | splat viewer |
| http://localhost:8082 | 360 viewer for input panoramas |
| http://localhost:8083 | the building simulated under RMF (Gazebo, over noVNC) |
| http://localhost:8084 | the traffic editor — author the map (over noVNC) |

(Eight services: those five plus the VLM and the two job workers.)

Remotely:

```bash
ssh -L 4200:localhost:4200 -L 8081:localhost:8081 -L 8082:localhost:8082 \
    -L 8083:localhost:8083 -L 8084:localhost:8084 <this-host>
```

Open splats in a **real browser tab** — embedded IDE browsers abort the
download partway.

One project is active at a time:

```bash
just projects              # what's there; * marks the active one
just use multilevel_office # switch the whole stack
DW_PROJECT=htx just world  # or override for a single command
```

`DW_PROJECT` lives in `.env`, so `just` and a bare `docker compose up` agree.

Requirements: NVIDIA GPUs (4+, ~60GB VRAM for generation) with CUDA 12.8, the
NVIDIA container runtime, [just](https://github.com/casey/just),
[uv](https://docs.astral.sh/uv/), ~350GB disk.

## The pipeline

Every operation is a Prefect job. Nothing does real work outside one, and the
justfile is only a wrapper — it submits and follows:

```
just <recipe>  ->  submit.py  ->  Prefect (:4200)  ->  the worker that can do it
```

**One run produces one thing**, and is named for it, so the queue reads as a
list of splats and worlds rather than random adjectives:

| Job | Runs in | Stages | Produces |
| --- | --- | --- | --- |
| `build-world` | `worldjobs` | generate → inspect → plan | one world + nav graph + capture plan |
| `capture-edge` | `worldjobs` | capture | one corridor's panoramas |
| `reconstruct-world` | `generator` | reproject → SfM → gaussian splatting → export | one splat, measured |
| `generate-world` | `generator` | 6 HY-World stages | one splat, imagined |
| `render-video` | `generator` | plan path → render → encode | one walkthrough |
| `plan-route` | `generator` | route → resolve → write | one route through the building |

Two workers, because the work needs different machines: world generation wants
Gazebo and `rmf_building_map_tools`, the splat flows want CUDA. Each is served
from the image that has what it needs, and both register with the same Prefect
server — so it is still one queue.

At :4200 you get per-stage timing and logs, retries, and the parameters a run
was submitted with. `build-world` also publishes its nav graph as a table
there: levels, waypoints, lanes, and which lanes cross a door.

```bash
just jobs                  # recent runs and their state
just plan                  # what the map defines vs what has been captured
```

Ctrl-C stops following; the job keeps running.

## Making things

**A world**, from the project's map:

```bash
just world
```

`maps/<map>.building.yaml` → `worlds/<map>/`: the Gazebo world, its models,
`sim.launch.xml`, and `nav_graphs/0.yaml`. The sim also generates it on first
start if it is missing, so a fresh checkout comes up with something to look at.

**Panoramas of a corridor, without walking it.** The simulated building can be
photographed, so the whole pipeline runs with no camera:

```bash
just capture L11.cafe--v7        # -> panos/L11.cafe--v7/000.png ...
just capture L11.cafe--v7 9      # denser
just capture-all                 # every corridor not yet shot, one job each
```

A camera stands at points along the lane and takes a full 360 at each. It
writes **panoramas and nothing else** — no poses, no positions, no marker that
it came from a simulator — because a synthetic run that leaked what a real
capture cannot would be testing the pipeline under conditions it never faces.

**A splat**, from panoramas of one corridor:

```bash
just generate L11.cafe--v7       # panos/<id>/ -> splats/<id>/
just generate L11.cafe--v7 1.5   # 1.5m between standpoints (default 0.5)
```

A *folder* of panoramas is reconstructed together — reproject → COLMAP →
gaussian splatting — and is faithful to the real room. A *single image file*
takes the generative HY-World path instead: one real vantage point, the rest
imagined. Output is `world.ply` (web) and `world.usdz` (Isaac Sim, NuRec).

The spacing argument is the distance you walked between standpoints. SfM is
scale-free, so this is the only thing that puts the world in metres; pass `0`
to leave it unitless.

`generate` skips a scene that already has a `world.ply` — delete the scene
directory to rebuild.

**A video**, along the capture path:

```bash
just video cafe             # -> splats/cafe/walkthrough.mp4
just video cafe 40          # longer
just video cafe 20 spline   # visit each standpoint exactly
```

One scene per job — build them one at a time and each gets its own run to
inspect, retry or compare.

## Moving a project

A project is self-contained — its map, its world, its panoramas, its splats —
so a tarball of one is everything another machine needs. Model weights are not
included; those come from `just setup` on the far side.

```bash
just bundle                 # the active project -> dist/
just bundle htx /tmp        # a named project, somewhere else
just unbundle <file>        # restore, in place, on the other node
```

Paths are stored as `assets/projects/<name>/...`, so an unbundle lands exactly
where the stack looks. `unbundle` warns before merging into a project that
already exists.

## Naming

Every part of the building gets an id, and its panoramas go in the folder of
that name. `build-world` computes them and writes
`worlds/<map>/capture_plan.json`; `just plan` reads it back.

**Corridors are what you capture.** A walk from `a` to `b` starts and ends on
the two vertices, so the vertex views are already in it — and the capture
burden halves, 26 walks instead of 53 places. Junctions come out better than a
dedicated capture would: `L11.lift_lobby` has four corridors meeting it, so
four walks each contribute a standpoint there from a different direction.

| | id | goes in |
| --- | --- | --- |
| a corridor between two waypoints | `<level>.<a>--<b>` | `panos/L11.cafe--v7/` |
| its endpoints, named | `<level>.<name>` | — already in the corridor |
| its endpoints, unnamed | `<level>.v<index>` | — already in the corridor |

Three decisions worth knowing:

**The level is always part of the id.** Waypoint names are not unique across a
building — the sample map has a `lift_lobby` on L1 and another on L11, one
directly above the other. Two different places must not share a folder.

**Unnamed vertices fall back to their index.** Most vertices are unnamed
corners with no other handle. The index comes from the nav graph, so it can
shift if you insert vertices in the traffic editor.

**An edge is one corridor, not two.** Every lane in these graphs is
bidirectional, so the endpoints are sorted and the edge gets one name whichever
way you walked it. The level is written once, since lanes never cross levels.

```bash
just plan            # every id, what is captured for it, and how good it came out
just plan missing    # only what still needs photographing
just capture L11.cafe--v7     # photograph it in sim
just generate L11.cafe--v7    # panos/<id>/ -> splats/<id>/
```

`just plan` ends with a table of every splat already built, so two of them can
be compared without opening two flow runs:

```
built splats
  id        panos  reg/views  gaussians    PSNR   scale      MB  video
  L11.cafe      4      48/48    778,449   27.58  0.1238    52.9  yes   (2 fragments)
```

Each splat records these in `world.info.json` at export. What to watch:
**reg/views** well under half means SfM fragmented and only the largest piece
was trained; **PSNR** is measured on held-out views, so it says the splat
matches the photographs — not that the room is covered, which is what the
capture count tells you; **scale** is the multiplier that put it in metres.

`generate` reads `panos/<id>/` and writes `splats/<id>/`, so the id is the only
name you ever type.

## Walking the building

Reconstructing a corridor now places it where it belongs. A COLMAP solve is
metric in scale but arbitrary in origin and orientation, so the `align` stage
solves the rigid transform that puts it in the building's frame and rewrites
`world.ply` there. Three constraints, all from the capture itself:

| | |
| --- | --- |
| the walk axis | → the lane's direction |
| the camera up | → the building's up |
| the walk centre | → the lane's midpoint |

`world.info.json` records the transform and `align_residual_m`, how far the
walk's own endpoints land from the lane's. What the capture *cannot* say is
which end you started from — the axis fits the lane just as well backwards — so
that comes from the id by convention (`L11.cafe--v7` means walked cafe → v7),
overridable with a `capture.json` next to the panoramas.

With every corridor in one frame, a route is just data:

```bash
just route cafe playpen      # -> traversals/L11.cafe__L11.playpen.route.json
```

That holds the path through the building in metres and which splat covers which
stretch of it. Nothing is rendered: the viewer follows the polyline with its
tour parameter and loads each splat as it reaches it, dropping the one behind.

**Not finished yet.** The viewer still opens one splat at a time; streaming
along a route is the remaining piece.


## Notes

- **Capture coverage dominates quality.** 3 → 7 standpoints took one room from
  16.6 to 28.8 dB PSNR, far more than any training knob. Scale spacing to the
  room: 0.5m for a small one, 2–3m for an atrium, since a 0.5m baseline against
  10–20m walls is only ~3% parallax. Bare white walls and glass give SfM
  nothing to match.
- **Watch the registered-image count** the reconstruct job logs. Well under the
  number of reprojected views means SfM fragmented, and only the largest
  fragment is trained.
- **Splats train in classic, not antialiased, mode** so the PLY stays portable
  across SuperSplat, web viewers and Isaac.
- **SAM 3 weights** come from ModelScope; `facebook/sam3` on HuggingFace is
  gated. See `scripts/README.md`.
- **Upstream patches** to HY-World live in `docker/splat-generator/hyworld.patch`;
  environment fixes are documented inline in `build_env.sh`.

Third-party code and model licenses: see `NOTICE.md`.
