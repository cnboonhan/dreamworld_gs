"""What is in assets/projects, and what each project has.

    python scripts/projects.py <assets-dir> [active-project]

One line per building: how much of the map, world, panorama and splat pipeline it
has got through. The active one is starred — that is the one every recipe defaults
to, from DW_PROJECT in .env.
"""

import os
import sys
from pathlib import Path

IMAGES = (".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG")


def counts(proj: Path) -> tuple[int, int, int, int]:
    maps = len(list((proj / "maps").glob("*.building.yaml")))
    worlds = len([d for d in (proj / "worlds").glob("*") if d.is_dir()])
    panos = len([p for p in (proj / "panos").glob("*") if p.suffix in IMAGES])
    splats = len(list((proj / "splats").glob("*/world.ply")))
    return maps, worlds, panos, splats


def report(assets: Path, active: str = "") -> int:
    projects = sorted(p for p in (assets / "projects").glob("*/") if p.is_dir())
    if not projects:
        print("  none yet — run: just _env   (seeds samples/)")
        return 0
    for proj in projects:
        n = proj.name
        maps, worlds, panos, splats = counts(proj)
        print(f"  {'*' if n == active else ' '} {n:<22} {maps} map(s)  "
              f"{worlds} world(s)  {panos} panorama(s)  {splats} splat(s)")
    print("\n  * = active (just use <name> to switch)")
    return 0


if __name__ == "__main__":
    sys.exit(report(Path(sys.argv[1]),
                    sys.argv[2] if len(sys.argv) > 2
                    else os.environ.get("DW_PROJECT", "")))
