"""Build a COLMAP model from poses we already know, instead of inferring them.

A simulated capture knows exactly where its camera stood. Structure from motion
exists to recover that from images alone, which a real 360 capture needs and a
simulated one does not — and an empty corridor of flat planes is close to the
worst case for it, because views of a plane leave camera position and depth
trading off against each other however well textured it is.

So when `poses.json` sits beside the panoramas, the poses go straight in and
only the points are triangulated. The splat is then born in the building's
coordinate frame: no alignment, no metric scale to infer from walked distance,
no direction to guess.

The one thing to get right is the frame. `capture.py` writes an equirectangular
image in the gz convention (+X forward, +Y left, +Z up), and
`equirect_to_pinhole` reads one in its own (Y as the pole axis). Both are
standard layouts — top of image is up — but they disagree about which world
axis that is, so a pose has to be carried across. Equating the two mappings
pixel for pixel gives a fixed rotation, derived in `GZ_TO_PANO` below.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

# capture.py:      d_gz = [cos(lat)cos(lon), cos(lat)sin(lon), sin(lat)]
#                  with lat = +pi/2 at the top of the image
# equirect_to_pinhole: lon = atan2(d.x, d.z), lat = asin(d.y)
#                  with lat = -pi/2 at the top of the image
#
# Same pixel, so lon matches and lat negates, giving
#     d_pano.x =  d_gz.y      d_pano.y = -d_gz.z      d_pano.z =  d_gz.x
GZ_TO_PANO = np.array([[0.0, 1.0, 0.0],
                       [0.0, 0.0, -1.0],
                       [1.0, 0.0, 0.0]])


def load_poses(panos: Path) -> list[dict] | None:
    """The standpoints a simulated capture recorded, or None for a real one."""
    f = Path(panos) / "poses.json"
    if not f.is_file():
        return None
    try:
        doc = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    pts = doc.get("standpoints") or []
    return pts or None


def view_rotation(yaw: float, pitch: float) -> np.ndarray:
    """Camera rays -> panorama frame, exactly as equirect_to_pinhole builds it."""
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    return (np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
            @ np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]]))


def cam_from_world(centre_gz, yaw: float, pitch: float):
    """(R, t) placing one reprojected view, in building coordinates.

    A world point goes into the panorama's frame by GZ_TO_PANO after moving the
    origin to the standpoint, then into the camera by the inverse of that
    view's rotation.
    """
    R = view_rotation(yaw, pitch).T @ GZ_TO_PANO
    return R, -R @ np.asarray(centre_gz, dtype=np.float64)


def seed_from_range(panos_dir: Path, stand: list[dict], want: int, logger):
    """A coloured point cloud of the surfaces the capture actually saw.

    Every camera centre on a corridor walk sits on one line, so depth along a
    ray is barely constrained: gaussians will happily settle wherever they were
    initialised and still reproduce every training view. Initialisation is
    therefore not a detail here, it is the geometry. Seeded with uniform noise
    in a box, a 2 m corridor came out smeared over 6.7 m.

    So the simulated pipeline starts from the range maps its capture recorded.
    Points are drawn with probability proportional to cos(lat), because an
    equirectangular image devotes far more pixels to the poles than they
    subtend, and sampling it flat would put most of the cloud on the ceiling.

    Returns None when there are no range maps — a capture from before they
    were recorded, which then falls back to noise.
    """
    import cv2

    pts, rgb = [], []
    per = max(1, want // max(1, len(stand)))
    rng = np.random.default_rng(0)
    for sp in stand:
        stem = Path(sp["image"]).stem
        f = panos_dir / f"{stem}.range.npy"
        img = panos_dir / sp["image"]
        if not f.is_file() or not img.is_file():
            continue
        R = np.load(f).astype(np.float64)
        H, W = R.shape
        colour = cv2.cvtColor(cv2.imread(str(img)), cv2.COLOR_BGR2RGB)
        if colour.shape[:2] != (H, W):
            colour = cv2.resize(colour, (W, H), interpolation=cv2.INTER_AREA)

        lat = math.pi / 2 - (np.arange(H) + 0.5) / H * math.pi
        w = np.repeat(np.cos(lat)[:, None], W, 1)      # equal solid angle
        w = np.where(np.isfinite(R) & (R > 1e-3) & (R < 30.0), w, 0.0).ravel()
        if not w.any():
            continue
        idx = rng.choice(w.size, size=min(per, int((w > 0).sum())),
                         replace=False, p=w / w.sum())
        r, c = np.divmod(idx, W)
        lon = (c + 0.5) / W * 2 * math.pi - math.pi
        la = math.pi / 2 - (r + 0.5) / H * math.pi
        cl = np.cos(la)
        d = np.stack([cl * np.cos(lon), cl * np.sin(lon), np.sin(la)], -1)
        pts.append(np.asarray(sp["xyz"], dtype=np.float64) + d * R.ravel()[idx][:, None])
        rgb.append(colour.reshape(-1, 3)[idx] / 255.0)

    if not pts:
        return None
    P = np.concatenate(pts).astype(np.float32)
    C = np.concatenate(rgb).astype(np.float32)
    logger.info("seeded from %d recorded range map(s): %d surface points, "
                "spanning %s m", len(pts), len(P),
                np.round(P.max(0) - P.min(0), 1).tolist())
    return P, C


def depth_view(rng_map: np.ndarray, yaw: float, pitch: float,
               fov_deg: float, size: int) -> np.ndarray:
    """The recorded range, resampled into one pinhole view as z-depth.

    Sampled exactly as `equirect_to_pinhole` samples the colour, so this lands
    pixel for pixel on the training image it supervises. Nearest-neighbour,
    not linear: interpolating across a depth discontinuity invents a surface
    halfway between a wall and whatever is behind it.
    """
    import cv2

    f = 0.5 * size / math.tan(math.radians(fov_deg) * 0.5)
    j, i = np.meshgrid(np.arange(size), np.arange(size), indexing="xy")
    dirs = np.stack([(j - size * 0.5) / f, (i - size * 0.5) / f,
                     np.ones_like(j, dtype=np.float64)], -1)
    n = np.linalg.norm(dirs, axis=-1, keepdims=True)
    d = (dirs / n) @ view_rotation(yaw, pitch).T

    lon = np.arctan2(d[..., 0], d[..., 2])
    lat = np.arcsin(np.clip(d[..., 1], -1, 1))
    H, W = rng_map.shape
    rng = cv2.remap(rng_map.astype(np.float32),
                    ((lon / (2 * math.pi) + 0.5) * W).astype(np.float32),
                    ((lat / math.pi + 0.5) * H).astype(np.float32),
                    cv2.INTER_NEAREST, borderMode=cv2.BORDER_WRAP)
    # range along the ray -> depth along the optical axis, which is what the
    # rasteriser reports. dirs was divided by n, so the cosine is 1/n.
    return (rng / n[..., 0]).astype(np.float32)


def load(panos_dir: Path, images: Path, angles, size: int, fov: float,
         downscale: int, device: str, logger):
    """Posed images and seed points for training, straight from the capture.

    The trainer needs intrinsics, view matrices, images and some starting
    points. `load_colmap` gets those out of a reconstruction — which is the only
    reason a simulated capture was building one at all, laundering poses we
    already had through COLMAP's rigs, frames, sensors and database consistency
    rules just to hand them back. This skips it.

    Seed points are random within the walked volume. 3DGS densifies from
    whatever it starts with, and a corridor's true geometry is not knowable
    without triangulating — which is the work being avoided.
    """
    import cv2
    import torch

    stand = load_poses(panos_dir)
    f = 0.5 * size / math.tan(math.radians(fov) * 0.5)
    Ks, viewmats, imgs, centres, depths = [], [], [], [], []
    for sp in stand:
        stem = Path(sp["image"]).stem
        centres.append(np.asarray(sp["xyz"], dtype=np.float64))
        rng_map = None
        rf = panos_dir / f"{stem}.range.npy"
        if rf.is_file():
            rng_map = np.load(rf).astype(np.float32)
        for k_i, (yaw, pitch) in enumerate(angles):
            path = images / f"view_{k_i:03d}" / f"{stem}.jpg"
            if not path.is_file():
                continue
            img = cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)
            fx = fy = f
            cx = cy = size / 2.0
            if downscale > 1:
                img = cv2.resize(img, (img.shape[1] // downscale,
                                       img.shape[0] // downscale),
                                 interpolation=cv2.INTER_AREA)
                fx, fy, cx, cy = (v / downscale for v in (fx, fy, cx, cy))
            Ks.append([[fx, 0, cx], [0, fy, cy], [0, 0, 1]])
            R, t = cam_from_world(sp["xyz"], yaw, pitch)
            m = np.eye(4, dtype=np.float32)
            m[:3, :3] = R
            m[:3, 3] = t
            viewmats.append(m)
            imgs.append(torch.from_numpy(img))
            if rng_map is None:
                depths.append(None)
            else:
                dv = depth_view(rng_map, yaw, pitch, fov, size)
                if downscale > 1:
                    dv = cv2.resize(dv, (dv.shape[1] // downscale,
                                         dv.shape[0] // downscale),
                                    interpolation=cv2.INTER_NEAREST)
                depths.append(torch.from_numpy(dv))

    C = np.stack(centres)
    scale = float(np.linalg.norm(C - C.mean(0), axis=1).max()) * 1.1
    scale = max(scale, 1e-3)

    seed = seed_from_range(panos_dir, stand, 200_000, logger)
    if seed is not None:
        pts, rgb = seed
    else:
        # no range maps: a capture from before they were recorded. Noise in a
        # box around the walk, which is what produced the smeared corridors —
        # kept only so an old capture still builds something.
        logger.warning("no range maps beside the panoramas — seeding with "
                       "noise, which reconstructs a corridor poorly. "
                       "Re-run `just capture %s` to record them.",
                       panos_dir.name)
        rng = np.random.default_rng(0)
        reach = max(4.0, scale * 3.0)
        lo, hi = C.min(0) - reach, C.max(0) + reach
        pts = rng.uniform(lo, hi, size=(60000, 3)).astype(np.float32)
        rgb = np.full((len(pts), 3), 0.5, dtype=np.float32)

    have_depth = all(d is not None for d in depths)
    logger.info("training from %d recorded pose(s) over %d standpoint(s); "
                "%d seed points, scene scale %.2f m%s",
                len(viewmats), len(stand), len(pts), scale,
                "; depth supervised" if have_depth else "")
    return (torch.tensor(Ks, dtype=torch.float32, device=device),
            torch.tensor(np.stack(viewmats), device=device),
            imgs, pts, rgb, scale, depths if have_depth else None)


def write_sidecars(panos_dir: Path, world: Path, angles) -> dict:
    """world.cam.json and world.path.json, straight from the recorded walk.

    The viewer wants a spawn pose and a path to tour. Both come from the
    standpoints — no reconstruction to read them out of.
    """
    stand = load_poses(panos_dir)
    C = np.stack([np.asarray(s["xyz"], dtype=np.float64) for s in stand])

    # spawn at the most central standpoint, facing the way the walk went
    mid = int(np.argmin(np.linalg.norm(C - np.median(C, 0), axis=1)))
    axis = C[-1] - C[0]
    yaw = math.atan2(axis[1], axis[0]) if np.linalg.norm(axis[:2]) > 1e-9 else 0.0
    R, t = cam_from_world(C[mid], yaw, 0.0)
    m = np.eye(4)
    m[:3, :3] = R
    m[:3, 3] = t
    Path(world.with_suffix("").as_posix() + ".cam.json").write_text(json.dumps({
        "viewMatrix": [round(float(v), 6) for v in m.T.flatten()],
        "source": f"standpoint {mid} of {len(C)} (recorded)",
    }, indent=2))

    # the walk itself, densely sampled — it is the path, not a fit to it
    pts = []
    for a, b in zip(C, C[1:]):
        for i in range(20):
            pts.append(a + (b - a) * (i / 20))
    pts.append(C[-1])
    Path(world.with_suffix("").as_posix() + ".path.json").write_text(json.dumps({
        "points": [[round(float(v), 5) for v in p] for p in pts],
        "up": [0.0, 0.0, 1.0],
        "standpoints": len(C),
    }))
    return {"standpoints": len(C), "points": len(pts)}
