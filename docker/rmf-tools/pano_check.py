"""Is a capture a faithful picture of where it stood?

Two standpoints of a corridor see mostly the same surfaces, and each one's
range map says where those surfaces are in building metres. So one panorama can
be reprojected into the other's viewpoint and compared pixel for pixel. Nothing
here involves a splat or a reconstruction: it is a property of the capture
alone, and it fails loudly when the views a panorama is composited from were
paired with the wrong pose.

That is not hypothetical. The sim's frame queue was once deeper than one, so a
view could be blended at the pose of the previous one, and sixty of those
ghosted the corridor over itself — translucent doors, the dado at two heights.
Nineteen of this building's twenty-six corridors were photographed that way and
nothing noticed for a day, because nothing looked. Corrupted captures score 7
to 16 here; faithful ones score under 2.

Pixels the other standpoint could not see are dropped, using its own range map,
so occlusion is never counted as disagreement. Two standpoints of a short
corridor can share too little to judge, which is reported rather than passed.

    python pano_check.py <panos-dir>
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

# The panoramas are 7680x3840 and the check is about geometry, not detail, so
# it runs on a small copy. Wall-clock matters: this runs inside every capture.
WIDTH = 960
# below this the two standpoints share too little of the corridor to compare
MIN_OVERLAP = 0.30
# corrupted captures scored 7 to 16, faithful ones under 2
MAX_DIFFERENCE = 4.0


def _equirect(path: Path, w: int) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB").resize((w, w // 2),
                                                             Image.BILINEAR),
                      dtype=np.float64)


def _ranges(path: Path, w: int) -> np.ndarray:
    r = np.load(path)
    step = max(1, r.shape[1] // w)
    return r[::step, ::step].astype(np.float64)


def _sample(img: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Bilinear, wrapping in longitude and clamping in latitude."""
    h, w = img.shape[:2]
    x, y = u * w - 0.5, v * h - 0.5
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    fx, fy = (x - x0)[..., None], (y - y0)[..., None]
    xa, xb = x0 % w, (x0 + 1) % w
    ya, yb = np.clip(y0, 0, h - 1), np.clip(y0 + 1, 0, h - 1)
    top = img[ya, xa] * (1 - fx) + img[ya, xb] * fx
    bot = img[yb, xa] * (1 - fx) + img[yb, xb] * fx
    return top * (1 - fy) + bot * fy


def _compare(panos: Path, xyz: dict, a: str, b: str, cache: dict) -> tuple[float, float]:
    """(overlap, difference) reprojecting standpoint a into standpoint b."""
    def load(n):
        if n not in cache:
            r = _ranges(panos / f"{Path(n).stem}.range.npy", WIDTH)
            cache[n] = (r, _equirect(panos / n, r.shape[1]))
        return cache[n]

    rb, ib = load(b)
    ra, ia = load(a)
    h, w = rb.shape
    lon = math.pi - (np.arange(w) + 0.5) / w * 2 * math.pi
    lat = math.pi / 2 - (np.arange(h) + 0.5) / h * math.pi
    lo, la = np.meshgrid(lon, lat)
    d = np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], -1)

    seen = xyz[b] + d * rb[..., None]                 # what b sees, in metres
    v = seen - xyz[a]
    r = np.linalg.norm(v, axis=-1)
    u = v / np.maximum(r, 1e-9)[..., None]
    mu = (math.pi - np.arctan2(u[..., 1], u[..., 0])) / (2 * math.pi)
    mv = (math.pi / 2 - np.arcsin(np.clip(u[..., 2], -1, 1))) / math.pi

    warped = _sample(ia, mu, mv)
    # a surface a cannot see is not a disagreement, it is an occlusion
    visible = np.abs(_sample(ra[..., None], mu, mv)[..., 0] - r) < 0.15
    ok = visible & (rb > 0.3) & (rb < 15) & (r > 0.3)
    if not ok.any():
        return 0.0, float("nan")
    return float(ok.mean()), float(np.abs(warped - ib).mean(-1)[ok].mean())


def check(panos: Path) -> dict:
    """Reproject one standpoint into another and compare.

    The widest baseline hides disagreement least, but two ends of a corridor
    that turns share almost nothing — L11.lift_lobby--v18 is corrupt and its
    end pair overlaps by 13%, too little to convict on. So candidates are tried
    from widest to narrowest and the first with enough overlap is the verdict.
    """
    stand = json.loads((panos / "poses.json").read_text())["standpoints"]
    xyz = {s["image"]: np.asarray(s["xyz"], dtype=np.float64) for s in stand}
    names = sorted(xyz)
    if len(names) < 2:
        return {"judged": False, "why": "one standpoint"}

    n = len(names)
    pairs, seen = [], set()
    for i, j in ((0, n - 1), (0, n // 2), (n // 2, n - 1), (0, 2), (0, 1)):
        if i < j < n and (i, j) not in seen:
            seen.add((i, j))
            pairs.append((names[i], names[j]))

    cache, best = {}, (0.0, None, None)
    for a, b in pairs:
        overlap, diff = _compare(panos, xyz, a, b, cache)
        if overlap >= MIN_OVERLAP:
            return {"judged": True, "corrupt": diff > MAX_DIFFERENCE,
                    "difference": round(diff, 2), "overlap": round(overlap, 3),
                    "pair": [a, b],
                    "baseline_m": round(float(np.linalg.norm(xyz[b] - xyz[a])), 2)}
        if overlap > best[0]:
            best = (overlap, a, b)
    return {"judged": False,
            "why": f"no pair shares more than {100 * best[0]:.0f}%",
            "overlap": round(best[0], 3), "pair": [best[1], best[2]]}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), indent=2))
