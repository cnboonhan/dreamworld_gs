"""Which panorama of a corridor to generate a world from.

HY-World is given one vantage point and imagines the rest, so this choice sets
what the world is made of. It was the middle standpoint, on the reasoning that
the ends of a lane sit against whatever the corridor opens onto. But a capture
weaves across the lane and alternates its height, so the middle standpoint can
land wedged against a wall — and then almost the whole panorama is flat plaster
at arm's length and there is nothing to build a corridor out of.

Which is measurable, because the capture wrote a metric range for every pixel.
Take the one that can see the most, in the band around the horizon where a
corridor is: over this building that lifts the median view from 1.60 m to
2.26 m, and on the two worst corridors from 0.47 m and 0.83 m to 1.33 m and
1.38 m — those two rendered as fields of coloured blobs.

Still never an endpoint. Those sit exactly on a vertex, where the openest view
is usually straight down the *next* corridor, and a world generated from there
is a world of somewhere else.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

# a corridor lives around the horizon; floor and ceiling are near and featureless
# wherever you stand, so including them would score every standpoint alike
BAND = (0.40, 0.60)


def pick(panos: Path) -> str:
    """The panorama in `panos` with the most open view."""
    names = sorted(p.name for p in panos.glob("[0-9]*.png"))
    if not names:
        raise SystemExit(f"no panoramas in {panos}")
    if len(names) < 3:
        return names[len(names) // 2]

    def view(name: str) -> float:
        r = np.load(panos / f"{Path(name).stem}.range.npy", mmap_mode="r")
        band = r[int(BAND[0] * r.shape[0]):int(BAND[1] * r.shape[0]):4]
        return float(np.median(np.asarray(band)))

    return max(names[1:-1], key=view)


if __name__ == "__main__":
    print(pick(Path(sys.argv[1])))
