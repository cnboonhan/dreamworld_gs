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

# one queue per generator instance; the client spreads the load. The
# comma-separated WANGEN_URLS wins over the single-instance WANGEN_URL.
URLS = [u.strip() for u in os.environ.get(
    "WANGEN_URLS", os.environ.get("WANGEN_URL", "http://wangen:8000")
    ).split(",") if u.strip()]
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
    # a template, not a prompt: default_prompt() fills {motion} with the
    # ride's real direction, read from the two levels' elevations. Both
    # conditioning frames PIN the doors open (the panoramas were shot from
    # the open cabin), so the close-ride-reopen is a strict three-act
    # story the prompt spells out in order — and NEG_LIFT below bans the
    # lazy alternative the model reaches for, riding with the doorway
    # open while the shaft slides past.
    "lift_ride": ("First-person view from inside a lift cabin, the camera "
                  "locked facing the open doorway. First the two "
                  "brushed-metal door panels slide in from the sides and "
                  "close completely, sealing the cabin behind a solid "
                  "metallic wall. The doors stay fully shut while the "
                  "cabin {motion}. Finally the metallic doors slide apart "
                  "and open onto the destination floor. The camera never "
                  "moves; only the doors close and later reopen. "
                  "Photorealistic indoor lighting."),
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


def edge_color(a: str, b: str) -> str:
    """The vertex scheme, for an edge's crossings: red until any video is
    generated, green once every look pair has one in both directions,
    yellow for the work in between."""
    have = need = 0
    for la in [None] + store.variants_of(a):
        for lb in [None] + store.variants_of(b):
            for f, lf, t, lt in ((a, la, b, lb), (b, lb, a, la)):
                need += 1
                if output(f, lf, t, lt):
                    have += 1
    return (store.C_RED if have == 0
            else store.C_GRN if have == need else store.C_YEL)


def saved_prompt(frm, la, to, lb):
    p = job_dir(frm, la, to, lb) / "prompt.txt"
    return p.read_text().strip() if p.is_file() else None


# what a lift ride must NOT show — sent to the generator as extra negative
# prompt, the counterweight to the open-door endpoints
NEG_LIFT = ("doors staying open during the ride, view into the elevator "
            "shaft, exposed shaft walls, passing floor slabs, camera "
            "moving through the building")


def default_prompt(frm, to):
    kind = store.edge_kind(frm, to)
    if kind != "lift_ride":
        return PROMPTS[kind]
    dream = store.load_dream()
    elev = {ln: L.get("elevation", 0)
            for ln, L in store.load_levels().items()}
    up = elev.get(dream[to]["level"], 0) > elev.get(dream[frm]["level"], 0)
    return PROMPTS["lift_ride"].format(
        motion="rises to a higher floor" if up
        else "descends to a lower floor")


def ready() -> bool:
    return any(_ready(u) for u in URLS)


def _ready(url) -> bool:
    try:
        return (requests.get(f"{url}/health", timeout=3)
                .json().get("status") == "ok")
    except (requests.RequestException, ValueError):
        return False


def status_of(url):
    try:
        return requests.get(f"{url}/status", timeout=3).json()
    except (requests.RequestException, ValueError):
        return None


def statuses():
    """One entry per generator instance, None where one is not answering."""
    return [(u, status_of(u)) for u in URLS]


def status():
    """Every instance folded into one page-readable document: the running
    scenes with their clocks, the queues end to end, every last-finished
    job — plus the single-instance fields for anything still reading them."""
    sts = [s for _, s in statuses() if s]
    if not sts:
        return None
    running = {}
    for s in sts:
        if s.get("busy") and s.get("scene"):
            running[str(s["scene"]).rstrip("/")] = {
                "elapsed": s.get("elapsed", 0),
                "loaded": s.get("loaded", True)}
    first = next(iter(running), None)
    return {"busy": bool(running), "scene": first, "running": running,
            "queue": [q for s in sts for q in (s.get("queue") or [])],
            # per-instance views, for anything that reports a POSITION —
            # a place in one card's line, not in the concatenation
            "by": [{"scene": s.get("scene"), "queue": s.get("queue") or [],
                    "elapsed": s.get("elapsed"),
                    "loaded": s.get("loaded", True)} for s in sts],
            "dones": [s["done"] for s in sts if s.get("done")],
            "done": next((s["done"] for s in sts if s.get("done")), None),
            "elapsed": running[first]["elapsed"] if first else None,
            "loaded": running[first]["loaded"] if first else True}


def submit(frm, la, to, lb, prompt: str) -> dict:
    """Extract both conditioning frames along the edge bearing, save the
    prompt beside them, and queue the crossing."""
    dream = store.load_dream()
    a, b = dream[frm], dream[to]
    if a["level"] != b["level"]:
        # a lift ride is vertical: each end faces ITS OWN cabin door —
        # east was only ever lift1's coincidence, and lift2's door faces
        # the opposite way. The prompt says "doors directly ahead", so
        # the frames must actually show them.
        b_frm = store.lift_door_bearing(frm)
        b_to = store.lift_door_bearing(to)
    else:
        b_frm = b_to = math.atan2(-(b["y"] - a["y"]), b["x"] - a["x"])
    d = job_dir(frm, la, to, lb)
    d.mkdir(parents=True, exist_ok=True)
    for nm, look, tag, brg in ((frm, la, "first", b_frm),
                               (to, lb, "last", b_to)):
        pano = Image.open(store.pano_of(nm, look))
        _extract_perspective(pano, brg, 0.0, 1.2, 832, 480).save(
            d / f"{tag}.png")
    (d / "prompt.txt").write_text(prompt.strip() + "\n")
    (d / "crossing.mp4").unlink(missing_ok=True)
    body = {"dir": str(d), "prompt": prompt}
    if a["level"] != b["level"]:
        body["negative"] = NEG_LIFT
    # each instance only knows its own queue, so the dedupe and the routing
    # both live here: refuse a crossing ANY instance already holds, then
    # hand the job to the least-loaded one
    mine = str(d).rstrip("/")
    target, load = None, None
    for u, s in statuses():
        if s is None:
            continue
        held = [s.get("scene") or ""] + list(s.get("queue") or [])
        if mine in (str(x).rstrip("/") for x in held):
            raise RuntimeError("that crossing is already running or queued")
        n = (1 if s.get("busy") else 0) + len(s.get("queue") or [])
        if load is None or n < load:
            target, load = u, n
    if target is None:
        raise RuntimeError("no video generator is answering")
    r = requests.post(f"{target}/generate", json=body, timeout=30)
    if r.status_code == 409:
        raise RuntimeError("that crossing is already running or queued")
    r.raise_for_status()
    return r.json()


def short(scene: str) -> str:
    return str(scene).rstrip("/").rsplit("/", 1)[-1].replace("__", " → ")
