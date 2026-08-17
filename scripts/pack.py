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


class Bar:
    """A progress bar over bytes, smooth inside big files: gzipping tens of
    GB is minutes of otherwise-silent work."""

    def __init__(self, total: int, label: str):
        self.total = max(1, total)
        self.done = 0
        self.label = label
        self.t0 = time.time()
        self._last = 0.0

    def add(self, n: int) -> None:
        self.done += n
        now = time.time()
        if now - self._last < 0.1 and self.done < self.total:
            return
        self._last = now
        frac = min(1.0, self.done / self.total)
        bar = "#" * int(frac * 30)
        rate = self.done / max(1e-9, now - self.t0)
        eta = int((self.total - self.done) / max(1.0, rate))
        sys.stderr.write(
            f"\r{self.label} [{bar:<30}] {frac * 100:5.1f}%  "
            f"{self.done / 1e9:6.2f}/{self.total / 1e9:.2f} GB  "
            f"{rate / 1e6:4.0f} MB/s  eta {eta // 60}:{eta % 60:02d}  ")
        sys.stderr.flush()

    def close(self) -> None:
        self.add(0)
        sys.stderr.write("\n")


class Counted:
    """A file object that reports every byte read to the bar."""

    def __init__(self, fh, bar: Bar):
        self.fh = fh
        self.bar = bar

    def read(self, n: int = -1):
        b = self.fh.read(n)
        self.bar.add(len(b))
        return b

    def close(self):
        self.fh.close()


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
    # Say what travels, by drawer, BEFORE the archive is written: the moment
    # to notice a surprise is before the copy, not after.
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
    out = dest.resolve() / f"{project}-{time.strftime('%Y%m%d-%H%M%S')}.tar"
    bar = Bar(total, "packing  ")
    # Plain tar, no compression: panoramas, plys and videos are already
    # compressed formats, so gzip bought a few percent at a tenth of the
    # speed — the archive now writes at disk rate.
    with tarfile.open(out, "w:") as tar:
        for f in files:
            info = tar.gettarinfo(f, arcname=str(f.relative_to(root)))
            with open(f, "rb") as fh:
                tar.addfile(info, Counted(fh, bar))
    bar.close()
    print(f"packed {project} -> {out}")
    print(f"  {out.stat().st_size / 1e9:.2f} GB on disk")
    print(f"  restore with: just unpack {out}")
    return 0


def unpack(repo: Path, tarball: Path) -> int:
    f = tarball if tarball.is_file() else repo / tarball
    if not f.is_file():
        print(f"no such archive: {tarball}", file=sys.stderr)
        return 1
    # Say what it will land on before it lands on it — from the first member,
    # so the whole archive is not decompressed twice just to learn one name.
    with tarfile.open(f) as tar:
        first = tar.next()
    landing = (first.name.split("/")[2]
               if first and first.name.startswith("assets/projects/")
               and first.name.count("/") >= 2 else None)
    if landing and (repo / "assets" / "projects" / landing).is_dir():
        print(f"note: assets/projects/{landing} exists and will be merged into")
    # Progress over the ARCHIVE's bytes in stream mode: one pass, smooth,
    # and the total is simply the file's size on disk. r|* keeps old
    # compressed archives unpackable beside the new plain ones.
    bar = Bar(f.stat().st_size, "unpacking")
    with open(f, "rb") as raw:
        with tarfile.open(fileobj=Counted(raw, bar), mode="r|*") as tar:
            tar.extractall(repo, filter="data")
    bar.close()
    print(f"unpacked into {repo}/assets/projects" +
          (f": {landing}" if landing else ""))
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
