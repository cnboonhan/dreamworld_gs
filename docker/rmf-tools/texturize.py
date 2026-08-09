#!/usr/bin/env python3
"""Give the generated world's surfaces a realistic finish.

The map generator paints each surface a near-flat colour. That is a problem
for gaussian splatting, and not the one it first looks like: a flat wall gives
the photometric loss almost nothing to say, so a haze that averages to the
right grey costs nothing to keep, and gaussians drift off the surface they
belong to. Removing the earlier texture entirely put 47% of a corridor's
gaussians into space the capture had seen straight through.

An earlier version of this file solved a different problem — structure from
motion needs corners to match, and untextured panoramas registered 2 of 60
views — by replacing every surface with a high-contrast quasiperiodic mosaic.
It worked for matching and was wrong for everything else: no corridor is a
chequerboard, and it dominated every render of the result. A simulated capture
no longer runs structure from motion at all.

So this adds finish rather than pattern:

  - the authored image is kept, colours and all, not flattened to its mean
  - fractal grain is laid over it, several octaves, a few percent deep
  - the UVs are left alone, so the grain lands at the scale the map intended

Painted plaster, linoleum and carpet all vary by a few percent over a
centimetre or two, and that is what this is. It gives the loss something to
hold on to at the resolution the capture actually resolves — about 10 pixels
per degree at the training views, so roughly a centimetre on a wall an arm's
length away — without pretending the building has a pattern on it.

Usage: texturize.py <models-dir>
"""

import hashlib
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# How deep the grain runs, as a fraction of each pixel's own brightness. Paint
# and vinyl vary by a few percent; much more than this reads as dirt.
STRENGTH = 0.14
# Finest octave, in pixels of the source texture. The map tiles these across a
# surface, so a 512 px image over a half-metre tile makes 8 px about a
# centimetre — near the finest thing the capture resolves.
FINEST_PX = 8
OCTAVES = 5


def grain(w: int, h: int, seed: int) -> np.ndarray:
    """Fractal value noise in [-1, 1]: several octaves, each half the last.

    One octave alone is either too fine to survive the capture or too coarse
    to look like a surface. Summing them gives the self-similar roughness real
    finishes have, which is also what keeps a gradient available at whatever
    distance the camera happens to be.
    """
    rng = np.random.default_rng(seed)
    out = np.zeros((h, w), np.float32)
    amp, total = 1.0, 0.0
    for o in range(OCTAVES):
        step = FINEST_PX << o
        gh, gw = max(2, h // step + 2), max(2, w // step + 2)
        cell = rng.random((gh, gw)).astype(np.float32) - 0.5
        # bilinear upsample: the smooth interpolation is what makes it read as
        # a surface rather than as noise
        layer = np.asarray(Image.fromarray(cell, mode="F").resize((w, h),
                                                                  Image.BILINEAR))
        out += amp * layer
        total += amp
        amp *= 0.55
    out /= max(total, 1e-6)
    peak = np.abs(out).max()
    return out / peak if peak > 1e-6 else out


def texturize(png: Path) -> str:
    img = Image.open(png).convert("RGB")
    base = np.asarray(img).astype(np.float32)
    h, w = base.shape[:2]
    seed = int(hashlib.sha256(png.name.encode()).hexdigest()[:8], 16)
    field = grain(w, h, seed)[..., None]
    # multiplicative, so a dark skirting stays dark and a white wall stays
    # white — the finish varies the surface rather than repainting it
    out = np.clip(base * (1.0 + STRENGTH * field), 0, 255).astype(np.uint8)
    Image.fromarray(out).save(png)
    return (f"{png.name}: {w}x{h}, contrast {base.std():.1f} -> {out.std():.1f}, "
            f"grain from {FINEST_PX} px over {OCTAVES} octaves")


def main() -> None:
    models = Path(sys.argv[1])
    print("adding a surface finish, so flat paint still gives the loss a gradient:")
    done = 0
    for png in sorted(models.rglob("*.png")):
        print("  " + texturize(png))
        done += 1
    if not done:
        print("  no textures found")


if __name__ == "__main__":
    main()
