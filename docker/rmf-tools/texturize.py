#!/usr/bin/env python3
"""Give the generated world's surfaces something *unrepeated* to look at.

Two problems, and the second is the one that bites.

**Flat paint.** The map generator bakes a near-uniform texture onto the walls —
`default.png` is 8 KB for a 597x1024 image. Structure from motion needs corners
to match, and a blank wall has none: panoramas of the untextured world
registered 2 of 60 views.

**Repetition.** Adding detail is not enough, because the meshes tile their
texture: the wall's UVs run 0..24.25, so a 512-pixel image repeats about 24
times along it, and the floor's repeat ~84 x 26. Every repeat is then not only
detailed but *identically* detailed, which is worse than blank — a feature
detector matches a corner to the wrong copy and folds the reconstruction. That
is what happened: 55 of 60 views registered while the recovered walk collapsed
from 2.2 m to 0.24 m.

So this does two things per mesh:

  - rescales the UVs to span 0..1, so the texture covers the surface once
  - paints one surface-sized texture that never repeats

The pattern is quasiperiodic. Cell edges come from two incommensurate spacings
whose ratio is irrational, so the tiling has no translational symmetry at any
offset — the same reason an aperiodic tiling (Penrose, or the "hat" monotile)
never repeats. Aperiodic *geometry* alone would not be enough here, because a
feature descriptor reads a small patch and those recur locally even when the
global arrangement does not; so every cell is also shaded by a hash of its
index. Aperiodic layout removes exact repeats, per-cell shading removes local
ambiguity.

None of this is a thumb on the scale. A real corridor has doors, signage, wear
and skirting, and does not look the same every 1.2 metres. The uniformity and
the tiling are both the simulator's artifacts.

Usage: texturize.py <models-dir>
"""

import hashlib
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# how strongly the pattern shows through, as a fraction of the base colour
STRENGTH = 0.30
# longest side of a generated texture; the other follows the surface's aspect
MAX_PX = 4096
# pixels per cell, roughly — small enough for several cells per camera view
CELL_PX = 26
# golden ratio: the least well approximated by rationals, so two spacings in
# this ratio come closest to never lining up
PHI = (1 + 5 ** 0.5) / 2


def rescale_uvs(obj: Path) -> tuple[float, float] | None:
    """Map this mesh's UVs onto 0..1, and report the extent they covered.

    The extent is the aspect ratio the surface wants: a wall 24 tiles long and
    1 tall should get a long thin texture, not a square one stretched."""
    lines = obj.read_text().splitlines(keepends=True)
    uv = [(i, [float(x) for x in l.split()[1:3]])
          for i, l in enumerate(lines) if l.startswith("vt ")]
    if not uv:
        return None
    us = [c[0] for _, c in uv]
    vs = [c[1] for _, c in uv]
    du, dv = max(us) - min(us), max(vs) - min(vs)
    if du < 1.01 and dv < 1.01:
        return du or 1.0, dv or 1.0        # already covers the surface once
    u0, v0 = min(us), min(vs)
    for i, (u, v) in uv:
        lines[i] = f"vt {(u - u0) / (du or 1):.6f} {(v - v0) / (dv or 1):.6f}\n"
    obj.write_text("".join(lines))
    return du or 1.0, dv or 1.0


def quasiperiodic(w: int, h: int, seed: int) -> np.ndarray:
    """A (h, w) field in [-1, 1] of cells that never repeat.

    Two incommensurate spacings per axis: their ratio is irrational, so the
    pair of indices a pixel falls into recurs at no finite offset."""
    rng = np.random.default_rng(seed)
    x = np.arange(w, dtype=np.float64)
    y = np.arange(h, dtype=np.float64)
    # index pairs, one per axis
    ia = np.floor(x / CELL_PX).astype(np.int64)
    ib = np.floor(x / (CELL_PX * PHI)).astype(np.int64)
    ja = np.floor(y / CELL_PX).astype(np.int64)
    jb = np.floor(y / (CELL_PX * PHI)).astype(np.int64)

    # hash the four indices into a value per cell — cheap, and stable so a
    # rebuild reproduces the same wall
    def mix(*idx):
        v = np.uint64(seed | 1)
        for a in idx:
            v = (v ^ a.astype(np.uint64)) * np.uint64(0x9E3779B97F4A7C15)
            v ^= v >> np.uint64(29)
        return v

    cell = mix(ia[None, :] * np.uint64(1), ib[None, :] * np.uint64(7),
               ja[:, None] * np.uint64(31), jb[:, None] * np.uint64(97))
    field = ((cell >> np.uint64(11)).astype(np.float64)
             / float(1 << 53) * 2.0 - 1.0)

    # a little fine grain on top, so a descriptor has structure inside a cell
    grain = rng.random((max(2, h // 3), max(2, w // 3))) - 0.5
    gy = (np.arange(h) * grain.shape[0] // h).clip(0, grain.shape[0] - 1)
    gx = (np.arange(w) * grain.shape[1] // w).clip(0, grain.shape[1] - 1)
    return np.clip(field + 0.5 * grain[gy][:, gx], -1, 1)


def texturize(png: Path, extent: tuple[float, float]) -> str:
    base = np.asarray(Image.open(png).convert("RGB")).astype(np.float32)
    colour = base.reshape(-1, 3).mean(0)          # keep the surface's colour
    du, dv = extent
    # size to the surface, not the old image: a long wall gets a long texture
    if du >= dv:
        w = int(min(MAX_PX, max(256, 512 * du)))
        h = int(max(64, round(w * dv / du)))
    else:
        h = int(min(MAX_PX, max(256, 512 * dv)))
        w = int(max(64, round(h * du / dv)))

    seed = int(hashlib.sha256(png.name.encode()).hexdigest()[:8], 16)
    field = quasiperiodic(w, h, seed)[..., None]
    out = np.clip(colour[None, None] * (1.0 + STRENGTH * field), 0, 255)
    out = out.astype(np.uint8)
    Image.fromarray(out).save(png)
    return (f"{png.name}: {w}x{h} over a {du:.1f}x{dv:.1f} surface, "
            f"contrast {base.std():.1f} -> {out.std():.1f}")


def main() -> None:
    models = Path(sys.argv[1])
    print("texturizing so SfM has features that do not repeat:")
    done = 0
    for obj in sorted(models.rglob("*.obj")):
        extent = rescale_uvs(obj)
        if extent is None:
            continue
        # the .mtl beside it names the texture this mesh uses
        mtl = obj.with_suffix(".mtl")
        names = [l.split(None, 1)[1].strip()
                 for l in mtl.read_text().splitlines()
                 if l.strip().startswith("map_Kd")] if mtl.is_file() else []
        for name in names:
            png = obj.parent / name
            if png.is_file():
                print("  " + texturize(png, extent))
                done += 1
    if not done:
        print("  no textured meshes found")


if __name__ == "__main__":
    main()
