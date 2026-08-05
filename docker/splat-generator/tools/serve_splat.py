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
    standpoints — playing forward on its own, or scrubbed by hand — while the
    direction you are facing stays yours.

    Position is set absolutely from the path each tick, never read back from
    the client and nudged: a round-trip to the browser takes long enough that
    read-modify-write races itself, which reads as the camera jumping between
    standpoints instead of gliding. Only the view *direction* is taken from
    the client, so turning the camera still works while it moves.
    """
    import threading

    from render_video import capture_path, straight_path

    model = scene / "undistorted" / "sparse" / "0"
    if not model.exists():
        return
    centres, up = capture_path(model)
    if len(centres) < 2:
        return
    path, axis = straight_path(centres, 2000)
    span = float(np.linalg.norm(path[-1] - path[0]))
    ahead = max(1.0, span * 0.3)

    with server.gui.add_folder("tour"):
        play = server.gui.add_checkbox("play", False)
        secs = server.gui.add_slider("seconds end to end", 5.0, 120.0, 1.0, 30.0)
        bounce = server.gui.add_checkbox("turn around at the end", True)
        where = server.gui.add_slider("along path", 0.0, 1.0, 0.001, 0.0)
        recentre = server.gui.add_button("face along path")

    t = {"v": 0.0, "dir": 1.0}          # authoritative position along the path

    def at(u: float) -> np.ndarray:
        return path[int(np.clip(u, 0.0, 1.0) * (len(path) - 1))]

    def move(u: float, reface: bool = False) -> None:
        pos = at(u)
        for c in server.get_clients().values():
            eye = np.asarray(c.camera.position, dtype=np.float64)
            look = np.asarray(c.camera.look_at, dtype=np.float64)
            d = look - eye
            n = float(np.linalg.norm(d))
            if reface or n < 1e-6:
                d, n = np.asarray(axis, dtype=np.float64), ahead
            c.camera.position = pos
            c.camera.look_at = pos + d / n * n
            c.camera.up_direction = up

    @where.on_update
    def _(_) -> None:
        # only react to a human scrubbing; playback updates this for display
        if abs(where.value - t["v"]) > 0.004:
            t["v"] = where.value
            move(t["v"])

    @recentre.on_click
    def _(_) -> None:
        move(t["v"], reface=True)

    @server.on_client_connect
    def _(client) -> None:
        move(t["v"], reface=True)

    def playback() -> None:
        tick, shown = 1 / 30, 0.0
        while True:
            time.sleep(tick)
            if not play.value:
                continue
            t["v"] += t["dir"] * tick / max(secs.value, 1e-3)
            if t["v"] >= 1.0 or t["v"] <= 0.0:
                if bounce.value:                  # walk back rather than cut
                    t["dir"] *= -1
                t["v"] = float(np.clip(t["v"], 0.0, 1.0)) if bounce.value else 0.0
            move(t["v"])
            if abs(t["v"] - shown) > 0.02:        # the slider is a readout;
                shown = t["v"]                    # updating it 30x/s is noise
                where.value = t["v"]

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
