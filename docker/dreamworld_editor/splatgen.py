"""The editor's line to the splat generator — HY-World jobs over HTTP.

The editor owns the tree, so it prepares the scene: the variant's panorama
becomes <splat-dir>/panorama.png and the generator is pointed at that
directory. Both containers mount the projects tree at /projects, so the
path the editor writes is the path the generator reads.
"""

import os

import requests
from PIL import Image

import store

URL = os.environ.get("SPLATGEN_URL", "http://splatgen:8000")


def ready() -> bool:
    try:
        return (requests.get(f"{URL}/health", timeout=3)
                .json().get("status") == "ok")
    except (requests.RequestException, ValueError):
        return False


def status():
    try:
        return requests.get(f"{URL}/status", timeout=3).json()
    except (requests.RequestException, ValueError):
        return None


def submit(name: str, variant: str | None, steps: int = 2000) -> dict:
    scene = store.splat_dir(name, variant)
    scene.mkdir(parents=True, exist_ok=True)
    src = store.pano_of(name, variant)
    png = scene / "panorama.png"
    # PNG-encoding a full equirect costs seconds — pay it only when the
    # source panorama is newer than the copy the generator reads (alignment
    # rolls the pano in place, so a re-roll bumps its mtime and re-encodes)
    if not png.is_file() or png.stat().st_mtime <= src.stat().st_mtime:
        Image.open(src).convert("RGB").save(png)
    r = requests.post(f"{URL}/generate",
                      json={"scene": str(scene), "steps": steps}, timeout=30)
    if r.status_code == 409:
        raise RuntimeError("that world is already running or queued")
    r.raise_for_status()
    return r.json()


def short(scene: str) -> str:
    """/projects/<p>/dreamworld/<vertex>/<splat[@v]> -> vertex · look"""
    parts = str(scene).rstrip("/").split("/")
    if len(parts) < 2:
        return str(scene)
    look = parts[-1].split("@", 1)
    return f"{parts[-2]} · {look[1] if len(look) > 1 else 'original'}"
