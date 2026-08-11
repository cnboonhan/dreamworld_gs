# Third-party code

Vendored into this repository:

| Path | Upstream | License |
| --- | --- | --- |
| `docker/splat-generator/tools/threedgrut/` | [nv-tlabs/3dgrut](https://github.com/nv-tlabs/3dgrut) — export subtree only, used to write Isaac Sim NuRec USDZ | Apache-2.0 |
| `docker/splat-viewer/www/` | [antimatter15/splat](https://github.com/antimatter15/splat) — WebGL 3DGS viewer by Kevin Kwok; patched to resolve `?url=` against this origin and to load a `.cam.json` spawn pose | MIT |
| `docker/pano-viewer/www/` | equirect WebGL viewer engine from the `dream_editor` tool in the internal htx-robotics-release dreamworld project; reduced to a single panel, given a file picker and an aspect-ratio check | internal |

Installed at build time, not vendored:

| Component | Upstream | License |
| --- | --- | --- |
| gsplat 1.5.3 | [nerfstudio-project/gsplat](https://github.com/nerfstudio-project/gsplat) — the CUDA rasteriser | Apache-2.0 |
| pycolmap 3.12.6 | [colmap/colmap](https://github.com/colmap/colmap) — structure from motion | BSD-3-Clause |
| PyTorch, OpenCV, torchmetrics, plyfile, usd-core, nvidia-ncore | see each project | permissive (BSD / Apache-2.0 / MIT) |

**No pretrained model weights.** This pipeline measures geometry from the
photographs rather than imagining it, so nothing is downloaded at runtime and
nothing is mounted but the projects themselves.

Earlier revisions carried a generative path — HY-World 2.0, and with it SAM 3,
Wan 2.1 I2V, Qwen3-VL, Qwen-Image-Edit, WorldStereo, MoGe, ZIM and
GroundingDINO — several of which are non-commercial or otherwise restricted.
All of it was removed, along with the weights it needed. If you restore that
path from history, those licenses apply again and want reviewing first.

## Open-RMF

The `rmf-tools` image is built `FROM ghcr.io/open-rmf/rmf/rmf_demos`, which
ships [Open-RMF](https://github.com/open-rmf) — `rmf_traffic`, `rmf_task`,
`rmf_fleet_adapter`, `rmf_building_map_tools` and `rmf_traffic_editor` — under
the **Apache License 2.0**, together with [Gazebo](https://gazebo.org/)
(Apache-2.0) and ROS 2 Jazzy (Apache-2.0).

The world generation and its SDF fixups (`generate_world.sh`,
`postprocess_world.py`, `sim.launch.xml.template`) are ported from the internal
`dreamworld` pipeline, trimmed to what a simulated — rather than generatively
rendered — building needs.

`samples/multilevel_office/` is the `multilevel_office` map from that same
pipeline (its robot meshes are not included, since no robot is simulated here).

The browser view of both Qt applications uses
[noVNC](https://github.com/novnc/noVNC) (MPL-2.0) and
[websockify](https://github.com/novnc/websockify) (LGPL-3.0), installed from
Ubuntu packages inside the image and not modified.
