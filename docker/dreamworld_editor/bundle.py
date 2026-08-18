"""Bundle the walkable dreamworld into a static tree — the demo's half.

The viewer fetches exactly three things: the graph document, each world's
32-byte records (world.splat) and each crossing's video. This copies those
three and NOTHING else into <project>/bundle; the panoramas never leave
the dreamworld tree, because the walkthrough does not use them.

    docker compose run --rm --no-deps dreamworld_editor \
        python bundle.py /projects/<project>/bundle

compose.demo.yaml then serves the bundle at the same paths the full stack
serves the live tree, so viewer.js never knows the difference.
"""
import json
import shutil
import sys
from pathlib import Path

import store
from config import DREAM


def main(dest):
    dest = Path(dest)
    files = dest / "files"
    if dest.exists():
        shutil.rmtree(dest)
    files.mkdir(parents=True)
    doc = store.graph_doc()          # builds any missing world.splat too
    (dest / "graph.json").write_text(json.dumps(doc))
    worlds = 0
    for name, v in (doc.get("vertices") or {}).items():
        for lk, info in (v.get("looks") or {}).items():
            if not info.get("records"):
                continue
            src = DREAM / name / info["dir"] / info["records"]
            out = files / name / info["dir"] / info["records"]
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
            worlds += 1
    crossings = 0
    for key in doc.get("crossings") or []:
        src = DREAM / ".crossings" / key / "crossing.mp4"
        if not src.is_file():
            continue
        out = files / ".crossings" / key / "crossing.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        crossings += 1
    size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    print(f"bundle: {worlds} worlds, {crossings} crossings, "
          f"{size / 1e6:.0f} MB -> {dest}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/projects/bundle")
