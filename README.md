# dreamworld_gs

Grow a photorealistic, walkable twin of a building — one 360 photo at a
time. **RMF** simulates the building's infrastructure (doors, lifts) from
an annotated floorplan. **Gaussian splatting** shows what each place looks
like, generated from a single panorama taken there. **A video model**
fills the seconds reality hides between two photographs: the door swinging
open, the lift riding between floors. An **agent** drives it all by tool
call. Everything runs on this box, offline, behind one forwarded port.

## Quick launch

Three sizes. Each is a superset of the one before.

| tier | command | how it runs | needs | you get |
| --- | --- | --- | --- | --- |
| **demo** | `just bundle` then `just demo` | a static site — no Docker, no backend | any machine with a browser | the walkthrough alone: walk the splat worlds, cross edges through the generated videos. |
| **minimal** | `just minimal` | `compose.minimal.yaml` | 1 GPU (~20 GB) | walk + simulate + command an already-generated dreamworld: viewer, Gazebo+RMF sim, harness dashboard and its mission agent (the GPU is the agent's VLM). |
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
| `/dreamworld_viewer` | walk it: splat worlds, spin-and-cross transitions |
| `/harness` | drive it by tool call: dashboard, tools, the mission agent |
| `/rmfsim` | the building under simulation (Gazebo + RMF) over noVNC |
| `/dreamworld_editor` | grow it: vertices, panoramas, alignment, generation |
| `/sim_editor` | author the building's structure (traffic editor, full tier) |

## Compute and generation times

Measured on this box (NVIDIA H200s), generating the sample office:

| job | model | cards | VRAM | time |
| --- | --- | --- | --- | --- |
| splat world | HY-World 2.0 | 4 | large | **15–20 min** per place |
| crossing video | Wan 2.2 FLF (×2 instances) | 1 each | ~72 GB | **6–7 min** per video (+~2 min model load on a cold instance) |
| panorama variant | Qwen-Image-Edit-2509 | 1 | ~60 GB | **~1 min** per edit |
| mission agent / planner | Qwen3-VL-8B (vLLM) | 1 | ~17 GB | interactive |

A building of V places and E edges costs roughly `V × 17 min` of splat
time (4 cards) and `2E × 6.5 min` of video time (halved across the two
wangen instances) — the queues run unattended and the editor toasts each
completion.

## The flow, in one paragraph

Trace walls, doors and lifts in `/sim_editor`; drop vertices and edges on
the plan in `/dreamworld_editor`; upload a 360 panorama per vertex, align
it to the building, generate its splat world; per edge, generate the
crossing videos (prompts default by what the map says the walk passes —
a door opens, a lift rides); then walk it in `/dreamworld_viewer` or hand
the harness agent a mission. Vertices and edges wear traffic lights
everywhere: red — not started/unaligned, yellow — in progress, green —
complete. The full authoring manual, layout and API seams live in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Carrying it elsewhere

- `just bundle` — the walkable demo (graph + worlds + videos) into
  `assets/projects/<project>/bundle`, servable by `just demo` anywhere
  Docker runs. ~30 MB per world, a few MB per crossing.
- `just pack` / `just unpack <tar>` — the WHOLE project (panoramas,
  intermediates, everything) for another generation-capable box.

Assets are gitignored: photographs of real places never enter git, and
nothing here publishes anything off this box.
