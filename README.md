# dreamworld_gs

<div align="center">

## 🌐 &nbsp;[**► WALK THE LIVE DEMO ◄**](https://cnboonhan.github.io/dreamworld_gs)

**[cnboonhan.github.io/dreamworld_gs](https://cnboonhan.github.io/dreamworld_gs)**

*in your browser, nothing to install — walk the splat worlds,<br>
cross doors and ride the lifts through their generated videos*

</div>

---

Grow a photorealistic, walkable twin of a building — one 360 photo at a
time. **RMF** simulates the building's infrastructure (doors, lifts) from
an annotated floorplan. **Panoramas** are what each place actually looks
like, and a **world model** brings the view you are standing in to life,
conditioned on a prompt you type. **A video model** fills the seconds
reality hides between two photographs: the door swinging open, the lift
riding between floors. **Gaussian splat** worlds carry the older
walkthrough. An **agent** drives it all by tool call. Everything runs on
this box, offline, behind one forwarded port.

## Quick launch

Three sizes. Each is a superset of the one before.

| tier | command | how it runs | needs | you get |
| --- | --- | --- | --- | --- |
| **demo** | `just bundle` then `just demo` | a static site — no Docker, no backend | any machine with a browser | the walkthrough alone: walk the splat worlds, cross edges through the generated videos. |
| **minimal** | `just minimal` | `compose.minimal.yaml` | no GPU — bring an OpenAI-compatible VLM (a cliproxyapi, a remote vLLM) for the agent: `DW_VLM_URL` / `DW_VLM_MODEL` / `DW_VLM_KEY` in `.env`, default the host's `:8000` | walk + simulate + command an already-generated dreamworld: viewer, Gazebo+RMF sim, harness dashboard and its mission agent. |
| **full** | `just up` | `compose.full.yaml` | 8 GPUs (see times below) | everything: authoring UIs and all the generators — splats, crossing videos, panorama variants. |

The demo bundle is a **self-contained static site**: `just bundle` folds
the viewer in with relative paths, so any file server serves the
directory from any subpath — `just demo` is literally `python3 -m
http.server` inside it. For **GitHub Pages**: `just pages` stages the
bundle onto a `gh-pages` branch as one parentless commit (with the
`.nojekyll` that keeps Jekyll from dropping `files/.crossings`); pushing
that branch and pointing Pages at it is deliberately left manual —
photoreal reconstructions of a real building need the same clearance the
photographs would before going anywhere public. Size-wise a bundle fits
comfortably: no single file near GitHub's 100 MB limit (worlds are
~25–30 MB) and the whole site sits well under the 1 GB Pages ceiling
until a building grows past roughly twenty-five worlds.

```
ssh -L 8080:localhost:8080 <box>        one tunnel carries every surface
```

First-time setup for **minimal/full**: `just fetch` downloads the model
weights (~600 GB, network needed once — the manifest is
`scripts/models.txt`). The **demo** tier needs no weights at all: the
bundle carries only reconstructions (`world.splat`), crossing videos and
the graph — the source panoramas never leave the dreamworld tree.

| surface | what |
| --- | --- |
| `/dreamworld_viewer` | **live**: stand in a waypoint's panorama, pan by keyboard, and the view comes alive under a prompt you type |
| `/dreamworld_viewer/walk.html` | the splat walkthrough: generated worlds, spin-and-cross transitions |
| `/harness` | drive it by tool call: dashboard, tools, the mission agent |
| `/rmfsim` | the building under simulation (Gazebo + RMF) over noVNC |
| `/dreamworld_editor` | grow it: vertices, panoramas, alignment, generation |
| `/sim_editor` | author the building's structure (traffic editor, full tier) |

## The live layer

The viewer's default page puts you inside a waypoint's photograph. Pan
with **A/D** and **W/S** (**Q/E** zooms) and you are moving through real
pixels; hold still and the view on screen is captured off the canvas and
handed to a world model as its seed, with whatever prompt is in the box.
The rollout streams over the top until the next keypress, so every
generated second starts from a true frame and ends at your next input —
which is what keeps a video model anchored to a real building.

Two knobs matter, both in `.env`:

- `DW_STREAMER_RUNNER` picks the model. The default,
  `lingbot-world-v2-14b-causal-fast-taehv-window15-sink3`, is distilled
  against long-horizon drift and measured at zero pixels of slide;
  `causal-forcing-wan2.1-i2v-1.3b-framewise` is an eleventh the size but
  wanders. The server asks the pipeline by signature whether it steers a
  camera, so either slug works unchanged.
- `DW_STREAMER_DRIFT_PX` rebuilds the rollout from its seed once the view
  has slid that far. `0` disables it — worth trying first, since the
  default checkpoint holds on its own.

`GET /streamer/status` reports frames, fps and the measured drift, and
`GET /streamer/seed.jpg` returns the exact frame the model is looking at.

## Compute and generation times

Measured on this box (NVIDIA H200s), generating the sample office:

| job | model | cards | VRAM | time |
| --- | --- | --- | --- | --- |
| splat world | HY-World 2.0 | 3 | large | **15–20 min** per place |
| live view | LingBot-World v2 14B causal-fast | 1 | ~80 GB | **~4.5 fps** streamed, ~20s to warm |
| crossing video | Wan 2.2 FLF (×2 instances) | 1 each | ~72 GB | **6–7 min** per video (+~2 min model load on a cold instance) |
| panorama variant | Qwen-Image-Edit-2509 | 1 | ~60 GB | **~1 min** per edit |
| mission agent / planner | Qwen3-VL-8B (vLLM) | 1 | ~17 GB | interactive |

A building of V places and E edges costs roughly `V × 17 min` of splat
time (3 cards) and `2E × 6.5 min` of video time (halved across the two
wangen instances) — the queues run unattended and the editor toasts each
completion.

## The flow, in one paragraph

Trace walls, doors and lifts in `/sim_editor`; drop vertices and edges on
the plan in `/dreamworld_editor`; upload a 360 panorama per vertex, align
it to the building, generate its splat world; per edge, generate the
crossing videos (prompts default by what the map says the walk passes —
a door opens, a lift rides); then stand in it at `/dreamworld_viewer`,
where the plan is the map, the keyboard turns your head, and holding
still hands the view to the world model to animate — or walk the splat
worlds, or hand the harness agent a mission. Vertices and edges wear
traffic lights
everywhere: red — not started/unaligned, yellow — in progress, green —
complete. The full authoring manual, layout and API seams live in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Carrying it elsewhere

Three sizes of move, smallest first:

- `just bundle` — the walkable demo (graph + worlds + videos) into
  `assets/projects/<project>/bundle`, servable by `just demo` anywhere.
  Static, no GPU, no weights. ~30 MB per world, a few MB per crossing.
- `just pack` / `just unpack <tar>` — the WHOLE project: panoramas,
  splat worlds, crossings, the map and its generated world. Tens of GB,
  and it is the only part that cannot be re-downloaded.
- **The full experience** on another box needs three things beyond the
  repo:
  1. **The project** — `just pack` here, `just unpack` there.
  2. **The weights** — `just fetch` there (~300 GB, from the manifest in
     `scripts/models.txt`). Faster from the hub than over scp, and it
     skips anything already cached.
  3. **A `.env`** — `just up` writes `DW_PROJECT` and the invoking
     user's `DW_UID`/`DW_GID`; copy over any `DW_STREAMER_*` or GPU
     assignments you have tuned, and set them to that box's free cards.

  Then `just up` (8 cards) or `just minimal` (no GPU, bring your own
  VLM endpoint). The images build from this repo and need network once,
  for apt, pip and the checkpoint the streamer image bakes in.

  Nothing to copy for `assets/cache/` — the streamer's Triton and
  Inductor kernel caches (~280 MB) live there so that recreating the
  container does not recompile them: the first prompt after a fresh
  `just up` costs ~85s with the cache cold and ~20s once it has filled,
  which takes two or three runs. It is keyed by torch version and GPU
  arch, so it is machine-specific and always safe to delete.

Assets are gitignored: photographs of real places never enter git, and
nothing here publishes anything off this box.
