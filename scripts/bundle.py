"""Carry a project to another machine, and unpack it there.

    python scripts/bundle.py pack   <assets-dir> <project> [dest-dir]
    python scripts/bundle.py unpack <repo-dir> <tarball>

What travels is the map, the Gazebo world, the panoramas, and each splat's
deliverables — world.ply, world.usdz, world.cam.json, world.paths.json and the
panorama it was generated from.

What does not is everything HY-World produced on the way to those (gs_data,
render_results, navmesh, gs_result), nor anything else sitting in the project
directory. This is a LIST of what to carry rather than a set of exclusions, and
that distinction is the whole point: excluding the intermediates by pattern still
swept up backup copies of panos/ and splats/ and a traversals/ from a pipeline
that no longer existed, and made a 3.4 GB archive of a 1.2 GB project. Anything
unlisted stays behind, including next month's stray folder.

Model weights are not included either — hundreds of GB, and `just setup` fetches
them on the far side.
"""

import subprocess
import sys
import tarfile
import time
from pathlib import Path

# One splat's worth of deliverables. Everything else under splats/<id>/ is input
# to a training run that has already happened.
KEEP = ("world.ply", "world.usdz", "world.cam.json", "world.paths.json",
        "panorama.png")


def carry(root: Path, project: str) -> list[Path]:
    """Every file that travels, relative to the repo root."""
    proj = root / "assets" / "projects" / project
    files = []
    for drawer in ("maps", "worlds", "panos"):
        for p in (proj / drawer).rglob("*"):
            # .previews is a downscale cache the editor rebuilds on demand
            if p.is_file() and ".previews" not in p.parts:
                files.append(p)
    for splat in sorted((proj / "splats").glob("*/")):
        files += [splat / name for name in KEEP if (splat / name).is_file()]
    return sorted(set(files))


def pack(assets: Path, project: str, dest: Path) -> int:
    root = assets.parent
    if not (assets / "projects" / project).is_dir():
        print(f"no such project: {project}", file=sys.stderr)
        return 1
    files = carry(root, project)
    if not files:
        print(f"{project} has nothing to bundle yet", file=sys.stderr)
        return 1
    dest.mkdir(parents=True, exist_ok=True)
    out = dest.resolve() / f"{project}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for f in files:
            tar.add(f, arcname=str(f.relative_to(root)))
    print(f"bundled {project} -> {out}")
    print(f"  {len(files)} file(s), {out.stat().st_size / 1e9:.2f} GB")
    print("  (training intermediates left behind — 'just generate' rebuilds them)")
    print(f"  restore with: just unbundle {out}")
    return 0


def unpack(repo: Path, tarball: Path) -> int:
    f = tarball if tarball.is_file() else repo / tarball
    if not f.is_file():
        print(f"no such bundle: {tarball}", file=sys.stderr)
        return 1
    with tarfile.open(f) as tar:
        names = tar.getnames()
        # Say what it will land on before it lands on it.
        landing = sorted({n.split("/")[2] for n in names
                          if n.startswith("assets/projects/") and n.count("/") > 2})
        for p in landing:
            if (repo / "assets" / "projects" / p).is_dir():
                print(f"note: assets/projects/{p} exists and will be merged into")
        tar.extractall(repo, filter="data")
    print(f"unbundled into {repo}/assets/projects: {', '.join(landing)}")
    subprocess.run([sys.executable, str(Path(__file__).with_name("projects.py")),
                    str(repo / "assets")], check=False)
    return 0


def main() -> int:
    mode = sys.argv[1]
    if mode == "pack":
        dest = Path(sys.argv[4]) if len(sys.argv) > 4 else Path("dist")
        return pack(Path(sys.argv[2]), sys.argv[3], dest)
    if mode == "unpack":
        return unpack(Path(sys.argv[2]), Path(sys.argv[3]))
    print(f"unknown mode {mode!r} — want pack or unpack", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
