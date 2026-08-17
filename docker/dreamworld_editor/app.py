"""dreamworld_editor — grow a dreamworld level by level, pano by pano.

The flow this page will carry: panorama -> splat -> align -> walkthrough.
Minimal first: a minimap of each level, read straight from the project's
building.yaml and drawn the way the splat viewer's picker draws its plan —
an abstract line drawing on a dark ground, no floorplan raster. Vertices in
the yaml are already in a single pixel frame (coordinate_system is
reference_image), so the geometry needs no projection, only a shift to the
drawing's own bounding box.

The page reflects the map as it is traced next door in /sim_editor: a watcher
rereads the yaml when it changes. Everything the flow produces — uploaded
panoramas, splats, alignment — will live under the project's dreamworld/
tree and be written through this UI; nothing here reads the legacy folders.

NiceGUI mounts ITSELF at /dreamworld_editor, so the proxy passes the prefix
through unstripped and page, assets and socket all agree on where they live.
"""

import html
import os
from pathlib import Path

import yaml
from fastapi import FastAPI
from nicegui import app, ui

MOUNT = "/dreamworld_editor"
PROJECT = os.environ.get("DW_PROJECT", "multilevel_office")
PROJ = Path("/projects") / PROJECT

# the splat viewer minimap's palette: its dark ground and wall stroke, the
# dashboard's lane, door and label colors — the family look from main
C_BG, C_WALL = "#0a0d12", "#3a4757"
C_LANE, C_DOOR = "#58a6ff", "#e0a030"
C_VERT, C_INK, C_LABEL = "#7aa2f7", "#0d1117", "#7d8590"


def building_file():
    hits = sorted((PROJ / "maps").glob("*.building.yaml"))
    return hits[0] if hits else None


def load_levels() -> dict:
    f = building_file()
    if not f:
        return {}
    levels = {}
    for name, L in (yaml.safe_load(f.read_text()).get("levels") or {}).items():
        V = L.get("vertices") or []

        def seg(edges):
            return [(V[e[0]][0], V[e[0]][1], V[e[1]][0], V[e[1]][1])
                    for e in edges or []]

        levels[name] = {
            "walls": seg(L.get("walls")),
            "doors": seg(L.get("doors")),
            "lanes": seg(L.get("lanes")),
            "verts": [(v[3], v[0], v[1]) for v in V
                      if len(v) > 3 and isinstance(v[3], str) and v[3]],
        }
    return levels


def signature():
    f = building_file()
    return f.stat().st_mtime if f else 0


def level_scene(lv: dict):
    """(svg, width, height, shift) for one level's line drawing. The yaml's
    pixel frame is shifted to the drawing's own bounding box, so the image
    is exactly as big as the building plus a margin."""
    pts = [(x, y) for seg in ("walls", "doors", "lanes")
           for x1, y1, x2, y2 in lv[seg] for x, y in ((x1, y1), (x2, y2))]
    pts += [(x, y) for _, x, y in lv["verts"]]
    if not pts:
        return ('<text x="200" y="150" font-size="14" text-anchor="middle" '
                f'fill="{C_LABEL}">nothing on this level yet</text>', 400, 300,
                (0, 0))
    pad = 40
    tx = pad - min(x for x, _ in pts)
    ty = pad - min(y for _, y in pts)
    w = int(max(x for x, _ in pts) + tx + pad + 1)
    h = int(max(y for _, y in pts) + ty + pad + 1)
    parts = [f'<rect x="0" y="0" width="{w}" height="{h}" fill="{C_BG}"/>',
             f'<g transform="translate({tx},{ty})">']
    for x1, y1, x2, y2 in lv["walls"]:
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     f'stroke="{C_WALL}" stroke-width="2"/>')
    for x1, y1, x2, y2 in lv["doors"]:
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     f'stroke="{C_DOOR}" stroke-width="3.5"/>')
    for x1, y1, x2, y2 in lv["lanes"]:
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     f'stroke="{C_LANE}" stroke-width="2.5" opacity="0.85"/>')
    for name, x, y in lv["verts"]:
        parts.append(f'<circle cx="{x}" cy="{y}" r="6" fill="{C_VERT}" '
                     f'stroke="{C_INK}" stroke-width="1.5"/>')
        parts.append(f'<text x="{x}" y="{y - 9}" font-size="12" '
                     f'text-anchor="middle" fill="{C_LABEL}">'
                     f'{html.escape(name)}</text>')
    parts.append('</g>')
    return "".join(parts), w, h, (tx, ty)


def chip(color: str, text: str) -> None:
    ui.html(f'<span style="display:inline-flex;align-items:center;gap:6px">'
            f'<span style="width:10px;height:10px;border-radius:5px;'
            f'background:{color}"></span>{text}</span>')


@ui.page("/")
def index():
    sel = {"level": None}

    def on_click(lv, shift):
        def handler(e):
            best, dist = None, 20.0
            for name, x, y in lv["verts"]:
                d = ((e.image_x - shift[0] - x) ** 2 +
                     (e.image_y - shift[1] - y) ** 2) ** 0.5
                if d < dist:
                    best, dist = name, d
            if best:
                ui.notify(best)
        return handler

    with ui.header().classes("items-center bg-[#0b0e13] gap-6"):
        ui.label("dreamworld editor").classes("text-lg font-bold text-[#4ea1ff]")
        ui.label(PROJECT).classes("text-sm text-gray-500")
        with ui.row().classes("gap-4 text-xs text-gray-400 items-center"):
            chip(C_LANE, "lane")
            chip(C_DOOR, "door")
            chip(C_WALL, "wall")

    names = list(load_levels())
    sel["level"] = names[0] if names else None
    picker = ui.select(names, value=sel["level"], label="level",
                       on_change=lambda e: (sel.update(level=e.value),
                                            board.refresh())
                       ).classes("w-40 mx-4")

    @ui.refreshable
    def board():
        levels = load_levels()
        if not levels:
            ui.label(f"no *.building.yaml under {PROJ}/maps — "
                     f"trace one in /sim_editor first").classes("m-4")
            return
        if sel["level"] not in levels:
            sel["level"] = next(iter(levels))
        lv = levels[sel["level"]]
        svg, w, h, shift = level_scene(lv)
        with ui.column().classes("w-full px-4"):
            # no source image at all — the plan IS the drawing; size gives
            # the SVG its coordinate frame
            ui.interactive_image(
                size=(w, h), content=svg,
                events=["mousedown"], on_mouse=on_click(lv, shift),
            ).classes("w-full max-w-4xl")

    board()

    state = {"sig": signature()}

    def poll():
        sig = signature()
        if sig != state["sig"]:
            state["sig"] = sig
            fresh = list(load_levels())
            if fresh != picker.options:
                picker.options = fresh
                picker.update()
            board.refresh()

    ui.timer(2.0, poll)


fastapi_app = FastAPI()
ui.run_with(fastapi_app, mount_path=MOUNT, title="dreamworld editor",
            dark=True, favicon="🌐")
