# dreamworld_gs

Two halves of one building. **RMF** simulates it from an annotated floorplan —
doors, lifts, waypoints, the lanes between them. **Gaussian splatting** shows
what it actually looks like, generated from one 360 photo taken at each
waypoint. They meet at the nav graph: a waypoint is a place you photograph, and
the lanes leaving it are the ways you can walk out.

Everything runs on this box, offline, behind `docker compose`.

## Layout

One project is one building, and one directory:

```
assets/projects/<project>/
  maps/            <map>.building.yaml + floorplans             (you author)
  worlds/<map>/    <map>.world, models/, nav_graphs/0.yaml,
                   capture_plan.json                            (generated)
  panos/<id>.jpg   one 360, taken standing at that waypoint     (you shoot)
  splats/<id>/     world.ply, world.usdz, world.cam.json,
                   world.paths.json                             (generated)
```

A panorama is named for the place it was taken, so a splat is addressed by
where it is in the building rather than by a name someone invented. See
[naming](#naming) for the ids.

The rest of the repo:

```
justfile                 every workflow — just --list
compose.yaml             the seven services; reads DW_PROJECT from .env
samples/                 starter projects, seeded into assets/ by `just setup`
docker/
  splat-generator/       the world-generation flow (GPU)
  splat-viewer/          WebGL splat viewer
  pano-viewer/           360 viewer for the input panoramas
  rmf-tools/             RMF + Gazebo + traffic editor + the build-world flow
scripts/                 host-side tools: model downloads, panorama alignment
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
| `capture-edge` | `worldjobs` | capture | one corridor's panoramas, in simulation |
| `generate-world` | `generator` | 6 HY-World stages | one waypoint's world |

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
photographed, so there is something to compare a real capture against:

```bash
just capture L11.cafe--v7        # -> panos/L11.cafe--v7/000.png ...
just capture-all                 # every corridor not yet shot, one job each
```

A camera weaves along the lane, stopping every half metre and taking a full 360
at each, alternating between 1.25 m and 1.95 m so every surface is seen from two
angles rather than ten nearly identical ones. Doors and lifts on the lane are
opened first, since a closed one is a wall the camera cannot see past.

It writes **panoramas and nothing else** — no poses, no positions, no marker
that it came from a simulator — because a synthetic run that leaked what a real
capture cannot would be testing the pipeline under conditions it never faces.

**A world**, from one panorama of one waypoint:

```bash
just generate L11.v6             # panos/L11.v6.jpg -> splats/L11.v6/
```

HY-World takes the single vantage point and imagines the rest: it plans a
trajectory over a navmesh, renders along it, expands that into consistent video
with Wan2.1, and trains a splat on the ~400 views that come out. Output is
`world.ply` (web) and `world.usdz` (Isaac Sim, NuRec).

`generate` skips a waypoint that already has a `world.ply` — delete the splat
directory to rebuild. One waypoint per job, so each gets its own run to inspect,
retry or compare.

**Facing the right way.** A 360 records no heading, so a panorama arrives turned
by whatever way the photographer happened to be standing, and the world
generated from it inherits that. The alignment tool puts it right:

```bash
just align                       # http://localhost:8085
```

Pick a waypoint, pick a corridor leaving it, and rotate the panorama until you
are looking down that corridor. Saving rewrites the file itself, so everything
downstream — generation, the viewer, the walks — loads it already facing the
building's +X, and nothing has to carry a correction around. `panos/.aligned/`
records what was applied, which is how `just plan` knows a panorama has been
looked at.

Do this **before** generating: the world is built from the panorama, so turning
it afterwards means generating again.

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

Every part of the building gets an id, and a panorama is named for the place it
was taken. `build-world` computes the ids and writes
`worlds/<map>/capture_plan.json`; `just plan` reads it back.

**Waypoints are what you photograph.** HY-World imagines a whole world from one
vantage point, so a place needs one 360 rather than a walk, and the corridors
between places are then walks *across* worlds rather than captures of their own.

| | id | goes in |
| --- | --- | --- |
| a waypoint, named | `<level>.<name>` | `panos/L11.cafe.jpg` |
| a waypoint, unnamed | `<level>.v<index>` | `panos/L11.v7.jpg` |
| the corridor between two of them | `<level>.<a>--<b>` | — walked, not shot |

Three decisions worth knowing:

**The level is always part of the id.** Waypoint names are not unique across a
building — the sample map has a `lift_lobby` on L1 and another on L11, one
directly above the other. Two different places must not share a name.

**Unnamed vertices fall back to their index.** Most vertices are unnamed
corners with no other handle. The index comes from the nav graph, so it can
shift if you insert vertices in the traffic editor.

**An edge is one corridor, not two.** Every lane in these graphs is
bidirectional, so the endpoints are sorted and the edge gets one name whichever
way you walk it. The level is written once, since lanes never cross levels.

```bash
just plan            # every waypoint and how far along it is
just plan missing    # only what is unfinished
just vertices L11    # the waypoints on one level, with their positions
```

`just plan` reads as a checklist, because that is what it is:

```
  L11   L11.v6                 built, 2 lanes walkable
  L11   L11.v5                 aligned, not generated
  L11   L11.v4                 shot, not aligned
  L11   L11.cafe               —
  -- 2/27 waypoints walkable
```

A waypoint is walkable when four things are true, and the state names whichever
is missing first: the panorama exists, it has been turned to face the building,
a world has been generated from it, and its neighbours have been marked in that
world.

## Walking the building

The viewer opens one waypoint's world:

```
http://localhost:8081/?url=files/multilevel_office/splats/L11.v6/world.ply
```

Each generated world stands alone — its own scale, its own origin — so there is
no single frame to place them all in and no route to precompute. What they do
share is a heading, because every panorama was aligned to the building before
its world was generated. That is enough: a corridor is a direction, and a
direction is comparable across worlds even when nothing else is.

**Marking a corridor.** `world.paths.json` lists the lanes leaving this
waypoint, straight out of the nav graph, with the bearing and length of each.
What it cannot know is where in *this* world those neighbours are, since the
world has no building coordinates. So you say: stand where the corridor ends
and mark it. Two marks make a walk — one at each end — and no walk exists until
both are placed, which is what `just plan` counts.

**Crossing.** Riding a walk to the end hands over to the world at the far end:
its splat is fetched and unpacked while you are still walking, in a second
worker so the one on screen keeps rendering, and swapped in when you arrive.
The camera keeps its heading through the swap — which is the whole reason the
panoramas were aligned — so a corridor you were walking down is still ahead of
you in the world you land in. No loading screen, and nothing is stitched: the
renderer only ever holds one world.

## Notes

- **Align before you generate.** The world is built from the panorama, so a
  panorama turned the wrong way produces a world turned the wrong way, and the
  only fix is to generate it again.
- **A splat is only as good as the one photo it came from.** Everything past
  the first vantage point is imagined, so a panorama shot in the middle of a
  junction gives a better world than one shot against a wall.
- **Splats train in classic, not antialiased, mode** so the PLY stays portable
  across SuperSplat, web viewers and Isaac.
- **SAM 3 weights** come from ModelScope; `facebook/sam3` on HuggingFace is
  gated. See `scripts/README.md`.
- **Upstream patches** to HY-World live in `docker/splat-generator/hyworld.patch`;
  environment fixes are documented inline in `build_env.sh`.
- **The viewer is baked into its image**, so a change to `main.js` needs
  `just build && just up` before nginx serves it.

Third-party code and model licenses: see `NOTICE.md`.
