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
  maps/            <map>.building.yaml + its floorplan images   (you author)
  worlds/<map>/    <map>.world, models/, nav_graphs/0.yaml      (generated)
  panos/<scene>/   360 captures of one space                    (you shoot)
  splats/<scene>/  world.ply, world.usdz, world.cam.json,
                   world.path.json, walkthrough.mp4             (generated)
```

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
| `build-world` | `worldjobs` | generate → inspect → publish | one world + nav graph |
| `reconstruct-world` | `generator` | reproject → SfM → gaussian splatting → export | one splat, measured |
| `generate-world` | `generator` | 6 HY-World stages | one splat, imagined |
| `render-video` | `generator` | plan path → render → encode | one walkthrough |

Two workers, because the work needs different machines: world generation wants
Gazebo and `rmf_building_map_tools`, the splat flows want CUDA. Each is served
from the image that has what it needs, and both register with the same Prefect
server — so it is still one queue.

At :4200 you get per-stage timing and logs, retries, and the parameters a run
was submitted with. `build-world` also publishes its nav graph as a table
there: levels, waypoints, lanes, and which lanes cross a door.

```bash
just jobs                  # recent runs and their state
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

**A splat**, from panoramas of one space:

```bash
just generate cafe          # panos/cafe/ -> splats/cafe/
just generate cafe 1.5      # 1.5m between standpoints (default 0.5)
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

## The goal: a splat per vertex, a traversal per lane

This is where the two halves join, and it is **not built yet** — today a scene
is named whatever you call it, and its tour path is a straight line fitted
through wherever the panoramas happened to be shot.

The nav graph already has the structure to key against. Vertices carry names,
and lanes know which door they cross:

```yaml
vertices: [28.52, -13.59, {name: cafe}]        # a place you can stand
lanes:    [0, 1, {door_name: lift_lobby_north_door}]  # a way between two
```

On the sample building those named vertices are `cafe`, `playpen`,
`apex_lab`, `lift_lobby_north`, `lift_lobby_south`, `gantry_entrance` — the
same places the existing captures cover.

The intended correspondence:

| nav graph | capture | splat |
| --- | --- | --- |
| **vertex** — somewhere you can stand | one 360 panorama, shot there | contributes a standpoint |
| **lane** — a way between two vertices | the walk along it | the segment the tour plays |
| **lane with a `door_name`** | the door you open partway | where a traversal is gated |

So: shoot one panorama per named vertex, name it after that vertex, and a
scene becomes a set of vertices rather than an unlabelled point cloud. Then
`world.path.json` follows the actual lane polyline instead of a fitted line —
which also fixes a real loss today, where a curved walk projected onto a
straight axis gives up about a third of its length.

What that buys: a route planned in RMF (Dijkstra over the lanes, opening the
doors it crosses) can be rendered from the splats, because both sides agree on
what a place is and what connects it.

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
