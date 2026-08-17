"""The editor's line to wangen — edge-crossing videos.

A crossing is one direction of one edge in one pair of looks: from
<vertex>[@look] to <vertex>[@look], filed under dreamworld/.crossings/.
The editor prepares everything the model needs — both conditioning frames
extracted from the ALIGNED panoramas along the edge's own bearing (the
prototype's recipe, verbatim), and the prompt, saved beside them — then
submits the directory. The default prompt is read off the map: a door
edge opens a door, a lift edge rides a lift, an open edge just walks.
"""

import math
import os

import requests
from PIL import Image

import store
from config import DREAM
from restyle import _extract_perspective

URL = os.environ.get("WANGEN_URL", "http://wangen:8000")
CROSS = DREAM / ".crossings"

PROMPTS = {
    "door": ("First-person walkthrough. The closed door directly ahead "
             "swings open, and the camera walks forward through the open "
             "doorway into the room beyond, ending inside. Smooth steady "
             "forward camera motion, photorealistic indoor office "
             "lighting, consistent geometry."),
    "lift_in": ("First-person walkthrough. The lift doors directly ahead "
                "slide open, and the camera steps forward into the lift "
                "cabin, ending inside the lift. Smooth steady forward "
                "camera motion, photorealistic indoor lighting."),
    "lift_out": ("First-person walkthrough. The lift doors slide open and "
                 "the camera steps forward out of the lift into the space "
                 "beyond, ending outside the lift. Smooth steady forward "
                 "camera motion, photorealistic indoor lighting."),
    "open": ("First-person walkthrough. The camera moves smoothly forward "
             "along the open corridor toward the destination, steady "
             "motion, photorealistic indoor office lighting, consistent "
             "geometry."),
}


def _tag(name: str, look) -> str:
    return f"{name}@{look}" if look else name


def job_dir(frm, la, to, lb):
    return CROSS / f"{_tag(frm, la)}__{_tag(to, lb)}"


def output(frm, la, to, lb):
    p = job_dir(frm, la, to, lb) / "crossing.mp4"
    return p if p.is_file() else None


def saved_prompt(frm, la, to, lb):
    p = job_dir(frm, la, to, lb) / "prompt.txt"
    return p.read_text().strip() if p.is_file() else None


def default_prompt(frm, to):
    return PROMPTS[store.edge_kind(frm, to)]


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


def submit(frm, la, to, lb, prompt: str) -> dict:
    """Extract both conditioning frames along the edge bearing, save the
    prompt beside them, and queue the crossing."""
    dream = store.load_dream()
    a, b = dream[frm], dream[to]
    bearing = math.atan2(-(b["y"] - a["y"]), b["x"] - a["x"])
    d = job_dir(frm, la, to, lb)
    d.mkdir(parents=True, exist_ok=True)
    for nm, look, tag in ((frm, la, "first"), (to, lb, "last")):
        pano = Image.open(store.pano_of(nm, look))
        _extract_perspective(pano, bearing, 0.0, 1.2, 832, 480).save(
            d / f"{tag}.png")
    (d / "prompt.txt").write_text(prompt.strip() + "\n")
    (d / "crossing.mp4").unlink(missing_ok=True)
    r = requests.post(f"{URL}/generate",
                      json={"dir": str(d), "prompt": prompt}, timeout=30)
    if r.status_code == 409:
        raise RuntimeError("that crossing is already running or queued")
    r.raise_for_status()
    return r.json()


def short(scene: str) -> str:
    return str(scene).rstrip("/").rsplit("/", 1)[-1].replace("__", " → ")
