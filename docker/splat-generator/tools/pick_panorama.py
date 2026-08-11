"""Which panorama of a corridor to generate a world from.

HY-World is given one vantage point and imagines the rest, so this choice sets
what the world is made of. It was the middle standpoint, on the reasoning that
the ends of a lane sit against whatever the corridor opens onto. But a capture
weaves across the lane and alternates its height, so which standpoint the
middle happens to be is a lottery, and it can land facing a wall a metre away
with nothing else in frame.

Which is measurable, because the capture wrote a metric range for every pixel.
Take the one that can see the most, in the band around the horizon where a
corridor is: over this building that lifts the median view from 1.57 m to
2.01 m, and on the corridors it matters for — L11.v0--v11, L11.v11--v15,
L11.v10--v9 — from 1.15, 0.96 and 0.53 m to 3.29, 2.84 and 1.74 m.

Measured on captures that pass pano_check. An earlier version of this file
quoted 1.60 -> 2.26 m, from panoramas composited out of stale frames: a camera
seeing through its own walls looks cramped in some directions and far-seeing in
others, which inflated both ends of the comparison.

Still never an endpoint. Those sit exactly on a vertex, where the openest view
is usually straight down the *next* corridor, and a world generated from there
is a world of somewhere else.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

# a corridor lives around the horizon; floor and ceiling are near and featureless
# wherever you stand, so including them would score every standpoint alike
BAND = (0.40, 0.60)


def standpoints(panos: Path) -> list[str]:
    """The panoramas this capture wrote, in walk order.

    From poses.json when a simulated capture left one, because the folder can
    hold more than the capture put there — a corridor re-photographed at a
    wider spacing used to leave the tail of the previous walk behind, and those
    files are a different capture under numbers this one never wrote.
    """
    record = panos / "poses.json"
    if record.is_file():
        return sorted(s["image"] for s in
                      json.loads(record.read_text())["standpoints"])
    # a real capture arrives as whatever the camera wrote — .JPG, usually
    return sorted(p.name for p in panos.iterdir()
                  if p.suffix.lower() in (".png", ".jpg", ".jpeg"))


def pick(panos: Path) -> str:
    """The panorama in `panos` with the most open view.

    A real capture has no range maps — a 360 camera measures nothing — so
    openness cannot be scored and the middle of the walk is taken instead.
    That is the rule this replaced, and it is the right fallback: it is where
    a corridor is most likely to be seen along rather than across.
    """
    names = standpoints(panos)
    if not names:
        raise SystemExit(f"no panoramas in {panos}")
    ranges = {n: panos / f"{Path(n).stem}.range.npy" for n in names}
    if len(names) < 3 or not all(p.is_file() for p in ranges.values()):
        return names[len(names) // 2]

    def view(name: str) -> float:
        r = np.load(ranges[name], mmap_mode="r")
        band = r[int(BAND[0] * r.shape[0]):int(BAND[1] * r.shape[0]):4]
        return float(np.median(np.asarray(band)))

    return max(names[1:-1], key=view)


if __name__ == "__main__":
    print(pick(Path(sys.argv[1])))
