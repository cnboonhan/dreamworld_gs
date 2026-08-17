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
from config import DREAM, MOUNT

DREAM.mkdir(parents=True, exist_ok=True)
app.add_static_files("/files", str(DREAM))

fastapi_app = FastAPI()
ui.run_with(fastapi_app, mount_path=MOUNT, title="dreamworld editor",
            dark=True, favicon="🌐")
