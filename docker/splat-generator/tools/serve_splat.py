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
    """Drive the camera along the capture path, leaving the view free.

    Position is never a free variable here: a capture only constrains the
    space its cameras saw, and flying out of it is what makes a splat look
    broken. So the tour rides the straight line fitted through the
    standpoints — playing forward on its own, or scrubbed by hand — while you
    keep hold of the camera's orientation. Each step translates the whole
    camera frame, so whatever you are looking at stays framed as you move.
    """
    import threading

    from render_video import capture_path, straight_path

    model = scene / "undistorted" / "sparse" / "0"
    if not model.exists():
        return
    centres, up = capture_path(model)
    if len(centres) < 2:
        return
    path, axis = straight_path(centres, 600)
    span = float(np.linalg.norm(path[-1] - path[0]))

    with server.gui.add_folder("tour"):
        play = server.gui.add_checkbox("play", False)
        secs = server.gui.add_slider("seconds end to end", 5.0, 120.0, 1.0, 30.0)
        where = server.gui.add_slider("along path", 0.0, 1.0, 0.001, 0.0)
        recentre = server.gui.add_button("face along path")

    last = {"pos": np.asarray(path[0], dtype=np.float64)}

    def at(t: float) -> np.ndarray:
        return path[int(np.clip(t, 0.0, 1.0) * (len(path) - 1))]

    def face_forward(client) -> None:
        """Absolute placement: on the path, looking the way it goes."""
        pos = at(where.value)
        client.camera.position = pos
        client.camera.look_at = pos + axis * max(1.0, span * 0.3)
        client.camera.up_direction = up

    def advance(t: float) -> None:
        """Translate the camera frame, so the view direction is untouched."""
        pos = np.asarray(at(t), dtype=np.float64)
        delta = pos - last["pos"]
        if not np.any(delta):
            return
        for client in server.get_clients().values():
            client.camera.position = np.asarray(client.camera.position) + delta
            client.camera.look_at = np.asarray(client.camera.look_at) + delta
        last["pos"] = pos

    @where.on_update
    def _(_) -> None:
        advance(where.value)

    @recentre.on_click
    def _(_) -> None:
        for client in server.get_clients().values():
            face_forward(client)
        last["pos"] = np.asarray(at(where.value), dtype=np.float64)

    @server.on_client_connect
    def _(client) -> None:
        face_forward(client)

    def playback() -> None:
        tick = 1 / 30
        while True:
            time.sleep(tick)
            if not play.value:
                continue
            t = where.value + tick / max(secs.value, 1e-3)
            where.value = t % 1.0 if t > 1.0 else t
            if t > 1.0:                       # wrapped: jump without dragging
                last["pos"] = np.asarray(at(where.value), dtype=np.float64)

    threading.Thread(target=playback, daemon=True).start()


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
