# Third-party code and models

## Vendored (ported into this repository)

| Path | Heritage | License |
| --- | --- | --- |
| `docker/dreamworld_editor/js/dw_splat.js`, `docker/dreamworld_viewer/www/viewer.js` | WebGL 3DGS renderer of [antimatter15/splat](https://github.com/antimatter15/splat) heritage — 32-byte records, counting-sort worker, packed-covariance texture, shaders | MIT |
| `docker/splat-generator/tools/threedgrut/` | export subtree of [nv-tlabs/3dgrut](https://github.com/nv-tlabs/3dgrut) — writes the Isaac Sim NuRec USDZ | Apache-2.0 |
| `docker/dreamworld_editor/js/dw_pano.js`, `docker/dreamworld_viewer/www/pano.js` | equirect panorama viewer from the internal htx-robotics dreamworld tools | internal |
| `docker/rmf-tools/generate_world.sh`, `postprocess_world.py`, `sim.launch.xml.template` | world generation from the internal dreamworld pipeline | internal |
| the sample `multilevel_office` map | same pipeline (robot meshes not included) | internal |

## Images built on

| Base | Ships | License |
| --- | --- | --- |
| `ghcr.io/open-rmf/rmf/rmf_demos` | [Open-RMF](https://github.com/open-rmf) (`rmf_traffic`, `rmf_fleet_adapter`, `rmf_traffic_editor` …), [Gazebo](https://gazebo.org/), ROS 2 Jazzy | Apache-2.0 |
| — plus, in that image | [noVNC](https://github.com/novnc/noVNC) / [websockify](https://github.com/novnc/websockify), unmodified Ubuntu packages | MPL-2.0 / LGPL-3.0 |
| `vllm/vllm-openai` | vLLM serving Qwen3-VL | Apache-2.0 |
| CUDA devel images | the generator and streamer stacks (PyTorch, gsplat, transformers, FlashDreams) | permissive |

## Model weights (fetched by `just fetch`, manifest in `scripts/models.txt`)

| Model | Role | License note |
| --- | --- | --- |
| tencent/HY-World-2.0 (+ WorldStereo, HunyuanWorld-Mirror, Wan 2.1 I2V base) | splat-world generation | **Tencent Hunyuan community license — territory and acceptable-use restricted; outputs must be identified as AI-generated in public contexts** |
| robbyant/lingbot-world-v2-14b-causal-fast | the live layer | Apache-2.0 |
| Wan-AI/Wan2.2-I2V-A14B | crossing videos | Apache-2.0 |
| Qwen/Qwen-Image-Edit-2509, Qwen/Qwen3-VL-8B | panorama variants; agent/planner | Apache-2.0 |
| facebook/sam3 (ModelScope), MoGe, ZIM, GroundingDINO, DINOv2 | the trajectory planner's perception | per-model; SAM 3 is gated on HF |

Weights and photographs stay in the gitignored `assets/` tree; nothing is
committed and nothing leaves the box at runtime. Publishing anything a
restricted model produced (the gh-pages bundle included) is a clearance
decision — see the license of the model that made it first.
