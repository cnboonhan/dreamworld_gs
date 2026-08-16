# dreamworld_gs

Two halves of one building. **RMF** simulates it from an annotated floorplan —
doors, lifts, waypoints, the lanes between them. **Gaussian splatting** shows
what it actually looks like, generated from one 360 photo taken at each
waypoint. They meet at the nav graph: a waypoint is a place you photograph, and
the lanes leaving it are the ways you can walk out.

Everything runs on this box, offline, behind `docker compose`.

This file is the operating manual. [architecture.md](architecture.md) is the
deep reference — every technology in the stack, the measured numbers, the
design rules, and what deploying against a real building involves.

## Layout

One project is one building, and one directory:

```
assets/projects/<project>/
  maps/            <map>.building.yaml + floorplans             (you author)
  worlds/<map>/    <map>.world, models/, nav_graphs/0.yaml,
                   capture_plan.json                            (generated)
  panos/<id>.jpg   one 360, taken standing at that waypoint     (you shoot)
  panos/<id>@<variant>.jpg   an edited look of the same place   (you edit)
  splats/<id>/     world.ply, world.usdz, world.cam.json,
                   world.paths.json                             (generated)
  splats/<id>@<variant>/     the world generated from a variant (generated)
```

A panorama is named for the place it was taken, so a splat is addressed by
where it is in the building rather than by a name someone invented. See
[naming](#naming) for the ids.

**Variants are looks, not places.** `<id>@<name>` is the same waypoint with a
different appearance — a pallet blocking the corridor, a cleared room — made
in the panorama editor (select a look, edit, **Save** to overwrite it or
**Save as variant…** to add one) and built with `just generate <id>@<name>`.
A variant world answers to its base waypoint: it inherits the base's
alignment marks until re-marked, its paths doc names the base, and the
dashboard and missions go on addressing the vertex. Choosing which look is
on screen is the splat viewer's variant dropdown in the side panel; scenario
authoring is exactly this loop.

The rest of the repo:

```
justfile                 every workflow — just --list; a wrapper over scripts/
compose.yaml             the twelve services; reads DW_PROJECT from .env
samples/                 starter projects, seeded into assets/ by `just setup`
docker/
  splat-generator/       the world-generation flow (GPU)
  splat-viewer/          WebGL splat viewer, and the agent channel into it
  pano-viewer/           360 viewer for the input panoramas
  pano-editor/           edit a panorama by prompt, and the model behind it
  interactive/           the tool surface: go_to, open_door, take_lift …
  rmf-tools/             RMF + Gazebo + traffic editor + build-world + the robot
scripts/                 the host side: downloads, alignment, and every report
assets/                  gitignored: model weights, job history, projects/
```

## Running it

```bash
just setup      # one-time: model weights + images (~550GB, needs network)
                #            `just setup images` alone after a code change
just up         # start everything, print the URLs
just summary    # waypoints, splat quality, progress, and every address
```

Seven web UIs come up and stay up:

| | |
| --- | --- |
| http://localhost:4200 | jobs, logs, retries (Prefect) |
| http://localhost:8081 | splat viewer |
| http://localhost:8082 | 360 viewer for input panoramas |
| http://localhost:8083 | the building simulated under RMF (Gazebo, over noVNC) |
| http://localhost:8084 | the traffic editor — author the map (over noVNC) |
| http://localhost:8086 | the dashboard — tools, mission agent, minimap |
| http://localhost:8087 | the panorama editor — face a view, prompt an edit, save |

(Twelve containers: those seven plus the robot bridge, the editor's model, the
VLM and the two job workers.)

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
Every other knob — GPU ids, ports, pacing, the agent's model and key — is a
`DW_` variable too: [.env.example](.env.example) lists them all with their
defaults. `.env` is gitignored and survives `just` runs, so it is the home
for deployment settings and keys.

Requirements: NVIDIA GPUs (4+, ~60GB VRAM for generation) with CUDA 12.8, the
NVIDIA container runtime, [just](https://github.com/casey/just),
[uv](https://docs.astral.sh/uv/), and about 600GB of disk — the model weights are
550GB of that, and one building's panoramas and splat worlds add ~35GB.

## The pipeline

The two expensive things are Prefect jobs — building a world takes minutes, and
generating a splat takes twenty of them on four GPUs, so both belong on a queue
that can be watched and retried rather than in a terminal that can be closed. The
justfile only submits and follows:

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
Gazebo and `rmf_building_map_tools`, the splat flow wants CUDA. Each is served
from the image that has what it needs, and both register with the same Prefect
server — so it is still one queue.

Everything else is a live service rather than a job, because it is interactive:
the viewers, the panorama aligner and editor, the tool surface at :8086 and the
robot bridge at :8090 all answer while you are looking at them. A job you submit
and come back to; a service you talk to.

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

A tarball of one project is everything another machine needs — the whole
project directory travels by default: the map, the Gazebo world, the
panoramas and their alignment records, every splat's deliverables and marks,
the robot's meshes, the interactable items. Only the named regenerables stay
behind — HY-World's training intermediates (`gs_data`, `render_results`,
`navmesh`, `gs_result`: 40 of the sample project's 41 GB) and the editor's
caches — so the bundle is the ~1.4 GB that cannot be rebuilt, and `pack`
prints a per-drawer size table so a bloated archive announces itself before
it is copied anywhere. Model weights are not included; those come from
`just setup` on the far side.

```bash
just bundle                 # the active project -> dist/
just bundle htx /tmp        # a named project, somewhere else
just unbundle <file>        # restore, in place, on the other node
```

Paths are stored as `assets/projects/<name>/...`, so an unbundle lands exactly
where the stack looks. `unbundle` warns before merging into a project that
already exists.

**Running on the far side needs none of the generation stack.** A device that
only walks, simulates and drives an already-generated project uses
`compose.minimal.yaml`: the splat viewer, the RMF sim, the robot bridge and
the harness — four services, no GPU, no Prefect, no model weights. Build the
three images where there is network and carry them with the bundle:

```bash
# where the images exist
docker save dreamworld/splat-viewer dreamworld/interactive \
            dreamworld/rmf-tools | gzip > dreamworld-runtime.tgz

# on the target
docker load < dreamworld-runtime.tgz
just unbundle <project>-<stamp>.tar.gz
just up minimal            # or, without just:
                           # docker compose -f compose.minimal.yaml up -d
```

The mission agent still works there: point `DW_VLM_URL` at any
OpenAI-compatible endpoint — the minimal file runs no model of its own, and a
device that wants one locally runs the full `compose.yaml`. The two compose
files share container names, so on the build box stop the full stack before
trying the minimal one.

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

**Crossing.** The two worlds trade places in the middle of the corridor, not
at its end. Both hold the same physical corridor between their own marks, so
at the halfway point — where the world being left is at its blurriest and the
next is at its sharpest — the ride swaps buffers under a cross-fade and
carries on: same line, same fraction, same heading. Flying by hand crosses
the same way past the midpoint of a marked corridor. Nothing is stitched and
there is no loading screen: the renderer only ever holds one world, and the
fade is a snapshot of the last frame dissolving over the next.
`?handover=` moves the trade point; `1` restores the swap at the vertex. A
corridor marked from only one end falls back to that older swap.

**Never waiting.** The whole building is fetched breadth-first from wherever
you stand into a browser cache that survives reloads, parsed once ever (the
parsed form is cached beside the source), and pre-unpacked into memory — so a
crossing normally costs nothing at all. The panel's cache line shows both
counts (`splats cached 16/16 · preheated 16/16`) and a clear button for after
a regeneration. The minimap carries a live green wedge for the camera — exact
while riding, projected through the marks while flying free.

## Driving it by tool call

The building is walkable by API as well as by hand. The tool surface is ported
from `dreamworld/docker/dream_interactive` — the same tools, name for name and
argument for argument, so a client written against that dashboard drives this one
unchanged. It comes up with the stack; `just interactive` prints where:

| | |
| --- | --- |
| navigation | `go_to` `turn` `face` `open_door` `close_door` |
| lifts | `take_lift` `select_lift` `call_lift` |
| items | `pick` `place` |
| planning | `plan_route` `where` `get_path` `get_graph` `write_mission` `write_todos` |

The dashboard is at **http://localhost:8086** — a mission bar across the top, a
minimap of the level with the Gazebo sim embedded under it (rmfsim's screen in
a resizable pane, so the robot is watchable without a second window), the tool
palette, the live log, and the world model the agent reads each turn. The splat
viewer opens in its own window from the link in the status line and connects
back to the dashboard by itself.

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
and handing over mid-corridor — live, at whatever framerate the box draws. Open the
viewer and it connects to the dashboard on its own host by itself (`?agent=off`
opts out; `?agent=<url>` points at a dashboard elsewhere), takes commands over SSE
and reports back when the camera has actually landed, so a tool call does not
return until the walk did. Taking the controls by
hand hands them back: clicking a walkthrough button detaches the agent first (the
chip is the light and the switch), while the viewer keeps proposing its position so
the model still follows a hand walk when it is idle. Until a viewer connects, the
walking tools say so rather than reporting a move that never happened.

**The robot is in the sim you can watch** — `:8083`, the same Gazebo the traffic
editor's world runs in. The dream booted a second one on its own transport
partition, which was right there (the pipeline's renders boot their own
`sim_world` and must never see the robot) and wrong here: it meant two Gazebos of
one building, with the robot standing in the one nobody was looking at. The
bridge now shares rmfsim's network namespace and joins its gazebo, starting only
the piece rmfsim does not — the `set_pose` service bridge.

**The robot half is otherwise unchanged.** `docker/rmf-tools/robot_bridge.py` is
the dream bridge, code for code — only its docstring differs, because this repo lays a project
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

**Teleport is the operator's, not the agent's.** `just teleport <waypoint> [level]`
puts the robot somewhere directly, and the world model with it — both move or
neither does, since resetting the bridge alone would leave the dashboard
describing a place the robot had left. The dashboard has the same control, and
double-clicking a waypoint on its minimap goes there. It is deliberately absent
from `/tools`, so a mission cannot skip a corridor by wishing itself past it.

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
- **The VLM needs two flags before the mission agent can call a tool.** vllm
  serves tool calls only with `--enable-auto-tool-choice` and a
  `--tool-call-parser`; both are set in `compose.yaml`, but the running server
  predates them until it is restarted. A Claude proxy serves them natively.
- **A generated world is only ever self-consistent.** `just summary` scores each
  against views held out of training — but those views were themselves generated
  from the one photograph, so a world can score well and still have invented a
  corridor. Nothing in this pipeline can catch that; the renders and your eye can.
- **The viewer is baked into its image**, so a change to `main.js` needs
  `just setup images && just up` before nginx serves it.

Third-party code and model licenses: see `NOTICE.md`.
