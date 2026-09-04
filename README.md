# dreamworld_gs

<div align="center">

## 🌐 &nbsp;[**► WALK THE LIVE DEMO ◄**](https://cnboonhan.github.io/dreamworld_gs)

*in your browser, nothing to install — walk the splat worlds,<br>
cross doors and ride the lifts through their generated videos*

</div>

---

Grow a photorealistic, walkable twin of a building — one 360 photo at a
time. RMF simulates the infrastructure (doors, lifts) from an annotated
floorplan; a world model brings the view you stand in to life under a
typed prompt; a video model fills the seconds between two photographs
(a door opening, a lift ride); gaussian-splat worlds carry the
walkthrough; an agent drives it all by tool call. Everything runs on one
box, offline, behind one forwarded port.

```
ssh -L 8080:localhost:8080 <box>        one tunnel carries every surface
```

## Launch

| tier | command | needs | you get |
| --- | --- | --- | --- |
| **demo** | `just bundle` → `just demo` | a browser | static walkthrough: splat worlds + crossing videos, no Docker, no GPU |
| **minimal** | `just minimal` | an OpenAI-compatible VLM endpoint (`DW_VLM_URL/MODEL/KEY` in `.env`) | walk + simulate + command an already-generated dreamworld |
| **full** | `just up` | 8 GPUs, `just fetch` once (~1.2 TB of weights) | everything: authoring UIs and all four generators |

## Surfaces

| surface | what |
| --- | --- |
| `/dreamworld_viewer` | **live**: stand in a waypoint's panorama; hold still and it comes alive under your prompt |
| `/dreamworld_viewer/walk.html` | splat walkthrough: generated worlds, spin-and-cross transitions |
| `/harness` | dashboard, tools, the mission agent |
| `/rmfsim` | the building under simulation (Gazebo + RMF, noVNC) |
| `/dreamworld_editor` | grow it: vertices, panoramas, alignment, generation |
| `/sim_editor` | author the building structure (traffic editor, full tier) |

## Compute and generation times

Measured on this box (NVIDIA H200s), generating the sample office:

| job | model | cards | VRAM | time |
| --- | --- | --- | --- | --- |
| splat world | HY-World 2.0 | 2 | large | **~31 min** per place (15–20 min on 4 cards) |
| live view | LingBot-World v2 14B causal-fast | 1 | ~80 GB | **~4.5 fps** streamed, ~20 s to warm |
| crossing video | Wan 2.2 FLF (×2 instances) | 1 each | ~72 GB | **6–7 min** per video |
| panorama variant | Qwen-Image-Edit-2509 | 1 | ~60 GB | **~1 min** per edit |
| mission agent / planner | Qwen3-VL-8B (vLLM) | 1 | ~17 GB | interactive |

A building of V places and E edges ≈ `V × 31 min` of splat time +
`2E × 6.5 min` of video time (halved across the two wangen instances).
Queues run unattended; the editor toasts each completion.

## The live layer

Pan with **A/D W/S** (**Q/E** zooms) through real pixels; hold still and
the on-screen view seeds the world model, which streams over the top
until your next key. Every generated second starts from a true frame.

| `.env` knob | default | meaning |
| --- | --- | --- |
| `DW_STREAMER_RUNNER` | `lingbot-world-v2-14b-causal-fast-taehv-window15-sink3` | the model; `causal-forcing-wan2.1-i2v-1.3b-framewise` is 11× smaller but wanders |
| `DW_STREAMER_LOOP_S` | `30` | seconds per rollout before it returns to the photograph |
| `DW_STREAMER_TAIL_S` | `5` | last seconds cross-fade home, so the loop closes instead of cutting (`0` = cut) |
| `DW_STREAMER_DRIFT_PX` | `48` | re-seed once the view slides this far (`0` = off) |

`GET /streamer/status` — frames, fps, drift · `GET /streamer/seed.jpg` —
the exact frame the model sees.

## The flow

Trace the building in `/sim_editor` → drop vertices/edges in
`/dreamworld_editor` → upload a 360 per vertex, align it, generate its
splat → generate each edge's crossing videos (prompts default by what
the map says the walk passes) → walk it, or hand the harness a mission.
Traffic lights everywhere: red not started · yellow in progress · green
complete. Full reference: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Carrying it elsewhere

| move | command | carries | size |
| --- | --- | --- | --- |
| demo | `just bundle`, serve the dir (or `just pages` for GitHub Pages — push is deliberately manual) | viewer + worlds + videos, static | ~30 MB/world |
| project | `just pack` → `just unpack <tar>` | the whole project tree — the only part that cannot be re-downloaded | tens of GB |
| project → private HF | `just push-hf <owner>` | same, as a private dataset repo | tens of GB |
| weights | `just fetch` on the new box | everything in `scripts/models.txt`, skipping what is cached | ~1.2 TB |

Plus a `.env`: `just up` writes `DW_PROJECT` and `DW_UID/DW_GID`; copy
any GPU assignments you tuned. `assets/cache/` (streamer kernel caches,
~280 MB) is machine-specific — never copy it, always safe to delete.

Assets are gitignored: photographs of real places never enter git, and
nothing here publishes anything off this box.
