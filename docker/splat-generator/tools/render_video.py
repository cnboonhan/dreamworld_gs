"""Render a walkthrough video of a built world.

The camera rides `world.path.json`, the walk make_spawn_cam wrote beside the
splat — a generated world's corridor out of the building map, a reconstructed
one's walk through its own standpoints. Either way it is where the scene was
actually observed, and off it is what makes gaussian splats look bad, since
those regions were never constrained by any view.

    python render_video.py <scene-dir>

Writes <scene>/walkthrough.mp4.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
import torch

SH_C0 = 0.28209479177387814
# 720p at a natural walking field of view. Never varied, and the render is
# short enough that a bigger one would not buy anything the capture resolves.
WIDTH, HEIGHT, FOV, FPS = 1280, 720, 75.0, 30
WALK_MS = 0.8                        # unhurried, and steady enough to look at


def load_splat(ply: Path, device: str):
    from plyfile import PlyData

    v = PlyData.read(str(ply))["vertex"].data
    t = lambda a: torch.tensor(np.ascontiguousarray(a), device=device)
    return {
        "means": t(np.stack([v["x"], v["y"], v["z"]], 1)),
        "scales": torch.exp(t(np.stack([v[f"scale_{i}"] for i in range(3)], 1))),
        "quats": t(np.stack([v[f"rot_{i}"] for i in range(4)], 1)),
        "opacities": torch.sigmoid(t(np.array(v["opacity"]))),
        "colors": (t(np.stack([v[f"f_dc_{i}"] for i in range(3)], 1))
                   * SH_C0 + 0.5).clamp(0, 1),
    }


def load_splats(plys: list[Path], device: str):
    """Several splats as one, which is all "several splats" ever means here.

    They were each placed in the building's frame by the poses their capture
    recorded, so the union is a scene: concatenating the gaussians is the whole
    operation. Nothing is merged or blended, and the rasteriser cannot tell.
    """
    parts = [load_splat(p, device) for p in plys]
    return {k: torch.cat([p[k] for p in parts]) for k in parts[0]}


def along_polyline(pts: np.ndarray, up: np.ndarray, n_frames: int):
    """(eyes, targets) walking a polyline, one per frame.

    Parameterised by arc length rather than by point index, so the pace is
    even however the polyline happens to be sampled, and each frame looks the
    way the path goes *here* — a walk turns corners.
    """
    up = up / max(np.linalg.norm(up), 1e-9)
    step = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(step)])
    at = lambda u: np.stack([np.interp(np.clip(u, 0, s[-1]), s, pts[:, i])
                             for i in range(3)], 1)
    u = np.linspace(0, s[-1], n_frames)
    eyes = at(u)

    # a centred difference over a metre: defined at both ends, and it rounds a
    # corner rather than snapping at the waypoint
    look = max(1.0, s[-1] * 0.02)
    d = at(u + look) - at(u - look)
    d -= up[None] * (d @ up)[:, None]                    # keep it level
    n = np.linalg.norm(d, axis=1, keepdims=True)
    d = np.where(n > 1e-9, d / np.maximum(n, 1e-9), np.array([1.0, 0.0, 0.0]))
    return eyes, eyes + d * look


def route_path(doc: dict, n_frames: int):
    """(eyes, targets, up) along a planned route, one per frame."""
    up = np.asarray(doc.get("up") or [0.0, 0.0, 1.0], dtype=np.float64)
    up = up / max(np.linalg.norm(up), 1e-9)
    eyes, targets = along_polyline(np.asarray(doc["points"], dtype=np.float64),
                                   up, n_frames)
    return eyes, targets, up


def capture_path(model: Path) -> tuple[np.ndarray, np.ndarray]:
    """Standpoint centres in capture order, plus the scene's up direction.

    Views are named <panorama>_<k>, so grouping by prefix recovers one centre
    per standpoint rather than one per reprojected view.
    """
    import pycolmap

    rec = pycolmap.Reconstruction(model)
    ups = []
    for _, im in sorted(rec.images.items()):
        cfw = im.cam_from_world() if callable(im.cam_from_world) else im.cam_from_world
        m = np.asarray(cfw.matrix(), dtype=np.float64)
        ups.append(-m[:3, :3].T @ np.array([0.0, 1.0, 0.0]))  # camera Y is down
    up = np.mean(np.stack(ups), 0)

    # A frame is one standpoint: the rig at one instant, holding the views of
    # a single panorama. Reading them directly beats grouping by filename —
    # the poses share a centre by construction rather than by agreement.
    frames = getattr(rec, "frames", None)
    if frames:
        centres, names = [], []
        for fid, fr in sorted(frames.items()):
            if not fr.has_pose:
                continue
            rfw = fr.rig_from_world
            m = np.asarray(rfw.matrix(), dtype=np.float64)
            centres.append(-m[:3, :3].T @ m[:3, 3])
            # frames come back in registration order, so sort by the standpoint
            # the images name rather than by when the solver reached it
            names.append(min((rec.images[d.id].name for d in fr.data_ids
                              if d.id in rec.images), default=str(fid)))
        if centres:
            order = np.argsort([Path(n).name for n in names])
            return np.stack(centres)[order], up / np.linalg.norm(up)

    # no rig in this reconstruction (built before the upgrade): fall back to
    # grouping views by the panorama their filename names
    groups: dict[str, list[np.ndarray]] = {}
    for _, im in sorted(rec.images.items()):
        cfw = im.cam_from_world() if callable(im.cam_from_world) else im.cam_from_world
        m = np.asarray(cfw.matrix(), dtype=np.float64)
        key = Path(im.name).name if "/" in im.name else im.name.rsplit("_", 1)[0]
        groups.setdefault(key, []).append(-m[:3, :3].T @ m[:3, 3])
    centres = np.stack([np.median(np.stack(v), 0) for _, v in sorted(groups.items())])
    return centres, up / np.linalg.norm(up)


def straight_path(centres: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Points along the least-squares line through the standpoints.

    Straight is usually the honest path: a capture walk is roughly a line,
    and a spline detours off it to hit every standpoint exactly — off the
    line is where the reconstruction has least support. Returns the points
    and the direction of travel, in capture order.
    """
    centred = centres - centres.mean(0)
    axis = np.linalg.svd(centred, full_matrices=False)[2][0]
    t = centred @ axis
    if t[0] > t[-1]:                      # travel the way the walk went
        axis, t = -axis, -t
    mid = centres.mean(0)
    return np.linspace(mid + axis * t.min(), mid + axis * t.max(), n), axis


def look_at(eye: np.ndarray, target: np.ndarray, up: np.ndarray) -> np.ndarray:
    """World->camera matrix, OpenCV convention (x right, y down, z forward)."""
    fwd = target - eye
    fwd /= max(np.linalg.norm(fwd), 1e-9)
    right = np.cross(fwd, up)
    right /= max(np.linalg.norm(right), 1e-9)
    down = np.cross(fwd, right)
    R = np.stack([right, down, fwd])
    m = np.eye(4)
    m[:3, :3] = R
    m[:3, 3] = -R @ eye
    return m


def plan_path(scene: Path, n_frames: int):
    """(eyes, targets, up) for the camera, one entry per frame.

    `world.path.json` is the walk itself, written by make_spawn_cam whichever
    way it knew it: a generated world's corridor comes out of the building map,
    a reconstructed one's out of its COLMAP model. One source for both, so the
    camera here only has to ride it.
    """
    doc = json.loads((scene / "world.path.json").read_text())
    return route_path(doc, n_frames)


def render_frames(eyes, targets, up, out: Path, plys: list[Path]) -> Path:
    """Rasterise every frame of the path into a raw mp4; returns its path."""
    import cv2
    from gsplat import rasterization

    dev = "cuda:0"
    splat = load_splats(plys, dev)
    f = 0.5 * WIDTH / np.tan(np.radians(FOV) * 0.5)
    K = torch.tensor([[[f, 0, WIDTH / 2], [0, f, HEIGHT / 2], [0, 0, 1]]],
                     dtype=torch.float32, device=dev)
    tmp = out.with_suffix(".raw.mp4")
    writer = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"mp4v"),
                             FPS, (WIDTH, HEIGHT))
    quats = torch.nn.functional.normalize(splat["quats"], dim=1)
    n = len(eyes)
    for i, (eye, tgt) in enumerate(zip(eyes, targets)):
        vm = torch.tensor(look_at(eye, tgt, up), dtype=torch.float32, device=dev)
        with torch.no_grad():
            rgb, _, _ = rasterization(
                splat["means"], quats, splat["scales"], splat["opacities"],
                splat["colors"], vm[None], K, WIDTH, HEIGHT,
                near_plane=0.01, rasterize_mode="classic")
        frame = (rgb[0].clamp(0, 1) * 255).byte().cpu().numpy()
        writer.write(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        if i % FPS == 0:
            print(f"  {i}/{n} frames", flush=True)
    writer.release()
    return tmp


def encode(tmp: Path, out: Path) -> None:
    """Re-encode to H.264 so browsers and players accept it."""
    import imageio_ffmpeg

    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(tmp),
                    "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
                    str(out)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tmp.unlink()


def frames_for(metres: float) -> int:
    """How many frames a walk of this length earns.

    A corridor here is between one and six metres and a route is tens, so one
    duration for all of them is a crawl through some and a dash through
    others. Walking speed instead, floored so the shortest is still watchable.
    """
    return int(max(6.0, metres / WALK_MS) * FPS)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scene", type=Path)
    args = ap.parse_args()

    doc = json.loads((args.scene / "world.path.json").read_text())
    n_frames = frames_for(doc["length_m"])
    out = args.scene / "walkthrough.mp4"
    eyes, targets, up = plan_path(args.scene, n_frames)
    tmp = render_frames(eyes, targets, up, out, [args.scene / "world.ply"])
    encode(tmp, out)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f}MB, {n_frames} frames)")


if __name__ == "__main__":
    main()
