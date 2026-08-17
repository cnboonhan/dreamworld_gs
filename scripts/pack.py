"""Pack a whole project to carry to another machine, and unpack it there.

    python scripts/pack.py pack   <assets-dir> <project> [dest-dir]
    python scripts/pack.py unpack <repo-dir> <tarball>

Everything under assets/projects/<project> travels — maps, worlds, panos,
splats, training intermediates, all of it. No exclusions: this is the whole
drawer, for when the far side should hold byte-for-byte what this side does.
Model weights are separate; `just fetch` provides them on the far side.

Paths are stored as assets/projects/<name>/..., so an unpack lands exactly
where the stack looks.
"""

import sys
import tarfile
import time
from pathlib import Path


def pack(assets: Path, project: str, dest: Path) -> int:
    root = assets.parent
    proj = assets / "projects" / project
    if not proj.is_dir():
        print(f"no such project: {project}", file=sys.stderr)
        return 1
    files = sorted(p for p in proj.rglob("*") if p.is_file())
    if not files:
        print(f"{project} is empty — nothing to pack", file=sys.stderr)
        return 1
    # Say what travels, by drawer, BEFORE the slow part: gzipping tens of GB
    # takes minutes, and the moment to notice a surprise is now.
    sizes: dict[str, int] = {}
    for f in files:
        top = f.relative_to(proj).parts[0]
        sizes[top] = sizes.get(top, 0) + f.stat().st_size
    total = sum(sizes.values())
    print(f"packing {project}: {len(files)} file(s), {total / 1e9:.2f} GB "
          f"— everything, intermediates included")
    for k in sorted(sizes, key=lambda k: -sizes[k]):
        print(f"  {sizes[k] / 1e6:10.1f} MB  {k}")
    dest.mkdir(parents=True, exist_ok=True)
    out = dest.resolve() / f"{project}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for f in files:
            tar.add(f, arcname=str(f.relative_to(root)))
    print(f"packed {project} -> {out}")
    print(f"  {out.stat().st_size / 1e9:.2f} GB on disk")
    print(f"  restore with: just unpack {out}")
    return 0


def unpack(repo: Path, tarball: Path) -> int:
    f = tarball if tarball.is_file() else repo / tarball
    if not f.is_file():
        print(f"no such archive: {tarball}", file=sys.stderr)
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
    print(f"unpacked into {repo}/assets/projects: {', '.join(landing)}")
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
    main()
