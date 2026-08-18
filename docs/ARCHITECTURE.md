# dreamworld_gs — architecture and authoring reference

The [README](../README.md) says how to launch; this says how it works:
the authoring flow step by step, where every file lives, the API seams a
harness rides, and which GPU holds what.

## The flow

1. **Structure** in `/sim_editor`: floorplan, walls, doors, lifts, one
   scale measurement. This is the traffic editor's half of
   `building.yaml`, and this stack only ever reads it.
2. **Vertices and edges** in `/dreamworld_editor`: press *vertex*, click
   the plan; press *edge*, click two vertices. Every change syncs the nav
   layer back into `building.yaml` — named vertices on their level, lanes
   on **nav graph 0** — so RMF's tools read the same graph the editor
   draws. Lift cabins materialize automatically as `<level>.<lift>`
   vertices (diamonds on the plan); the same lift on two levels is the
   same cabin.
3. **Panorama**: select a vertex, upload the 360 shot standing there.
4. **Align**: face a neighbour, drag until that corridor sits on the
   dashed line, check the other neighbours agree, save. Alignment belongs
   to the VERTEX — one roll turns the original, every variant, and every
   undo stash together, so the offsets can never drift. Saving a 0° turn
   marks an already-correct panorama as aligned.
5. **Variants** are looks, not places: *new* copies the original into a
   named look; aim the free-look viewer's rectangle at what should
   change, write a prompt, *edit* — Qwen-Image-Edit repaints only where
   you aimed. Undo swaps back; the original is never touched.
6. **Splats**: per look, *generate splat* queues HY-World 2.0 — six
   stages, four GPUs, ~17 minutes a world. The box shows the stage and
   the queue; the viewer opens the result at its spawn camera.
7. **Crossings**: select an edge; each direction card shows both
   panoramas faced along the walk's own bearing and the splat
   walkthrough, with a *video transition* box beneath. The prompt
   defaults by what the map says the walk passes through — a door edge
   opens a door, a lift edge rides a lift, an open edge just walks — and
   Wan 2.2 generates the crossing from the two aligned panoramas as
   first and last frames.
8. **Walk** in `/dreamworld_viewer`: the plan shows where you stand and
   the ways out; click a neighbour, pick the look to arrive in, go — the
   camera spins to the edge's bearing, the crossing video plays while
   the destination loads behind it, and the fade lands you at the next
   capture point facing the way you walked. Worlds and crossings preheat
   in the background after the first load.

9. **Drive** in `/harness`: main's interactive dashboard on the v2
   seams. Tools (`go_to`, `face`, `open_door`, `take_lift`, `pick` …)
   move the walker by commanding `dreamworld_core` and work the building
   through the sim's infra bridge; `go_to` refuses a door-blocked edge
   until the door is opened. The mission agent (Qwen3-VL via the vLLM)
   takes an instruction, plans with the same tools, and is gated by the
   same movement lock. A viewer tab, if one is open, walks every move.

Vertex colors on the plan: **red** — panorama missing or alignment never
saved; **yellow** — work remains; **green** — every look, original and
each variant, has its splat.

## Layout

One project is one building, one directory:

```
assets/projects/<project>/
  maps/                    <map>.building.yaml + floorplans
  worlds/<map>/            Gazebo world + nav_graphs/0.yaml   (just world)
  sim_assets/              robot models
  dreamworld/              everything the flow produces — the editor is
    <vertex>/              this tree's ONLY writer
      vertex.json          level, position (drawing pixels), lift if any
      pano.jpg             the 360, rolled in place by alignment
      pano@<look>.png      a variant of the same place
      aligned.json         the vertex's one alignment record
      splat/               world.ply, world.splat, world.cam.json …
      splat@<look>/        the world generated from that variant
    edges.json             the graph, mirrored into building.yaml lanes
    .crossings/<a>__<b>/   first.png, last.png, prompt.txt, crossing.mp4
```

`just pack` carries the whole project directory to another machine;
`just unpack <tar>` restores it.

The repo:

```
justfile                   every workflow — just --list
compose.full.yaml          every service; minimal and demo pick from it
docker/
  proxy/                   nginx: one port, every surface
  rmf-tools/               traffic editor, world build, Gazebo+RMF sim
  dreamworld_editor/       the NiceGUI editor and its store
  dreamworld_viewer/       the walkthrough (static, fed by the editor)
  dreamworld_core/         the state holder — the seam's fixed address
  harness/                 the dashboard, tools and mission agent
  splatgen/                HY-World 2.0 behind a one-job queue
  qwen/                    Qwen-Image-Edit-2509 (panorama variants)
  wangen/                  Wan 2.2 (crossing videos) behind the same queue
scripts/                   fetch_assets.py, models.txt, pack.py
assets/                    gitignored: weights (hf/, models/), projects/
```

GPUs on this box: **0** vLLM (Qwen3-VL — trajectory planning and the
mission agent) · **1–4** splat generation · **5** qwen image edit ·
**6** wan video · **7** wan video (second instance).

## The harness seam

RMF owns the building's infrastructure; the viewer owns the walk; the
core owns the truth of where the walker stands. `/harness` is the
reference consumer of all three, but the seams are plain HTTP and
anything may speak them.

**Moving the walker** — command the core, monitor the viewer's report.
The core is the ONE writer of position; the viewer follows it (spin,
crossing video, arrive), and reports back:

```
POST /dreamworld_core/position        { "at": "L11.v0.apex_lab",
                                        "look": "original",
                                        "yaw_deg": 90.0 }   (optional;
                                       same-at + yaw = turn in place)
```

A command lands whether or not a viewer tab is open — the core's truth
advances either way, and a tab that attaches later teleports to it.

**Where the walker is** — the viewer reports on change and heartbeats
every second:

```
GET /dreamworld_viewer/state          (held by dreamworld_core, the
                                       stack's state holder — the state
                                       is born in a browser tab, which
                                       can only push)
{ "state": { "at": "L11.v0.apex_lab", "look": "original",
             "level": "L11", "x": 734.5, "y": 65.9, "lift": null,
             "yaw_deg": 0.8, "pitch_deg": 0.0, "moving": false,
             "transition": { "to": "…", "look": "…",
                             "phase": "spin | crossing" } },
  "age": 0.4, "live": true }
```

**The building's levers** — the infra bridge runs beside the sim
(inside its ROS graph, `rmfsim:8090` on the compose network) and turns
HTTP into the same four topics main's bridge spoke:

```
POST /door  /call_lift  /pick  /place    GET /door_state  /lift_state
          ↓ inside the sim ↓
publish   /door_requests   rmf_door_msgs/DoorRequest   (mode 2 = open)
publish   /lift_requests   rmf_lift_msgs/LiftRequest   (destination_floor)
subscribe /door_states, /lift_states
```

Note `/lift_requests`, not `/adapter_lift_requests`: this stack runs the
simulation's door and lift plugins directly, with no supervisor
arbitrating — a lift is whoever asked last. Both paths are verified
against the live sim: a DoorRequest opened `apex_lab_door`, a LiftRequest
rode `lift1` from L1 to L11.

The editor also serves `GET /dreamworld_editor/graph` — every vertex,
its looks and built worlds (each with its compass: building east, up and
the capture point in ply coordinates), the edges, and which crossings
have videos. This is the document the viewer runs on, and a harness may
read it for the same map.

## Two seams to respect

- `building.yaml` is shared: structure (walls, doors, lifts,
  measurements, unnamed vertices) is the traffic editor's; the nav layer
  (named vertices, lanes) is the dreamworld editor's, regenerated
  wholesale on every change. Last writer wins on the file — keep the
  traffic editor closed while tracing nav, or reopen it after.
- Restarting `splatgen` or `wangen` kills the job they are running (the
  queue is memory-only, by design — every job's inputs are on disk and
  resubmission costs one click). Don't rebuild them mid-generation.
