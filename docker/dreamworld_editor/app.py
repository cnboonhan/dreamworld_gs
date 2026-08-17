"""dreamworld_editor — grow a dreamworld level by level, pano by pano.

The flow this page carries: panorama -> splat -> align -> walkthrough,
over a minimap read from the traffic editor's building.yaml and drawn the
way the splat viewer's picker draws its plan. One module per concern:

    config.py   where everything lives and how it looks
    store.py    the dreamworld tree and the map readers — the tree's ONLY
                writer; building.yaml is never written here
    scene.py    the level line drawing
    page.py     the page: toolbar, plan, vertex/panorama/splat boxes
    js/         the two vanilla-JS pieces NiceGUI does not carry:
                pan/zoom on the plan, the equirect panorama viewer

NiceGUI mounts ITSELF at MOUNT, so the proxy passes the prefix through
unstripped and page, assets and socket all agree on where they live.
"""

from fastapi import FastAPI
from nicegui import app, ui

import page  # noqa: F401 — importing registers the @ui.page routes
import store
from config import DREAM, MOUNT

DREAM.mkdir(parents=True, exist_ok=True)
app.add_static_files("/files", str(DREAM))


@app.get("/graph")
def graph():
    """The dreamworld, for the walkthrough viewer at /dreamworld_viewer."""
    return store.graph_doc()


# ---- where the viewer is, for the harness to come ---------------------------
# The viewer pushes its state here (main's truth-protocol shape: the walker
# reports, the broker holds, anyone may ask). RMF owns the building's
# infrastructure; the walkthrough is the viewer's; this is the seam between
# them — a harness reads /viewer/state to know where the camera stands, which
# vertex it left, which edge it is crossing, and coordinates doors and lifts
# through RMF accordingly.

import time as _time

VIEWER = {"state": None, "stamp": 0.0}


@app.post("/viewer/state")
def viewer_state_post(doc: dict):
    VIEWER["state"] = doc
    VIEWER["stamp"] = _time.time()
    return {"ok": True}


@app.get("/viewer/state")
def viewer_state_get():
    age = (_time.time() - VIEWER["stamp"]) if VIEWER["state"] else None
    return {"state": VIEWER["state"], "age": age,
            "live": age is not None and age < 2.0}

fastapi_app = FastAPI()
ui.run_with(fastapi_app, mount_path=MOUNT, title="dreamworld editor",
            dark=True, favicon="🌐")
