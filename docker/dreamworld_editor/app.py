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

import re
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from nicegui import app, run, ui

import page  # noqa: F401 — importing registers the @ui.page routes
import store
from config import DREAM, MOUNT

DREAM.mkdir(parents=True, exist_ok=True)
app.add_static_files("/files", str(DREAM))


@app.get("/graph")
def graph():
    """The dreamworld, for the walkthrough viewer at /dreamworld_viewer."""
    import crossing  # here, not at top: crossing imports store imports config
    doc = store.graph_doc()
    # the edges' traffic lights, keyed a__b — computed here because store
    # cannot import crossing (crossing imports store)
    doc["edge_colors"] = {f"{a}__{b}": crossing.edge_color(a, b)
                          for a, b in doc.get("edges") or []}
    return doc


@app.post("/upload_pano")
async def upload_pano(request: Request):
    """One slice of a paced panorama upload (js/dw_upload.js is the sender).

    The slices accumulate OUTSIDE the dreamworld tree so the 2-second
    change watcher stays quiet until the finished panorama lands — then it
    fires once and every open page picks the vertex up."""
    q = request.query_params
    name = q.get("vertex", "")
    uid = q.get("id", "")
    suffix = Path(q.get("name", "")).suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png"):
        return PlainTextResponse("jpg or png only", status_code=400)
    if not re.fullmatch(r"[a-z0-9]{1,16}", uid):
        return PlainTextResponse("bad upload id", status_code=400)
    if ("/" in name or ".." in name
            or not (DREAM / name / "vertex.json").is_file()):
        return PlainTextResponse(f"no such vertex: {name}", status_code=404)
    replace = q.get("replace") == "1"
    if store.pano_of(name) and not replace:
        return PlainTextResponse(f"{name} already has a panorama",
                                 status_code=409)
    part = Path(tempfile.gettempdir()) / f"dw_upload_{uid}.part"
    data = await request.body()
    with part.open("wb" if q.get("seq") == "0" else "ab") as f:
        f.write(data)
    if q.get("last") == "1":
        if replace:
            # a new shot of the same place: whatever extension the old one
            # wore goes, and the alignment record with it — the new pixels
            # arrive with an unknown correction, so the vertex turns red
            # until it is re-aligned. Everything GENERATED from the old
            # shot goes too (splat worlds, crossing videos at this
            # vertex); variants stay, they are edits the user made.
            for ext in (".jpg", ".jpeg", ".png"):
                if ext != suffix:
                    (DREAM / name / f"pano{ext}").unlink(missing_ok=True)
            (DREAM / name / "aligned.json").unlink(missing_ok=True)
            await run.io_bound(store.purge_worlds, name)
        await run.io_bound(shutil.move, str(part),
                           str(DREAM / name / f"pano{suffix}"))
        await run.io_bound(store.preview_of, name)
    return {"ok": True}

fastapi_app = FastAPI()
# reconnect_timeout: how long a disconnected client's state survives on the
# server. The default 3s is shorter than any real tunnel hiccup, so every
# blip ended in "Connection lost" and a full page reload — zoom, selection
# and in-flight uploads gone. Within this window the page resumes in place.
ui.run_with(fastapi_app, mount_path=MOUNT, title="dreamworld editor",
            dark=True, favicon="🌐", reconnect_timeout=300)
