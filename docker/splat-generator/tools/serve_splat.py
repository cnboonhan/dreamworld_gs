"""Serve a world rendered by gsplat itself, not by a WebGL reimplementation.

The browser viewer is convenient but lossy: it sorts splats approximately,
resamples textures down, and we strip spherical harmonics from its copy, so
view-dependent shading is flat. Here the browser only sends a camera pose and
receives a frame; the rasteriser is the same CUDA one that trained the scene,
at full spherical-harmonic fidelity. The cost is a GPU held open and a frame
round-trip per movement.

    python serve_splat.py <scene-dir> [--port 8083]
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import torch

SH_C0 = 0.28209479177387814


def load_splat(ply: Path, device: str) -> dict:
    from plyfile import PlyData

    v = PlyData.read(str(ply))["vertex"].data
    t = lambda a: torch.tensor(np.ascontiguousarray(a), device=device)
    g = {
        "means": t(np.stack([v["x"], v["y"], v["z"]], 1)),
        "scales": torch.exp(t(np.stack([v[f"scale_{i}"] for i in range(3)], 1))),
        "quats": torch.nn.functional.normalize(
            t(np.stack([v[f"rot_{i}"] for i in range(4)], 1)), dim=1),
        "opacities": torch.sigmoid(t(np.array(v["opacity"]))),
    }
    dc = t(np.stack([v[f"f_dc_{i}"] for i in range(3)], 1))
    rest = sorted((n for n in v.dtype.names if n.startswith("f_rest_")),
                  key=lambda n: int(n.split("_")[-1]))
    if rest:
        # higher-order harmonics: what gives glass and polished floors their
        # view-dependent look, and what the web copy throws away
        extra = t(np.stack([v[n] for n in rest], 1))
        k = extra.shape[1] // 3
        g["sh"] = torch.cat([dc[:, None, :],
                             extra.reshape(-1, 3, k).transpose(1, 2)], dim=1)
        g["sh_degree"] = int(round((k + 1) ** 0.5)) - 1
    else:
        g["colors"] = (dc * SH_C0 + 0.5).clamp(0, 1)
        g["sh_degree"] = None
    return g


def add_tour(server, scene: Path) -> None:
    """A slider that walks the camera along the capture path.

    Free flight is the honest way to inspect a reconstruction and also the
    quickest way to end up somewhere no camera ever stood, where the geometry
    was never constrained and everything smears. The tour keeps the camera on
    the spline through the standpoints, which is the part of the space the
    panoramas actually observed.
    """
    from render_video import capture_path, catmull_rom

    model = scene / "undistorted" / "sparse" / "0"
    if not model.exists():
        return
    centres, up = capture_path(model)
    if len(centres) < 2:
        return
    path = catmull_rom(centres, 600)

    with server.gui.add_folder("tour"):
        follow = server.gui.add_checkbox("stay on capture path", False)
        where = server.gui.add_slider("along path", 0.0, 1.0, 0.002, 0.0)
        step = server.gui.add_button_group("standpoint", ("prev", "next"))

    def place(t: float) -> None:
        i = int(np.clip(t, 0, 1) * (len(path) - 1))
        eye = path[i]
        ahead = path[(i + len(path) // 60) % len(path)]
        for client in server.get_clients().values():
            client.camera.position = eye
            client.camera.look_at = ahead
            client.camera.up_direction = up

    @where.on_update
    def _(_) -> None:
        if follow.value:
            place(where.value)

    @follow.on_update
    def _(_) -> None:
        if follow.value:
            place(where.value)

    @step.on_click
    def _(_) -> None:
        n = len(centres)
        delta = (1.0 / n) * (1 if step.value == "next" else -1)
        where.value = float(np.clip(where.value + delta, 0.0, 1.0))
        follow.value = True
        place(where.value)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scene", type=Path)
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--device", default="cuda:0")
    args = ap.parse_args()

    import nerfview
    import viser
    from gsplat import rasterization

    g = load_splat(args.scene / "world.ply", args.device)
    n = g["means"].shape[0]
    print(f"{n:,} gaussians"
          f"{' with SH degree ' + str(g['sh_degree']) if g['sh_degree'] else ''}",
          flush=True)

    @torch.no_grad()
    def render(camera_state: nerfview.CameraState, img_wh: tuple[int, int]):
        W, H = img_wh
        c2w = torch.tensor(camera_state.c2w, dtype=torch.float32, device=args.device)
        K = torch.tensor(camera_state.get_K(img_wh), dtype=torch.float32,
                         device=args.device)
        kwargs = ({"colors": g["sh"], "sh_degree": g["sh_degree"]}
                  if g["sh_degree"] is not None else {"colors": g["colors"]})
        rgb, _, _ = rasterization(
            g["means"], g["quats"], g["scales"], g["opacities"],
            viewmats=torch.linalg.inv(c2w)[None], Ks=K[None],
            width=W, height=H, near_plane=0.01, rasterize_mode="classic",
            **kwargs)
        return rgb[0].clamp(0, 1).cpu().numpy()

    server = viser.ViserServer(port=args.port, verbose=False)
    nerfview.Viewer(server=server, render_fn=render, mode="rendering")
    add_tour(server, args.scene)
    print(f"serving {args.scene.name} on :{args.port}", flush=True)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
