"""dreamworld_editor — grow a dreamworld level by level, pano by pano.

The flow this page will carry: panorama -> splat -> align -> walkthrough.
Minimal first: a minimap of each level, read straight from the project's
building.yaml. Vertices there are already in floorplan pixels (the map's
coordinate_system is reference_image), so unlike the main-branch dashboard
there is no metres->pixels affine to fit — the file IS the picture.

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

# lanes and doors in the dashboard's palette, walls in the splat viewer
# minimap's — each ported from the surface that drew that thing on main
C_LANE, C_DOOR, C_WALL = "#58a6ff", "#e0a030", "#3a4757"
C_VERT, C_INK = "#7aa2f7", "#0d1117"

app.add_static_files("/maps", str(PROJ / "maps"))


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
            "drawing": (L.get("drawing") or {}).get("filename", ""),
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


def level_svg(lv: dict) -> str:
    parts = []
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
        label = html.escape(name)
        parts.append(f'<circle cx="{x}" cy="{y}" r="6" fill="{C_VERT}" '
                     f'stroke="{C_INK}" stroke-width="1.5"/>')
        # a white halo keeps the label readable on the drawing's own linework
        parts.append(f'<text x="{x}" y="{y - 9}" font-size="12" '
                     f'text-anchor="middle" fill="{C_INK}" stroke="#ffffff" '
                     f'stroke-width="3" paint-order="stroke">{label}</text>')
    return "".join(parts)


def chip(color: str, text: str) -> None:
    ui.html(f'<span style="display:inline-flex;align-items:center;gap:6px">'
            f'<span style="width:10px;height:10px;border-radius:5px;'
            f'background:{color}"></span>{text}</span>')


@ui.page("/")
def index():
    sel = {"level": None}

    def on_click(lv):
        def handler(e):
            best, dist = None, 20.0
            for name, x, y in lv["verts"]:
                d = ((e.image_x - x) ** 2 + (e.image_y - y) ** 2) ** 0.5
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
        with ui.column().classes("w-full px-4"):
            ui.label(f"{len(lv['verts'])} vertices · "
                     f"{len(lv['lanes'])} lanes · "
                     f"{len(lv['doors'])} doors").classes(
                "text-xs text-gray-500")
            ui.interactive_image(
                f"{MOUNT}/maps/{lv['drawing']}",
                content=level_svg(lv),
                events=["mousedown"], on_mouse=on_click(lv),
            ).classes("w-full")

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
