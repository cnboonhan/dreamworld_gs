#!/usr/bin/env python3
"""Give the generated world's surfaces something to look at.

The map generator bakes a near-uniform texture onto the walls — `default.png`
is 8 KB for a 597x1024 image, i.e. flat paint. That is fine for a simulation
you drive a robot through, and fatal for one you photograph: structure from
motion needs corners to match, and a blank wall has none. Measured on the
sample building, panoramas of the untextured world registered **2 of 60 views**.

So this overlays fine detail on every mesh texture, keeping the original colour
— the floor stays blue, the walls stay pale — while adding the grain a real
painted wall has. It is not a trick to help the reconstructor: a perfectly
uniform surface is the simulator's artifact, and a real room has scuffs, grain
and skirting. This makes the sim less wrong, and SfM works as a consequence.

The pattern is deterministic (seeded per file) and non-repeating within the
image, because a tiling pattern gives feature matching the wrong answer rather
than no answer.

Usage: texturize.py <models-dir>
"""

import hashlib
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# how strongly the detail shows through, as a fraction of the base colour.
# Enough to matter to a feature detector, little enough that the world still
# looks like the map someone drew.
STRENGTH = 0.22


def detail(w: int, h: int, seed: int) -> np.ndarray:
    """A (h, w) field in [-1, 1]: several octaves of value noise plus a few
    long edges, which is roughly what a wall gives a corner detector."""
    rng = np.random.default_rng(seed)
    out = np.zeros((h, w), np.float32)
    amp = 1.0
    for octave in range(4):
        gh = max(2, h >> (5 - octave))
        gw = max(2, w >> (5 - octave))
        coarse = rng.random((gh, gw), dtype=np.float32)
        # nearest-neighbour upsample keeps hard edges, which detect better than
        # a smooth gradient
        ys = (np.arange(h) * gh // h).clip(0, gh - 1)
        xs = (np.arange(w) * gw // w).clip(0, gw - 1)
        out += amp * (coarse[ys][:, xs] - 0.5)
        amp *= 0.55

    # a handful of long straight scuffs: strong, sparse, oriented features
    for _ in range(6):
        y = rng.integers(0, h)
        thick = int(rng.integers(1, max(2, h // 120)))
        out[y:y + thick, :] += rng.uniform(-0.4, 0.4)
    for _ in range(6):
        x = rng.integers(0, w)
        thick = int(rng.integers(1, max(2, w // 120)))
        out[:, x:x + thick] += rng.uniform(-0.4, 0.4)

    peak = float(np.abs(out).max()) or 1.0
    return out / peak


def texturize(path: Path) -> str:
    img = Image.open(path).convert("RGB")
    arr = np.asarray(img).astype(np.float32)
    h, w = arr.shape[:2]
    # seeded by name, so a rebuild reproduces the same world
    seed = int(hashlib.sha256(path.name.encode()).hexdigest()[:8], 16)
    d = detail(w, h, seed)[..., None]
    # modulate rather than add, so dark surfaces stay dark and the hue holds
    out = np.clip(arr * (1.0 + STRENGTH * d), 0, 255).astype(np.uint8)
    Image.fromarray(out).save(path)
    before = float(np.asarray(img).astype(np.float32).std())
    return f"{path.name} ({w}x{h}) contrast {before:.1f} -> {float(out.std()):.1f}"


def main() -> None:
    models = Path(sys.argv[1])
    pngs = sorted(models.rglob("*.png"))
    if not pngs:
        print("  no mesh textures to texturize")
        return
    print(f"texturizing {len(pngs)} mesh texture(s) so SfM has features:")
    for p in pngs:
        print("  " + texturize(p))


if __name__ == "__main__":
    main()
