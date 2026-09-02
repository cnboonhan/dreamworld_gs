"""Download every model the pipeline needs into assets/.

Idempotent, and cheaply so: a repo already in the cache is recognised without a
network call, so running this again on a complete box takes about three seconds
rather than revalidating half a terabyte against the hub. Interrupted downloads
still resume — a partial cache is treated as absent, not present.

Usage:
    python scripts/fetch_assets.py <assets-dir>
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def already_here(repo: str):
    """The cached snapshot of `repo`, or None if it needs downloading.

    A local-only resolve answers this in microseconds and without touching the
    network, where a plain snapshot_download revalidates every file against the
    hub — which on a 54 GB repo is a long way to go to be told nothing changed.
    On a box with no route to huggingface.co it is also the difference between
    starting and hanging.

    An interrupted download leaves .incomplete blobs behind and a snapshot that
    resolves but is short of files, so both are checked: a partial cache must
    look like a miss, not a hit.
    """
    from huggingface_hub import snapshot_download

    try:
        path = Path(snapshot_download(repo, local_files_only=True))
    except Exception:                       # noqa: BLE001 — any failure means fetch it
        return None
    if list(path.parent.parent.glob("blobs/*.incomplete")):
        return None
    if any(f.is_symlink() and not f.exists() for f in path.rglob("*")):
        return None
    return path


def fetch_hf(assets: Path) -> None:
    from huggingface_hub import snapshot_download

    repos = [
        line.strip()
        for line in (HERE / "models.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for repo in repos:
        here = already_here(repo)
        if here is not None:
            size = sum(f.stat().st_size for f in here.rglob("*") if f.is_file())
            print(f"    {repo:44} have it ({size / 1e9:.1f} GB)", flush=True)
            continue
        print(f"==> {repo}", flush=True)
        snapshot_download(repo)


def main() -> None:
    assets = Path(sys.argv[1]).resolve()
    (assets / "models").mkdir(parents=True, exist_ok=True)
    fetch_hf(assets)
    total = subprocess.run(["du", "-sh", str(assets)], capture_output=True,
                           text=True).stdout.split()[0]
    print(f"assets ready: {total}")


if __name__ == "__main__":
    main()
