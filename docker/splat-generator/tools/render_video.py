"""Render a walkthrough video of a reconstructed world.

The camera follows the capture path, because that is where the scene was
actually observed — straying from it is what makes gaussian splats look bad,
since those regions were never constrained by any view. By default the path
is the straight line fitted through the standpoints; --path spline visits
each one exactly, at the cost of weaving off that line.

    python render_video.py <scene-dir> [--seconds 20] [--fps 30]
                           [--path line|spline|orbit] [--fov 75]

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


def route_path(doc: dict, n_frames: int):
    """(eyes, targets, up) along a planned route, one per frame.

    Parameterised by arc length rather than by point index, so the pace is
    even however the polyline happens to be sampled, and each frame looks the
    way the path goes *here* — a route turns corners.
    """
    pts = np.asarray(doc["points"], dtype=np.float64)
    up = np.asarray(doc.get("up") or [0.0, 0.0, 1.0], dtype=np.float64)
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
    return eyes, eyes + d * look, up


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


def catmull_rom(points: np.ndarray, n: int, loop: bool = True) -> np.ndarray:
    """Smooth spline through the given points."""
    pts = np.vstack([points, points[:1]]) if loop else points
    k = len(pts)
    idx = lambda i: pts[i % k] if loop else pts[np.clip(i, 0, k - 1)]

    out = []
    segments = k if loop else k - 1
    for s in range(segments):
        p0, p1, p2, p3 = idx(s - 1), idx(s), idx(s + 1), idx(s + 2)
        for j in range(max(1, n // segments)):
            t = j / max(1, n // segments)
            t2, t3 = t * t, t * t * t
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t
                              + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
    return np.stack(out)


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


def walked_path(scene: Path) -> tuple[np.ndarray, np.ndarray, int]:
    """(points, up, standpoints) for this scene's walk, however it was obtained.

    `world.path.json` is the walk itself — written by whichever stage knew it.
    A simulated capture recorded its standpoints and has no reconstruction to
    read them back out of, so this is the only place they exist; a real one is
    reconstructed first and the sidecar is derived from that. Preferring the
    sidecar means one path source for both, and the COLMAP read below is the
    fallback for splats built before it was written.
    """
    sidecar = scene / "world.path.json"
    if sidecar.is_file():
        doc = json.loads(sidecar.read_text())
        pts = np.asarray(doc.get("points") or [], dtype=np.float64)
        if len(pts) > 1:
            up = np.asarray(doc.get("up") or [0.0, 0.0, 1.0], dtype=np.float64)
            return (pts, up / max(np.linalg.norm(up), 1e-9),
                    int(doc.get("standpoints") or len(pts)))
    centres, up = capture_path(scene / "undistorted" / "sparse" / "0")
    return centres, up, len(centres)


def plan_path(scene: Path, kind: str, n_frames: int):
    """(eyes, targets, up) for the camera, one entry per frame."""
    centres, up, n_stand = walked_path(scene)

    if kind == "orbit":
        mid = centres.mean(0)
        radius = float(np.linalg.norm(centres - mid, axis=1).max()) * 1.6 + 1e-3
        ang = np.linspace(0, 2 * np.pi, n_frames, endpoint=False)
        basis = np.eye(3)[np.argsort(np.abs(up))[:2]]  # two axes most level
        eyes = mid + radius * (np.outer(np.cos(ang), basis[0])
                               + np.outer(np.sin(ang), basis[1]))
        targets = np.repeat(mid[None], n_frames, 0)
    elif kind == "spline":
        eyes = catmull_rom(centres, n_frames)
        # aim a little ahead along the path so the motion reads as walking
        targets = np.roll(eyes, -max(2, n_frames // 60), axis=0)
    else:
        eyes, axis = straight_path(centres, n_frames)
        span = float(np.linalg.norm(eyes[-1] - eyes[0]))
        targets = eyes + axis * max(1.0, span * 0.3)
    return eyes, targets, up, n_stand


def render_frames(scene: Path, eyes, targets, up, out: Path,
                  width: int, height: int, fov: float, fps: int,
                  plys: list[Path] | None = None) -> Path:
    """Rasterise every frame of the path into a raw mp4; returns its path."""
    import cv2
    from gsplat import rasterization

    dev = "cuda:0"
    splat = load_splats(plys or [scene / "world.ply"], dev)
    f = 0.5 * width / np.tan(np.radians(fov) * 0.5)
    K = torch.tensor([[[f, 0, width / 2], [0, f, height / 2], [0, 0, 1]]],
                     dtype=torch.float32, device=dev)
    tmp = out.with_suffix(".raw.mp4")
    writer = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"mp4v"),
                             fps, (width, height))
    quats = torch.nn.functional.normalize(splat["quats"], dim=1)
    n = len(eyes)
    for i, (eye, tgt) in enumerate(zip(eyes, targets)):
        vm = torch.tensor(look_at(eye, tgt, up), dtype=torch.float32, device=dev)
        with torch.no_grad():
            rgb, _, _ = rasterization(
                splat["means"], quats, splat["scales"], splat["opacities"],
                splat["colors"], vm[None], K, width, height,
                near_plane=0.01, rasterize_mode="classic")
        frame = (rgb[0].clamp(0, 1) * 255).byte().cpu().numpy()
        writer.write(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        if i % fps == 0:
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scene", type=Path)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fov", type=float, default=75.0)
    ap.add_argument("--path", choices=("line", "spline", "orbit"),
                    default="line",
                    help="line: straight along the standpoints' best-fit axis "
                         "(default); spline: through each standpoint exactly; "
                         "orbit: circle the centre (expect off-path artifacts)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    n_frames = int(args.seconds * args.fps)
    out = args.out or args.scene / "walkthrough.mp4"
    eyes, targets, up, n_stand = plan_path(args.scene, args.path, n_frames)
    tmp = render_frames(args.scene, eyes, targets, up, out,
                        args.width, args.height, args.fov, args.fps)
    encode(tmp, out)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f}MB, "
          f"{n_frames} frames, {n_stand} standpoints)")


if __name__ == "__main__":
    main()
