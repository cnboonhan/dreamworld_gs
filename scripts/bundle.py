"""Carry a project to another machine, and unpack it there.

    python scripts/bundle.py pack   <assets-dir> <project> [dest-dir]
    python scripts/bundle.py unpack <repo-dir> <tarball>

Everything in the project directory travels, except what a finished training
run can rebuild and caches that rebuild themselves. The exclusions are FEW and
NAMED — gs_data, render_results, navmesh, gs_result (HY-World's intermediates:
40 of the sample project's 41 GB), plus the editor's .previews and .candidates
caches. Everything else goes by default, and that default is the point: this
used to be a list of what to carry, and the list left the Galaxea R1 meshes,
splats/scenes.json and the alignment marks behind — discovered when a robot
spawned meshless on the receiving machine. A new drawer someone adds next
month travels without anyone remembering to bless it here.

The old fear about exclusion lists — junk swept in silently, a 3.4 GB archive
of a 1.2 GB project — is answered by the per-drawer size report printed at
pack time: a bloated archive announces itself before it is copied anywhere.

Model weights are not included either — hundreds of GB, and `just setup`
fetches them on the far side.
"""

import subprocess
import sys
import tarfile
import time
from pathlib import Path

# Directory names excluded at any depth: a training run's inputs and outputs
# that `just generate` rebuilds, and caches their tools rebuild on demand.
SKIP_DIRS = {"gs_data", "render_results", "navmesh", "gs_result",
             ".previews", ".candidates"}


def carry(root: Path, project: str) -> list[Path]:
    """Every file that travels, relative to the repo root."""
    proj = root / "assets" / "projects" / project
    return sorted(p for p in proj.rglob("*")
                  if p.is_file()
                  and not (SKIP_DIRS & set(p.relative_to(proj).parts[:-1])))


def pack(assets: Path, project: str, dest: Path) -> int:
    root = assets.parent
    if not (assets / "projects" / project).is_dir():
        print(f"no such project: {project}", file=sys.stderr)
        return 1
    files = carry(root, project)
    if not files:
        print(f"{project} has nothing to bundle yet", file=sys.stderr)
        return 1
    # Say what travels, by drawer, BEFORE the slow part — an archive that is
    # about to be ten times the project's deliverables should be caught here,
    # not discovered on the far side of a copy.
    proj = assets / "projects" / project
    sizes: dict[str, int] = {}
    for f in files:
        top = f.relative_to(proj).parts[0]
        sizes[top] = sizes.get(top, 0) + f.stat().st_size
    total = sum(sizes.values())
    print(f"bundling {project}: {len(files)} file(s), {total / 1e9:.2f} GB")
    for k in sorted(sizes, key=lambda k: -sizes[k]):
        print(f"  {sizes[k] / 1e6:10.1f} MB  {k}")
    dest.mkdir(parents=True, exist_ok=True)
    out = dest.resolve() / f"{project}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for f in files:
            tar.add(f, arcname=str(f.relative_to(root)))
    print(f"bundled {project} -> {out}")
    print(f"  {out.stat().st_size / 1e9:.2f} GB on disk")
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
