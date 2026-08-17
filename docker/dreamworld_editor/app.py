"""dreamworld_editor — grow a dreamworld level by level, pano by pano.

The flow this page carries: panorama -> splat -> align -> walkthrough. A
minimap of each level, read straight from the project's building.yaml and
drawn the way the splat viewer's picker draws its plan — an abstract line
drawing on a dark ground. Switch on "add vertex" and click the plan to drop
a vertex; click a vertex to select it, rename it, upload the panorama shot
there, and align it.

Everything the flow produces lives under the project's dreamworld/ tree,
one folder per vertex — vertex.json, pano.jpg, aligned.json, splat later —
and this UI is that tree's only writer. building.yaml stays the traffic
editor's file: walls, doors and scale are read from it, never written.

The panorama viewer and its alignment are ported from main's
scripts/align_panos.py: a WebGL equirect projection you turn by dragging
until the corridor a neighbour names sits on the dashed centre line, then
save — which ROLLS the image file itself, so every reader downstream gets
a panorama already in the building's frame with no yaw field to plumb
through. The applied total is kept beside it in aligned.json, because the
file carries no record of having been turned.

NiceGUI mounts ITSELF at /dreamworld_editor, so the proxy passes the prefix
through unstripped and page, assets and socket all agree on where they live.
"""

import json
import math
import os
import shutil
from pathlib import Path

import numpy as np
import yaml
from fastapi import FastAPI
from nicegui import app, run, ui
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

MOUNT = "/dreamworld_editor"
PROJECT = os.environ.get("DW_PROJECT", "multilevel_office")
PROJ = Path("/projects") / PROJECT
DREAM = PROJ / "dreamworld"
PREVIEW_W = 2048        # main's number: wide enough to aim by, light enough

# the splat viewer minimap's palette: its dark ground and wall stroke, the
# dashboard's lane, door and label colors — the family look from main
C_BG, C_WALL = "#0a0d12", "#3a4757"
C_LANE, C_DOOR = "#58a6ff", "#e0a030"
C_VERT, C_INK, C_LABEL, C_SEL = "#7aa2f7", "#0d1117", "#7d8590", "#4ea1ff"

DREAM.mkdir(parents=True, exist_ok=True)
app.add_static_files("/files", str(DREAM))


# ---- the map, read from the traffic editor's file ---------------------------

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

        # the measurement anchors the drawing's scale; without one distances
        # can only be reported in pixels
        scale = None
        for m in L.get("measurements") or []:
            px = math.hypot(V[m[1]][0] - V[m[0]][0], V[m[1]][1] - V[m[0]][1])
            metres = (m[2] or {}).get("distance", [0, 0])[1]
            if px > 0 and metres:
                scale = metres / px
        levels[name] = {
            "walls": seg(L.get("walls")),
            "doors": seg(L.get("doors")),
            "lanes": seg(L.get("lanes")),
            "verts": [(v[3], v[0], v[1]) for v in V
                      if len(v) > 3 and isinstance(v[3], str) and v[3]],
            "scale": scale,
        }
    return levels


# ---- the dreamworld tree, this UI's own store -------------------------------

def pano_of(name: str):
    d = DREAM / name
    for ext in (".jpg", ".jpeg", ".png"):
        if (d / f"pano{ext}").is_file():
            return d / f"pano{ext}"
    return None


def load_dream() -> dict:
    out = {}
    if not DREAM.is_dir():
        return out
    for d in sorted(DREAM.iterdir()):
        vj = d / "vertex.json"
        if not d.is_dir() or not vj.is_file():
            continue
        try:
            v = json.loads(vj.read_text())
        except (OSError, ValueError):
            continue
        applied = None
        rec = d / "aligned.json"
        if rec.is_file():
            try:
                applied = float(json.loads(rec.read_text())["degrees"])
            except (OSError, ValueError, KeyError):
                pass
        out[d.name] = {"level": v.get("level", ""), "x": float(v["x"]),
                       "y": float(v["y"]), "pano": pano_of(d.name) is not None,
                       "applied": applied}
    return out


def new_vertex(level: str, x: float, y: float) -> str:
    dream = load_dream()
    n = 0
    while f"{level}.v{n}" in dream:
        n += 1
    name = f"{level}.v{n}"
    d = DREAM / name
    d.mkdir(parents=True)
    (d / "vertex.json").write_text(json.dumps(
        {"level": level, "x": round(x, 2), "y": round(y, 2)}, indent=1))
    return name


def bearings_from(dream: dict, name: str, scale) -> list:
    """Where the other vertices of this level lie, as compass bearings —
    the things a panorama can be aimed by. Main aimed by lanes; before any
    lanes exist, every neighbouring vertex is a landmark."""
    me = dream[name]
    out = []
    for other, v in dream.items():
        if other == name or v["level"] != me["level"]:
            continue
        dx, dy = v["x"] - me["x"], v["y"] - me["y"]
        d = math.hypot(dx, dy)
        if d < 1e-6:
            continue
        # drawing pixels run y-down; bearings live in the y-up compass frame
        out.append({"to": other, "bearing": math.atan2(-dy, dx),
                    "dist": d * scale if scale else d})
    out.sort(key=lambda o: o["dist"])
    return out[:8]


def preview_of(name: str) -> Path:
    """The downscaled copy the browser gets, made on first ask — main's
    pattern, sized to main's width."""
    src = pano_of(name)
    p = DREAM / name / "pano.preview.jpg"
    if src and (not p.is_file() or p.stat().st_mtime < src.stat().st_mtime):
        Image.open(src).convert("RGB").resize(
            (PREVIEW_W, PREVIEW_W // 2), Image.LANCZOS).save(p, quality=88)
    return p


def apply_roll(name: str, degrees: float) -> int:
    """Roll the panorama by `degrees` and record the running total.

    The shift is NEGATIVE, and that sign is the whole bug main already paid
    for: the preview shows content at longitude L appearing at L + corr,
    while np.roll by +shift moves it from L to L - delta. Opposite signs —
    save with the wrong one and a panorama turned until it looked right
    comes back with the corridor at twice the angle on the wrong side.
    """
    f = pano_of(name)
    im = np.asarray(Image.open(f).convert("RGB"))
    shift = -int(round(degrees / 360.0 * im.shape[1]))
    Image.fromarray(np.roll(im, shift, axis=1)).save(f, quality=95)
    (DREAM / name / "pano.preview.jpg").unlink(missing_ok=True)
    preview_of(name)
    rec = DREAM / name / "aligned.json"
    old = 0.0
    if rec.is_file():
        try:
            old = float(json.loads(rec.read_text())["degrees"])
        except (OSError, ValueError, KeyError):
            pass
    rec.write_text(json.dumps({"degrees": round((old + degrees) % 360.0, 2),
                               "last": round(degrees, 2)}, indent=1))
    return abs(shift)


def signature():
    """Cheap change detector: the map file plus everything in the dreamworld
    tree. Another client's upload or roll must redraw this one too."""
    f = building_file()
    sig = [str(f.stat().st_mtime if f else 0)]
    if DREAM.is_dir():
        for d in sorted(DREAM.iterdir()):
            if d.is_dir():
                sig.append(d.name)
                sig.extend(f"{x.name}:{x.stat().st_mtime}"
                           for x in sorted(d.iterdir()))
    return hash(tuple(sig))


# ---- the drawing -------------------------------------------------------------

def level_scene(lv: dict, dream: dict, level: str, selected):
    """(svg, width, height, shift) for one level's line drawing. The yaml's
    pixel frame is shifted to the drawing's own bounding box, so the image
    is exactly as big as the building plus a margin."""
    mine = {n: v for n, v in dream.items() if v["level"] == level}
    pts = [(x, y) for seg in ("walls", "doors", "lanes")
           for x1, y1, x2, y2 in lv[seg] for x, y in ((x1, y1), (x2, y2))]
    pts += [(x, y) for _, x, y in lv["verts"]]
    pts += [(v["x"], v["y"]) for v in mine.values()]
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

    def label(x, y, text, color):
        import html as _h
        return (f'<text x="{x}" y="{y - 10}" font-size="12" '
                f'text-anchor="middle" fill="{color}">{_h.escape(text)}</text>')

    for name, x, y in lv["verts"]:
        parts.append(f'<circle cx="{x}" cy="{y}" r="6" fill="{C_VERT}" '
                     f'stroke="{C_INK}" stroke-width="1.5"/>')
        parts.append(label(x, y, name, C_LABEL))
    for name, v in mine.items():
        x, y = v["x"], v["y"]
        if name == selected:
            parts.append(f'<circle cx="{x}" cy="{y}" r="11" fill="none" '
                         f'stroke="{C_SEL}" stroke-width="2.5"/>')
        # filled once its panorama is up, hollow while it waits — the same
        # ring the dashboard uses for a waypoint with no world built
        if v["pano"]:
            parts.append(f'<circle cx="{x}" cy="{y}" r="6" fill="{C_VERT}" '
                         f'stroke="{C_INK}" stroke-width="1.5"/>')
        else:
            parts.append(f'<circle cx="{x}" cy="{y}" r="6" fill="{C_BG}" '
                         f'stroke="{C_VERT}" stroke-width="2"/>')
        parts.append(label(x, y, name,
                           C_SEL if name == selected else C_LABEL))
    parts.append('</g>')
    return "".join(parts), w, h, (tx, ty)


# Pan and zoom, the one interaction NiceGUI does not carry: a CSS transform
# on the drawing inside a clipping box. Wheel pans (a touchpad's two-finger
# scroll), pinch or ctrl+wheel zooms toward the cursor (a touchpad pinch
# arrives as exactly that), dragging pans, double-click refits. offsetX-based
# hit-testing is computed in the element's local frame, so the click-to-name
# handler keeps working untouched under any transform.
DW_VIEW_JS = """<script>
window.dwView = (id, w, h) => {
  const box = document.getElementById('c' + id);
  if (!box || box.dataset.dw) return;
  box.dataset.dw = 1;
  const kid = box.firstElementChild;
  // the imageless interactive_image has NO intrinsic size — just an
  // aspect-ratio and width:100%, so it lays out as wide as this box and
  // the fit math scales garbage. Pin its width and the aspect-ratio makes
  // layout size equal declared size, which the math assumes.
  kid.style.width = w + 'px';
  kid.style.transformOrigin = '0 0';
  let k = 1, px = 0, py = 0, drag = null;
  const apply = () => kid.style.transform =
    `translate(${px}px,${py}px) scale(${k})`;
  const fit = () => { const r = box.getBoundingClientRect();
    k = Math.min(r.width / w, r.height / h);
    px = (r.width - w * k) / 2; py = (r.height - h * k) / 2; apply(); };
  fit();
  box.addEventListener('wheel', e => { e.preventDefault();
    if (e.ctrlKey || e.metaKey) {
      const r = box.getBoundingClientRect();
      const cx = e.clientX - r.left, cy = e.clientY - r.top;
      const nk = Math.min(20, Math.max(0.05, k * Math.exp(-e.deltaY * 0.01)));
      px = cx - (cx - px) * nk / k; py = cy - (cy - py) * nk / k; k = nk;
    } else { px -= e.deltaX; py -= e.deltaY; }
    apply(); }, {passive: false});
  box.addEventListener('pointerdown', e => {
    drag = [e.clientX, e.clientY]; box.setPointerCapture(e.pointerId); });
  box.addEventListener('pointermove', e => { if (!drag) return;
    px += e.clientX - drag[0]; py += e.clientY - drag[1];
    drag = [e.clientX, e.clientY]; apply(); });
  box.addEventListener('pointerup', () => drag = null);
  box.addEventListener('dblclick', fit);
};
</script>"""

# The panorama viewer, ported from main's align_panos.py: the same fragment
# shader (our convention: column c holds lon = pi - 2pi(c+0.5)/W), the same
# drag-to-turn at 0.12 degrees per pixel, the same fov wheel. `corr` previews
# the roll that saving will bake into the file. Controls stay in Python;
# this only renders, turns, and answers dwPanoOff() when asked.
DW_PANO_JS = """<script>
window.dwPano = (id, url, offId) => {
  const cv = document.getElementById('c' + id);
  if (!cv || cv.dataset.dw) return;
  cv.dataset.dw = 1;
  const st = { off: 0, look: 0, pitch: 0, fov: 1.6, drag: null, ready: false };
  const gl = cv.getContext('webgl', { antialias: true });
  const VS = 'attribute vec2 p;void main(){gl_Position=vec4(p,0.0,1.0);}';
  const FS = 'precision highp float;uniform sampler2D tex;uniform vec2 res;' +
    'uniform float yaw,pitch,fov,corr;const float PI=3.14159265358979;' +
    'void main(){' +
    'vec3 F=vec3(cos(pitch)*cos(yaw),cos(pitch)*sin(yaw),sin(pitch));' +
    'vec3 R=normalize(cross(F,vec3(0.0,0.0,1.0)));vec3 U=cross(R,F);' +
    'float t=tan(fov*0.5);vec2 c=(gl_FragCoord.xy-0.5*res)/(0.5*res.x);' +
    'vec3 d=normalize(F+c.x*t*R+c.y*t*U);' +
    'float lon=atan(d.y,d.x)-corr,lat=asin(clamp(d.z,-1.0,1.0));' +
    'gl_FragColor=texture2D(tex,vec2((PI-lon)/(2.0*PI),0.5-lat/PI));}';
  const mk = (t, s) => { const o = gl.createShader(t);
    gl.shaderSource(o, s); gl.compileShader(o); return o; };
  const prog = gl.createProgram();
  gl.attachShader(prog, mk(gl.VERTEX_SHADER, VS));
  gl.attachShader(prog, mk(gl.FRAGMENT_SHADER, FS));
  gl.linkProgram(prog); gl.useProgram(prog);
  const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,3,-1,-1,3]),
                gl.STATIC_DRAW);
  const pl = gl.getAttribLocation(prog, 'p');
  gl.enableVertexAttribArray(pl);
  gl.vertexAttribPointer(pl, 2, gl.FLOAT, false, 0, 0);
  const U = {};
  for (const n of ['res','yaw','pitch','fov','corr'])
    U[n] = gl.getUniformLocation(prog, n);
  const tex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  const readout = () => { const el = document.getElementById('c' + offId);
    if (el) el.textContent = st.off.toFixed(1) + '\\u00b0'; };
  cv.addEventListener('pointerdown', e => {
    st.drag = e.clientX; cv.setPointerCapture(e.pointerId); });
  cv.addEventListener('pointermove', e => { if (st.drag === null) return;
    st.off = (st.off + (e.clientX - st.drag) * 0.12 + 360) % 360;
    st.drag = e.clientX; readout(); });
  cv.addEventListener('pointerup', () => st.drag = null);
  cv.addEventListener('wheel', e => { e.preventDefault();
    st.fov = Math.max(0.5, Math.min(2.6, st.fov * (1 + e.deltaY * 0.001)));
  }, { passive: false });
  const im = new Image();
  im.onload = () => { gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, im);
    st.ready = true; };
  im.src = url;
  const loop = () => {
    if (!cv.isConnected) return;               // gone with its card
    const w = cv.clientWidth, h = cv.clientHeight;
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    gl.viewport(0, 0, w, h);
    gl.uniform2f(U.res, w, h);
    gl.uniform1f(U.yaw, st.look); gl.uniform1f(U.pitch, st.pitch);
    gl.uniform1f(U.fov, st.fov);
    gl.uniform1f(U.corr, st.off * Math.PI / 180);
    if (st.ready) gl.drawArrays(gl.TRIANGLES, 0, 3);
    else { gl.clearColor(0.04, 0.05, 0.07, 1); gl.clear(gl.COLOR_BUFFER_BIT); }
    requestAnimationFrame(loop);
  };
  loop();
  window.dwPanoFace = r => { st.look = r; st.pitch = 0; };
  window.dwPanoNudge = d => { st.off = (st.off + d + 360) % 360; readout(); };
  window.dwPanoOff = () => st.off;
};
</script>"""


def chip(color: str, text: str) -> None:
    ui.html(f'<span style="display:inline-flex;align-items:center;gap:6px">'
            f'<span style="width:10px;height:10px;border-radius:5px;'
            f'background:{color}"></span>{text}</span>')


@ui.page("/")
def index():
    # inside the page builder, not at module level: head html added outside
    # a page lands on NiceGUI's auto-index client, and this page never sees it
    ui.add_head_html(DW_VIEW_JS)
    ui.add_head_html(DW_PANO_JS)
    sel = {"level": None, "vertex": None, "add": False}

    with ui.header().classes("items-center bg-[#0b0e13] gap-6"):
        ui.label("dreamworld editor").classes("text-lg font-bold text-[#4ea1ff]")
        ui.label(PROJECT).classes("text-sm text-gray-500")
        with ui.row().classes("gap-4 text-xs text-gray-400 items-center"):
            chip(C_LANE, "lane")
            chip(C_DOOR, "door")
            chip(C_WALL, "wall")
            chip(C_VERT, "vertex (hollow = no panorama)")

    def refresh_all():
        state["sig"] = signature()
        board.refresh()
        side.refresh()

    # ---- toolbar ----
    names = list(load_levels())
    sel["level"] = names[0] if names else None
    with ui.row().classes("items-center px-4 gap-6"):
        picker = ui.select(names, value=sel["level"], label="level",
                           on_change=lambda e: (sel.update(level=e.value),
                                                board.refresh())
                           ).classes("w-40")
        ui.switch("add vertex",
                  on_change=lambda e: sel.update(add=e.value))
        ui.label("click the plan to drop one · click a vertex to select it"
                 ).classes("text-xs text-gray-600")

    # ---- the plan and the two boxes beside it ----
    def on_map_mouse(dream, shift):
        down = {}

        def handler(e):
            x, y = e.image_x - shift[0], e.image_y - shift[1]
            if e.type == "mousedown":
                down["at"] = (x, y)
                return
            if e.type != "mouseup" or "at" not in down:
                return
            moved = math.hypot(x - down["at"][0], y - down["at"][1])
            down.clear()
            if moved > 5:            # that was a pan, not a click
                return
            if sel["add"]:
                sel["vertex"] = new_vertex(sel["level"], x, y)
                ui.notify(f"dropped {sel['vertex']}")
            else:
                best, bd = None, 15.0
                for nm, v in dream.items():
                    if v["level"] != sel["level"]:
                        continue
                    d = math.hypot(v["x"] - x, v["y"] - y)
                    if d < bd:
                        best, bd = nm, d
                sel["vertex"] = best
            refresh_all()
        return handler

    with ui.row().classes("w-full px-4 gap-4 flex-nowrap items-start"):

        with ui.column().classes("grow min-w-0"):
            @ui.refreshable
            def board():
                levels = load_levels()
                if not levels:
                    ui.label(f"no *.building.yaml under {PROJ}/maps — "
                             f"trace one in /sim_editor first").classes("m-4")
                    return
                if sel["level"] not in levels:
                    sel["level"] = next(iter(levels))
                dream = load_dream()
                svg, w, h, shift = level_scene(levels[sel["level"]], dream,
                                               sel["level"], sel["vertex"])
                box = ui.element("div").classes("w-full").style(
                    f"height:78vh;overflow:hidden;touch-action:none;"
                    f"background:{C_BG};border-radius:8px;cursor:grab")
                with box:
                    ui.interactive_image(
                        size=(w, h), content=svg,
                        events=["mousedown", "mouseup"],
                        on_mouse=on_map_mouse(dream, shift),
                    )
                ui.label("drag or scroll to pan · pinch or ctrl+scroll to "
                         "zoom · double-click to fit").classes(
                    "text-xs text-gray-600")
                ui.timer(0.1, lambda: ui.run_javascript(
                    f"dwView({box.id}, {w}, {h})"), once=True)
            board()

        with ui.column().classes("w-[430px] shrink-0 gap-4"):
            @ui.refreshable
            def side():
                dream = load_dream()
                name = sel["vertex"] if sel["vertex"] in dream else None
                if not name:
                    ui.label("no vertex selected — click one on the plan, "
                             "or switch on add vertex and click a spot"
                             ).classes("text-sm text-gray-500 mt-2")
                    return
                v = dream[name]
                scale = (load_levels().get(v["level"]) or {}).get("scale")

                # -- the vertex itself: rename, delete --
                with ui.card().classes("w-full bg-[#11151c]"):
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(name).classes("font-bold text-[#7aa2f7]")
                        ui.label(f"{v['level']} · ({v['x']:.0f}, {v['y']:.0f})"
                                 ).classes("text-xs text-gray-500")
                    with ui.row().classes("w-full items-end gap-2"):
                        field = ui.input("rename to", value=name).classes(
                            "grow").props("dense")

                        def rename():
                            new = field.value.strip()
                            if not new or new == name:
                                return
                            if "/" in new or new.startswith("."):
                                ui.notify("not a usable name",
                                          type="negative")
                                return
                            if (DREAM / new).exists():
                                ui.notify(f"{new} already exists",
                                          type="negative")
                                return
                            (DREAM / name).rename(DREAM / new)
                            sel["vertex"] = new
                            refresh_all()

                        ui.button("rename", on_click=rename).props("dense")

                        def delete():
                            with ui.dialog() as dlg, ui.card():
                                ui.label(f"delete {name} and everything "
                                         f"under it?")
                                with ui.row():
                                    def yes():
                                        shutil.rmtree(DREAM / name)
                                        sel["vertex"] = None
                                        dlg.close()
                                        refresh_all()
                                    ui.button("delete", color="negative",
                                              on_click=yes)
                                    ui.button("keep", on_click=dlg.close)
                            dlg.open()

                        ui.button("delete", on_click=delete).props(
                            "dense flat color=negative")

                # -- panorama: upload, view, align --
                with ui.card().classes("w-full bg-[#11151c]"):
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label("panorama").classes("font-bold")
                        if not v["pano"]:
                            ui.label("none yet").classes(
                                "text-xs text-gray-500")
                        elif v["applied"] is not None:
                            ui.label(f"✓ {v['applied']:.1f}° rolled in"
                                     ).classes("text-xs text-[#6c6]")
                        else:
                            ui.label("not yet aligned").classes(
                                "text-xs text-[#f0a35e]")

                    if not v["pano"]:
                        async def on_upload(e):
                            suffix = Path(e.name).suffix.lower()
                            if suffix not in (".jpg", ".jpeg", ".png"):
                                ui.notify("jpg or png only", type="negative")
                                return
                            data = e.content.read()
                            (DREAM / name / f"pano{suffix}").write_bytes(data)
                            await run.io_bound(preview_of, name)
                            ui.notify(f"panorama saved — "
                                      f"{len(data) / 1e6:.1f} MB")
                            refresh_all()

                        ui.upload(on_upload=on_upload, auto_upload=True
                                  ).props('accept=".jpg,.jpeg,.png" flat'
                                          ).classes("w-full")
                        ui.label("upload the equirectangular panorama shot "
                                 "at this vertex").classes(
                            "text-xs text-gray-500")
                    else:
                        p = preview_of(name)
                        url = (f"{MOUNT}/files/{name}/pano.preview.jpg"
                               f"?t={int(p.stat().st_mtime)}")
                        brs = bearings_from(dream, name, scale)

                        with ui.element("div").classes("w-full relative"):
                            cv = ui.element("canvas").classes("w-full").style(
                                "height:300px;display:block;cursor:ew-resize;"
                                f"touch-action:none;background:{C_BG};"
                                "border-radius:6px")
                            ui.html('<div style="position:absolute;left:50%;'
                                    'top:0;bottom:0;border-left:2px dashed '
                                    '#4ea1ff;pointer-events:none"></div>')
                            facing = ui.label("").classes(
                                "absolute top-2 left-1/2 ml-2 text-xs px-2 "
                                "py-0.5 rounded z-10").style(
                                "background:#4ea1ff;color:#08121e")

                        if brs:
                            with ui.row().classes("w-full gap-1"):
                                for b in brs:
                                    d = (f"{b['dist']:.1f}m" if scale
                                         else f"{b['dist']:.0f}px")

                                    def face(b=b):
                                        ui.run_javascript(
                                            "window.dwPanoFace && "
                                            f"dwPanoFace({b['bearing']})")
                                        facing.set_text(f"should be "
                                                        f"{b['to']}")

                                    ui.button(f"{b['to']} · {d}",
                                              on_click=face).props(
                                        "dense flat no-caps").classes(
                                        "text-xs")
                            ui.label("face a neighbour, then drag the "
                                     "panorama until that corridor sits on "
                                     "the dashed line").classes(
                                "text-xs text-gray-500")
                        else:
                            ui.label("drop another vertex on this level to "
                                     "have something to aim by").classes(
                                "text-xs text-[#f0a35e]")

                        with ui.row().classes("w-full items-center gap-1"):
                            for lbl, d in (("←5°", -5), ("←1°", -1)):
                                ui.button(lbl, on_click=lambda d=d:
                                          ui.run_javascript(
                                              "window.dwPanoNudge && "
                                              f"dwPanoNudge({d})")).props(
                                    "dense flat")
                            off_lb = ui.label("0.0°").classes(
                                "text-sm font-semibold tabular-nums")
                            for lbl, d in (("1°→", 1), ("5°→", 5)):
                                ui.button(lbl, on_click=lambda d=d:
                                          ui.run_javascript(
                                              "window.dwPanoNudge && "
                                              f"dwPanoNudge({d})")).props(
                                    "dense flat")
                            ui.space()

                            async def save_alignment():
                                off = float(await ui.run_javascript(
                                    "window.dwPanoOff ? dwPanoOff() : 0")
                                    or 0)
                                if min(off, 360 - off) < 0.05:
                                    ui.notify("no turn to save")
                                    return
                                px = await run.io_bound(apply_roll, name, off)
                                ui.notify(f"rolled {px} px into the file")
                                refresh_all()

                            ui.button("save alignment",
                                      on_click=save_alignment).props(
                                "dense").classes("bg-[#2c6e3f]")

                        first = brs[0] if brs else None
                        if first:
                            facing.set_text(f"should be {first['to']}")
                        ui.timer(0.15, lambda: ui.run_javascript(
                            f"dwPano({cv.id}, '{url}', {off_lb.id});" +
                            (f"dwPanoFace({first['bearing']})" if first
                             else "")), once=True)

                # -- splat: a placeholder until generation arrives --
                with ui.card().classes("w-full bg-[#11151c]"):
                    ui.label("splat").classes("font-bold")
                    ui.element("div").classes("w-full").style(
                        f"height:120px;border:2px dashed {C_WALL};"
                        "border-radius:6px;display:flex;align-items:center;"
                        "justify-content:center")
                    ui.label("no splat yet — generation will land here"
                             ).classes("text-xs text-gray-500")
            side()

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
            side.refresh()

    ui.timer(2.0, poll)


fastapi_app = FastAPI()
ui.run_with(fastapi_app, mount_path=MOUNT, title="dreamworld editor",
            dark=True, favicon="🌐")
