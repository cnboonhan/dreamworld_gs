# Third-party code

Vendored into this repository:

| Path | Upstream | License |
| --- | --- | --- |
| `docker/splat-generator/tools/threedgrut/` | [nv-tlabs/3dgrut](https://github.com/nv-tlabs/3dgrut) — export subtree only, used to write Isaac Sim NuRec USDZ | Apache-2.0 |
| `docker/splat-viewer/www/` | [antimatter15/splat](https://github.com/antimatter15/splat) — WebGL 3DGS viewer by Kevin Kwok; patched to resolve `?url=` against this origin and to load a `.cam.json` spawn pose | MIT |
| `docker/pano-viewer/www/` | equirect WebGL viewer engine from the `dream_editor` tool in the internal htx-robotics-release dreamworld project; reduced to a single panel, given a file picker and an aspect-ratio check | internal |

Fetched at build or run time, not vendored:

| Component | Upstream | License |
| --- | --- | --- |
| HY-World 2.0 | [Tencent-Hunyuan/HY-World-2.0](https://github.com/Tencent-Hunyuan/HY-World-2.0) — cloned at the pinned commit in the generator Dockerfile, patched with `docker/splat-generator/hyworld.patch` | see upstream `License.txt` |
| SAM 3 | Meta, via ModelScope `facebook/sam3` | Meta SAM 3 license |
| Wan 2.1 I2V, Qwen3-VL, Qwen-Image-Edit, WorldStereo, MoGe, ZIM, GroundingDINO | see each model card | respective licenses |

Model weights are downloaded by `just fetch-assets` into `assets/`, which is
not tracked here. Review the individual model licenses before any deployment
beyond evaluation — several are non-commercial or otherwise restricted.
