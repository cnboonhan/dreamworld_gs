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
just summary    # waypoints, splat quality, progress, and every address
```

Five web UIs come up and stay up:

| | |
| --- | --- |
| http://localhost:4200 | jobs, logs, retries (Prefect) |
| http://localhost:8081 | splat viewer |
| http://localhost:8082 | 360 viewer for input panoramas |
| http://localhost:8083 | the building simulated under RMF (Gazebo, over noVNC) |
| http://localhost:8084 | the traffic editor — author the map (over noVNC) |
| http://localhost:8086 | the dashboard — tools, mission agent, minimap |
| http://localhost:8087 | the panorama editor — face a view, prompt an edit, save |

(Eight containers: those five plus the VLM and the two job workers.)

One more runs on the host rather than in a container, because it rewrites files
in `assets/` in place:

| | |
| --- | --- |
| http://localhost:8085 | the panorama aligner — `just align` starts it |

Remotely:

```bash
ssh -L 4200:localhost:4200 -L 8081:localhost:8081 -L 8082:localhost:8082 \
    -L 8083:localhost:8083 -L 8084:localhost:8084 -L 8085:localhost:8085 \
    -L 8086:localhost:8086 -L 8090:localhost:8090 <this-host>
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
| `build-world` | `worldjobs` | generate → inspect → plan | one Gazebo world + nav graph + capture plan |
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
just summary               # waypoints, quality, progress, addresses
```

Ctrl-C stops following; the job keeps running.

## Making things

**The building in simulation**, from the project's map:

```bash
just world
```

`maps/<map>.building.yaml` → `worlds/<map>/`: the Gazebo world, its models,
`sim.launch.xml`, and `nav_graphs/0.yaml`. The sim also generates it on first
start if it is missing, so a fresh checkout comes up with something to look at.

**A world you can look at**, from one panorama of one waypoint:

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

**Editing what a place looks like.** `just up` brings up a panorama editor at
**http://localhost:8087**, ported from `dreamworld/docker/dream_editor`. Pick a
waypoint on the floorplan, look around its 360 with the mouse, type what to change,
and it crops what you are facing, edits that undistorted crop through
Qwen-Image-Edit-2509, reprojects it back into the equirect and composites only the
pixels that changed. Stack several edits, compare before and after in the synced
viewer, then **Save**.

It edits and nothing else. The dream had four modes — `restyle` and `reference`
re-rendered a whole panorama from the simulator's flat geometry toward a style
phrase, and `inpaint` brushed part of such a restyle back in. There is no
simulated panorama here and no style to impose: these are photographs of a real
building, and the job is to change one thing in one and leave the rest of the
photograph alone. So the style-prompt anchor is gone with them, and the
instruction is wrapped server-side — the model is told to hold the camera
viewpoint, walls, floor, ceiling, windows and lighting exactly as they are and
change only what you asked. Describe the change alone: "add a potted plant in the
corner", not a description of the room.

Save writes `panos/<id>` — keeping what it replaced under `panos/.before-edit/`,
since that is a photograph of a real place and an edit is not obviously an
improvement until you have looked at it. Nothing else happens: the panorama is the
input to everything, so the propagation is `just generate <id>`, which rebuilds
that waypoint's splat world from the file you just wrote. That is 20 minutes of GPU
and belongs on the queue, not inside a click.

The model needs ~45 GB and gets a card of its own — `DW_EDIT_GPU`, default 5,
which is off the four world generation uses.

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
records what was applied, which is how `just summary` knows a panorama has
been looked at.

Do this **before** generating: the world is built from the panorama, so turning
it afterwards means generating again.

## Moving a project

A tarball of one project is everything another machine needs: the map, the
Gazebo world, the panoramas, and each splat's `world.ply`, `world.usdz`,
`world.cam.json`, `world.paths.json` and source panorama. What HY-World
produced on the way there — `gs_data`, `render_results`, `navmesh`,
`gs_result` — stays behind, since it is input to a training run that has
already happened and it is 34 of this project's 103 GB. Model weights are not
included either; those come from `just setup` on the far side.

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
`worlds/<map>/capture_plan.json`; `just summary` reads it back.

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
just summary         # every level
just summary L11     # one level
```

Four sections, in the order you want them picking work up: where each waypoint is
(id, nav index, drawing index, pixel position, lanes), how good each built world
came out, how far along the whole thing is, and every address with a live probe.
It is a wrapper — the report is `scripts/summary.py`, so the recipe never becomes
a second place any of it is written down.

The **splat quality** section scores every built world against views the trainer
held out of training, so a bad one is a line in a table rather than something you
notice weeks later while walking through it:

```
  L11.apex_lab            23.20  0.826  0.182    355,903
  L11.lift_lobby          20.39  0.782  0.226    360,194
  L11.v7                  19.06  0.719  0.261    386,721   (soft, geometry)
```

Those are held-out views, so it measures **self-consistency, not fidelity** —
every one of them was generated by HY-World from a single photograph, so a world
can score well and still have invented a corridor. The renders under
`gs_result/renders/` are the side-by-side rendered-against-truth images that catch
that, and `just summary --renders` prints their paths. Low SSIM beside a fair PSNR
usually means the geometry moved rather than the image being noisy; LPIPS is the
closest of the three to "does it look right to a person".

The **progress** section reads as a checklist, because that is what it is:

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
both are placed, which is what `just summary` counts.

**Crossing.** Riding a walk to the end hands over to the world at the far end:
its splat is fetched and unpacked while you are still walking, in a second
worker so the one on screen keeps rendering, and swapped in when you arrive.
The camera keeps its heading through the swap — which is the whole reason the
panoramas were aligned — so a corridor you were walking down is still ahead of
you in the world you land in. No loading screen, and nothing is stitched: the
renderer only ever holds one world.

## Driving it by tool call

The building is walkable by API as well as by hand. `just interactive` brings up a
tool surface ported from `dreamworld/docker/dream_interactive` — the same tools,
name for name and argument for argument, so a client written against that
dashboard drives this one unchanged:

| | |
| --- | --- |
| navigation | `go_to` `turn` `face` `open_door` `close_door` |
| lifts | `take_lift` `select_lift` `call_lift` |
| items | `pick` `place` |
| planning | `plan_route` `where` `get_path` `get_graph` `write_mission` `write_todos` |

The dashboard is at **http://localhost:8086** — a mission bar across the top, a
minimap of the level, the tool palette, the live log, and the world model the agent
reads each turn. The splat viewer opens in its own window from the link in the
status line, already carrying the `?agent=` parameter that hands over its camera.

The page is the dream harness's own, ported: the mission bar and run/pause/cancel
across the top, the floorplan filling the left column with the arrow pad (`↰ ↑ ↱`,
and the arrow keys) and the tool palette beneath it, and the agent's world model
over the log in a resizable right panel. Only the two camera panes are gone — they
streamed stitched MJPEG, and there is nothing here to stream.

The minimap is the nav graph on the level's own floorplan, projected by an affine
fitted from the waypoints the drawing and the nav graph both name (7 on L11). Hover
a waypoint for its name; with a tool field open, click one to fill it in. A hollow
ring means no splat world has been generated for that waypoint yet, so what is
walkable is visible at a glance.

```bash
just interactive
curl localhost:8086/tools
curl -X POST localhost:8086/tool -H 'Content-Type: application/json' \
     -d '{"tool":"go_to","args":{"vertex":"apex_lab"}}'
curl -X POST localhost:8086/command -H 'Content-Type: application/json' \
     -d '{"text":"go to apex_lab"}'
```

**What a call rolls out onto is the difference.** The dream stitched pre-rendered
library clips into an MJPEG pane; there is no such video here. Each waypoint is a
splat world of its own, so a walk is the viewer riding that world's marked corridor
and handing over at the vertex — live, at whatever framerate the box draws. Open the
viewer with `&agent=http://localhost:8086` and it takes commands over SSE and reports
back when the camera has actually landed, so a tool call does not return until the
walk did. Until a viewer connects, the walking tools say so rather than reporting a
move that never happened.

**The robot half is unchanged.** `docker/rmf-tools/robot_bridge.py` is the dream
bridge, code for code — only its docstring differs, because this repo lays a project
out as `worlds/<map>/` rather than `outputs/generate_gz/`. It spawns the Galaxea R1
into its own Gazebo on its own transport partition, drives it by interpolating its
pose along the nav polyline (`DRIVE_SPEED` 2.0 m/s, `TURN_RATE` 1.25 rad/s), routes
doors and lifts through RMF, and serves `/goto` `/turn` `/door` `/call_lift` `/pick`
`/place` `/state` on :8090. Every traversal is mirrored onto it, so the splat walk
and the robot stay edge-for-edge in step.

The two halves read the same `nav_graphs/0.yaml`, which is what makes that true:
one graph, one set of indices, one metric frame.

**Staying in step** takes one more thing, because a splat walk is live rather than
rendered. The dream got this for free: `move_gz` rendered every clip at exactly
`DRIVE_SPEED` and `TURN_RATE`, so a clip's duration *was* its distance over its
speed and the robot could not fall behind. Nothing forces a live walk to take any
particular length of time — it ran at a fixed 10 s a corridor, so the robot
crossed a 1.97 m lane in one second and the camera took ten.

Both endpoints of every edge are known in both frames — the nav graph has them in
metres, the marked walk has them in the splat world's own coordinates — so the
motion is not approximated from a rate and checked afterwards. The server computes
the leg once, from the graph: the arc is the shortest turn from the heading being
held to the bearing of this leg, exactly as `drive_path` takes it, and the
distance is the lane's. Both sides are handed those same two numbers and their
durations, so they perform one motion rather than two motions timed alike.

    lift_lobby       -> lift_lobby_north    0.0 deg in     0 ms | 1.97 m in  983 ms
    lift_lobby_north -> v0                 -2.5 deg in    35 ms | 1.79 m in  893 ms
    v0               -> v11                 0.6 deg in     9 ms | 4.41 m in 2203 ms

Both step one edge at a time, started together.
Per edge rather than per route matters — sending the robot the whole polyline up
front let the two drift with nothing to pull them back, where now any difference
is bounded by one corridor and corrected at every vertex. `DW_DRIVE_SPEED` and
`DW_TURN_RATE` set both halves at once.

**Lifts go through `take_lift`, never by hand.** It installs a fixed eight-step
template as the subtask plan — `select_lift` → `face` cabin → `call_lift` this level
→ `open_door` → `go_to` cabin → `call_lift` target level → `open_door` → `go_to`
lobby — resolving the exit lobby on the *target* level by name, so it still resolves
after the ride switches the graph. Calling `select_lift` or `call_lift` outside such
a template is hard-rejected, so a level change cannot be improvised a step at a time.

**The mission agent** (`POST /agent`) is the deepagents graph over these same tools,
with `/pause`, `/resume` and `/cancel`. The harness owns subtask status, as it did in
the dream: `write_todos` rejects anything that is not exactly one tool call, a call
out of turn is rejected as `out_of_order`, and a subtask completes only when its
verify passes — which here means the viewer reporting where it stands *and* the
bridge's RMF state putting the robot within 0.8 m of the waypoint. An agent cannot
mark its own work done.

It speaks to any OpenAI-compatible endpoint, so the model is configuration rather
than code:

```bash
DW_VLM_URL=http://localhost:3000/v1 DW_VLM_MODEL=claude-opus-4-8 \
DW_VLM_KEY=<key> docker compose up -d interactive
```

It defaults to the Qwen3-VL the stack already runs; point it at a cliproxyapi and a
Claude model and nothing else changes.

`GALAXEA_URL` unset runs the viewer half alone, on a box with no Gazebo. The Galaxea
R1 meshes are ~32 MB and live at `assets/projects/<p>/GalaxeaR1/`, outside git.


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
