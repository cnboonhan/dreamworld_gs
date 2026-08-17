"""The page itself: toolbar, plan, and the two boxes beside it.

Clicking is mode-driven — drop a vertex, connect an edge, or select — and
the side panel follows the selection: rename and delete, the panorama box
(upload, then the ported align_panos viewer), and the splat placeholder.
A 2-second watcher refreshes every client when the map file or the
dreamworld tree changes under it.
"""

import math
import time
from pathlib import Path

from nicegui import run, ui

from config import C_BG, C_WALL, CURSOR, DREAM, MOUNT, PROJ, PROJECT
import crossing
import restyle
import splatgen
import store
from scene import level_scene

_JS = Path(__file__).parent / "js"

STATE_TIP = ("red — panorama missing or alignment never saved · "
             "yellow — in progress · green — every look (original and "
             "each variant) has its splat")


def _script(name: str) -> str:
    return f"<script>{(_JS / name).read_text()}</script>"


@ui.page("/")
def index():
    # inside the page builder, not at module level: head html added outside
    # a page lands on NiceGUI's auto-index client, and this page never sees it
    ui.add_head_html(_script("dw_view.js"))
    ui.add_head_html(_script("dw_pano.js"))
    ui.add_head_html(_script("dw_splat.js"))
    ui.add_head_html(_script("dw_walk.js"))
    sel = {"level": None, "vertex": None, "mode": None, "edge_from": None,
           "variant": None, "splat_var": None, "edge": None, "elook": {},
           "busy": False}

    def select_vertex(nm):
        if nm != sel["vertex"]:
            sel["variant"] = None       # the dropdowns belong to one vertex
            sel["splat_var"] = None
        sel["vertex"] = nm
        sel["edge"] = None              # a vertex and an edge never both

    with ui.header().classes("items-center bg-[#0b0e13] gap-6"):
        ui.label("dreamworld editor").classes("text-lg font-bold text-[#4ea1ff]")
        ui.label(PROJECT).classes("text-sm text-gray-500")

    def refresh_all():
        state["sig"] = store.signature()
        board.refresh()
        side.refresh()
        tools.refresh()     # the move button follows the selection

    # ---- toolbar ----
    names = list(store.load_levels())
    sel["level"] = names[0] if names else None
    with ui.row().classes("items-center px-4 gap-4"):
        picker = ui.select(names, value=sel["level"], label="level",
                           on_change=lambda e: (sel.update(level=e.value),
                                                board.refresh())
                           ).classes("w-40")

        HINT = {None: "click a vertex to select it",
                "add": "click the plan to drop a vertex",
                "edge": "click one vertex, then the other",
                "move": "drag a vertex to its new spot"}

        @ui.refreshable
        def tools():
            def switch(mode):
                sel["mode"] = None if sel["mode"] == mode else mode
                sel["edge_from"] = None
                tools.refresh()
                board.refresh()

            for mode, text, icon in (("add", "vertex", "add_location_alt"),
                                     ("edge", "edge", "timeline"),
                                     ("move", "move", "open_with")):
                on = sel["mode"] == mode
                props = "dense no-caps" + ("" if on else " outline")
                if mode == "move" and not sel["vertex"]:
                    props += " disable"     # nothing selected, nothing to move
                ui.button(text, icon=icon,
                          on_click=lambda m=mode: switch(m)).props(props)
            ui.label(HINT[sel["mode"]]).classes("text-xs text-gray-600")
        tools()

    # ---- the plan ----
    def on_map_mouse(dream, shift):
        down = {}

        def nearest(x, y, r=15.0):
            best, bd = None, r
            for nm, v in dream.items():
                if v["level"] != sel["level"]:
                    continue
                d = math.hypot(v["x"] - x, v["y"] - y)
                if d < bd:
                    best, bd = nm, d
            return best

        def nearest_edge(x, y, r):
            best, bd = None, r
            for a, b in store.load_edges():
                va, vb = dream.get(a), dream.get(b)
                if not va or not vb or va["level"] != sel["level"]:
                    continue
                vx, vy = vb["x"] - va["x"], vb["y"] - va["y"]
                L2 = vx * vx + vy * vy
                u = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, (
                    (x - va["x"]) * vx + (y - va["y"]) * vy) / L2))
                d = math.hypot(x - va["x"] - u * vx, y - va["y"] - u * vy)
                if d < bd:
                    best, bd = (a, b), d
            return best

        async def handler(e):
            x, y = e.image_x - shift[0], e.image_y - shift[1]
            if e.type == "mousedown":
                down["at"] = (x, y)
                return
            if e.type != "mouseup" or "at" not in down:
                return
            # movement judged in SCREEN pixels: image units shrink with
            # zoom, so a fixed image-unit threshold read a small pan at
            # high zoom as a click — which deselected the vertex. The hit
            # radius converts the other way: markers hold a constant size
            # on screen, so the radius that hits them must too.
            level = (sel["level"] or "").replace("'", "")
            k = float(await ui.run_javascript(
                f"(window._dwvMem && window._dwvMem['{level}']) "
                f"? window._dwvMem['{level}'].k : 1") or 1)
            hit = max(12.0 / k, 3.0)
            moved = math.hypot(x - down["at"][0], y - down["at"][1]) * k
            grabbed = nearest(*down["at"], hit)
            down.clear()
            if sel["mode"] == "move":
                if grabbed and dream.get(grabbed, {}).get("lift"):
                    ui.notify("lift waypoints are placed by the building "
                              "map — move the lift in /sim_editor")
                    return
                if grabbed and moved > 6:
                    store.move_vertex(grabbed, x, y)
                    select_vertex(grabbed)
                    refresh_all()
                return
            if moved > 6:            # that was a pan, not a click
                return
            if sel["mode"] == "add":
                select_vertex(store.new_vertex(sel["level"], x, y))
                ui.notify(f"dropped {sel['vertex']}")
                sel["mode"] = None      # one drop per press of the button
                tools.refresh()
            elif sel["mode"] == "edge":
                near = nearest(x, y, hit)
                if near is None or near == sel["edge_from"]:
                    sel["edge_from"] = None
                elif sel["edge_from"] is None:
                    sel["edge_from"] = near
                    ui.notify(f"{near} — now click the other end")
                else:
                    ui.notify(store.add_edge(sel["edge_from"], near))
                    sel["edge_from"] = None
            else:
                near = nearest(x, y, hit)
                lift_arrow = None
                if not near:
                    # a lift's up/down arrows sit a marker-height off the
                    # diamond — the building's vertical edges, clickable
                    off = 18.0 / k
                    for nm, v in dream.items():
                        if v["level"] != sel["level"] or not v.get("lift"):
                            continue
                        for d, dy in (("up", -1), ("down", 1)):
                            if math.hypot(v["x"] - x,
                                          v["y"] + dy * off - y) < hit:
                                lift_arrow = (nm, d)
                if near:             # empty space keeps the selection
                    select_vertex(near)
                elif lift_arrow:
                    nm, d = lift_arrow
                    tgt = store.lift_neighbors(nm)[d]
                    if tgt:
                        e = (nm, tgt)
                        if e != sel["edge"] and e[::-1] != sel["edge"]:
                            sel["elook"] = {}
                        sel["edge"] = e
                        sel["vertex"] = None
                    else:
                        ui.notify(f"{nm}: no stop {d} from here")
                else:
                    e = nearest_edge(x, y, hit)
                    if e:            # vertices win; edges take the rest
                        if e != sel["edge"]:
                            sel["elook"] = {}   # looks belong to one edge
                        sel["edge"] = e
                        sel["vertex"] = None
            refresh_all()
        return handler

    # a splitter, so the plan and the side boxes trade width by dragging
    # the boundary between them
    with ui.splitter(value=70, limits=(30, 85)).classes(
            "w-full px-4") as split:

        with split.before, ui.column().classes("w-full pr-2"):
            @ui.refreshable
            def board():
                levels = store.load_levels()
                if not levels:
                    ui.label(f"no *.building.yaml under {PROJ}/maps — "
                             f"trace one in /sim_editor first").classes("m-4")
                    return
                if sel["level"] not in levels:
                    sel["level"] = next(iter(levels))
                dream = store.load_dream()
                svg, w, h, shift = level_scene(
                    levels[sel["level"]], dream, store.load_edges(),
                    sel["level"], sel["vertex"], sel["edge_from"],
                    sel["edge"])
                box = ui.element("div").classes("w-full").style(
                    f"height:78vh;overflow:hidden;touch-action:none;"
                    f"background:{C_BG};border-radius:8px;"
                    f"cursor:{CURSOR[sel['mode']]}")
                with box:
                    # hidden until dwView places it — otherwise every
                    # refresh flashes the unplaced drawing for a frame
                    ui.interactive_image(
                        size=(w, h), content=svg,
                        events=["mousedown", "mouseup"],
                        on_mouse=on_map_mouse(dream, shift),
                    ).style("visibility:hidden")
                ui.label("drag or scroll to pan · pinch or ctrl+scroll to "
                         "zoom · double-click to fit").classes(
                    "text-xs text-gray-600")
                level = sel["level"].replace("'", "")
                ui.timer(0.1, lambda: ui.run_javascript(
                    f"dwView({box.id}, {w}, {h}, '{level}')"), once=True)
            board()

        # its own scroll region: the side boxes scroll, the plan stays put
        with split.after, ui.column().classes("w-full pl-2 gap-4").style(
                "height:82vh;overflow-y:auto"):
            @ui.refreshable
            def side():
                dream = store.load_dream()
                edge = sel["edge"]
                if edge and all(n in dream for n in edge) \
                        and (set(edge) in [set(e)
                                           for e in store.load_edges()]
                             or store.is_lift_edge(*edge)):
                    edge_card(edge, dream, sel, refresh_all)
                    a, b = edge
                    la, lb = _elook(sel, a), _elook(sel, b)
                    edge_direction_card(a, b, dream, "ab", sel, refresh_all)
                    transition_box(a, la, b, lb, refresh_all)
                    edge_direction_card(b, a, dream, "ba", sel, refresh_all)
                    transition_box(b, lb, a, la, refresh_all)
                    return
                sel["edge"] = None
                name = sel["vertex"] if sel["vertex"] in dream else None
                if not name:
                    ui.label("nothing selected — click a vertex or an edge "
                             "on the plan").classes(
                        "text-sm text-gray-500 mt-2")
                    return
                v = dream[name]
                scale = (store.load_levels().get(v["level"]) or {}).get("scale")
                vertex_card(name, v, sel, refresh_all)
                pano_card(name, v, dream, scale, sel, refresh_all)
                variants_card(name, v, sel, refresh_all)
                splat_card(name, v, sel, refresh_all)
            side()

    def on_key(e):
        if e.key.escape and e.action.keydown and (sel["vertex"]
                                                  or sel["edge_from"]
                                                  or sel["edge"]):
            sel["vertex"] = None
            sel["edge_from"] = None
            sel["edge"] = None
            refresh_all()
    ui.keyboard(on_key=on_key)

    state = {"sig": store.signature()}

    def poll():
        sig = store.signature()
        if sig != state["sig"]:
            state["sig"] = sig
            fresh = list(store.load_levels())
            if fresh != picker.options:
                picker.options = fresh
                picker.update()
            board.refresh()
            side.refresh()

    ui.timer(2.0, poll)


# ---- the edge boxes -------------------------------------------------------------

def edge_card(edge, dream, sel, refresh_all):
    a, b = edge
    va, vb = dream[a], dream[b]
    lift_edge = store.is_lift_edge(a, b)
    scale = (store.load_levels().get(va["level"]) or {}).get("scale")
    d = math.hypot(vb["x"] - va["x"], vb["y"] - va["y"])
    dist = (f"{va['level']} ⇵ {vb['level']} · {va['lift']}" if lift_edge
            else f"{d * scale:.1f} m" if scale else f"{d:.0f} px")
    with ui.card().classes("w-full bg-[#11151c]"):
        ui.label("Edge").classes("font-bold")
        with ui.row().classes("w-full items-center gap-2"):
            for nm, v in ((a, va), (b, vb)):
                ui.html(f'<span style="width:10px;height:10px;'
                        f'border-radius:5px;display:inline-block;'
                        f'background:{store.state_color(v)}"></span>')
                ui.label(nm).classes("text-sm text-[#7aa2f7]")
                if nm == a:
                    ui.label("—").classes("text-gray-600")
            ui.space()
            ui.label(dist).classes("text-xs text-gray-500")

        def delete():
            with ui.dialog() as dlg, ui.card():
                ui.label(f"delete the edge {a} — {b}?")
                with ui.row():
                    def yes():
                        store.remove_edge(a, b)
                        sel["edge"] = None
                        dlg.close()
                        refresh_all()
                    ui.button("delete", color="negative", on_click=yes)
                    ui.button("keep", on_click=dlg.close)
            dlg.open()

        if lift_edge:
            ui.label("the building's own edge — it goes when the lift "
                     "does, in /sim_editor").classes(
                "text-xs text-[#d24dcf]")
        else:
            ui.button("delete", color="negative", on_click=delete).props(
                "dense flat")


def _elook(sel, nm):
    lk = sel["elook"].get(nm)
    return lk if lk in store.variants_of(nm) else None


def edge_direction_card(frm, to, dream, tag, sel, refresh_all):
    """One direction of the crossing: both panoramas faced along the walk's
    own bearing — standing at each end, looking the way you would travel —
    and the splat transition beneath, scaffolded from main's mid-corridor
    handover: out of the first world, crossfading into the second. Each end
    can wear any of its looks: the dropdowns pick the variant whose
    panorama AND world this card uses, shared by both directions. The walk
    is a nominal straight line along each spawn's forward axis until
    splat-to-building placement gives it the real corridor."""
    va, vb = dream[frm], dream[to]
    la, lb = _elook(sel, frm), _elook(sel, to)
    if va["level"] != vb["level"]:
        # a lift ride is vertical: both panoramas face east, paired, and
        # the crossing prompt carries the actual direction of travel
        bearing = 0.0
    else:
        # drawing pixels run y-down; bearings live in the y-up compass frame
        bearing = math.atan2(-(vb["y"] - va["y"]), vb["x"] - va["x"])
    with ui.card().classes("w-full bg-[#11151c]"):
        ui.label(f"{frm} → {to}").classes("font-bold")

        with ui.row().classes("w-full gap-2 flex-nowrap"):
            for nm, look in ((frm, la), (to, lb)):
                with ui.column().classes("grow min-w-0 gap-1"):
                    ui.label(f"at {nm}").classes(
                        "text-xs text-gray-500 truncate")
                    variants = store.variants_of(nm)
                    ui.select(["original"] + variants,
                              value=look or "original",
                              on_change=lambda e, nm=nm: (
                                  sel["elook"].update(
                                      {nm: None if e.value == "original"
                                       else e.value}),
                                  refresh_all())
                              ).classes("w-full").props(
                        "dense options-dense")
                    if store.pano_of(nm, look) is None:
                        ui.element("div").classes("w-full").style(
                            f"height:130px;border:2px dashed {C_WALL};"
                            "border-radius:6px")
                        continue
                    prev = store.preview_of(nm, look)
                    url = (f"{MOUNT}/files/{nm}/{prev.name}"
                           f"?t={int(prev.stat().st_mtime)}")
                    cv = ui.element("canvas").classes("w-full").style(
                        f"height:130px;display:block;cursor:grab;"
                        f"touch-action:none;background:{C_BG};"
                        "border-radius:6px")
                    ns = f"ep_{tag}_{nm}"
                    # grouped per direction card: the pair starts on the
                    # edge's bearing and pans together, apart from the
                    # vertex viewers' own shared heading
                    ui.timer(0.15, lambda cv=cv, url=url, ns=ns:
                             ui.run_javascript(
                                 f"dwPano({cv.id}, '{url}', -1, '{ns}', "
                                 f"{{free:true, group:'ep_{tag}'}});"
                                 f"dwp('{ns}','face',{bearing})"),
                             once=True)
        ui.label("both panoramas face the direction of travel and pan "
                 "together — aligned ends show the same corridor from its "
                 "two ends").classes("text-xs text-gray-500")

        pa, pb = store.splat_of(frm, la), store.splat_of(to, lb)
        if not (pa and pb):
            missing = ", ".join(f"{n} ({lk or 'original'})"
                                for n, lk, p in ((frm, la, pa), (to, lb, pb))
                                if not p)
            ui.label(f"splat walkthrough needs a splat at both ends — "
                     f"missing: {missing}").classes("text-xs text-[#f0a35e]")
            return
        with ui.element("div").classes("w-full relative").style(
                "height:260px"):
            cvs = [ui.element("canvas").classes(
                "absolute inset-0 w-full h-full").style(
                f"background:{C_BG};border-radius:6px;pointer-events:none")
                for _ in (frm, to)]
        ua = f"{MOUNT}/files/{frm}/{store.splat_dir(frm, la).name}"
        ub = f"{MOUNT}/files/{to}/{store.splat_dir(to, lb).name}"
        ta, tb = int(pa.stat().st_mtime), int(pb.stat().st_mtime)
        # each world's camera stands at its capture point facing the edge's
        # building bearing — both ends of the walk look the same way, which
        # is what makes the crossing line up. Spawn cam is the fallback for
        # worlds generated before position meta was kept.
        cam_a = store.edge_view(frm, la, bearing) \
            or f"'{ua}/world.cam.json?t={ta}'"
        cam_b = store.edge_view(to, lb, bearing) \
            or f"'{ub}/world.cam.json?t={tb}'"

        async def boot():
            ra = await run.io_bound(store.splat_records, frm, la)
            rb = await run.io_bound(store.splat_records, to, lb)
            if not (ra and rb):
                return
            ui.run_javascript(
                f"dwSplat({cvs[0].id}, "
                f"'{ua}/{ra.name}?t={int(ra.stat().st_mtime)}', "
                f"{cam_a}, 'ws_{tag}_a');"
                f"dwSplat({cvs[1].id}, "
                f"'{ub}/{rb.name}?t={int(rb.stat().st_mtime)}', "
                f"{cam_b}, 'ws_{tag}_b');"
                f"dwWalkInit('{tag}', {cvs[0].id}, {cvs[1].id}, "
                f"'ws_{tag}_a', 'ws_{tag}_b', 4.0)")
        ui.timer(0.15, boot, once=True)
        with ui.row().classes("w-full items-center gap-2"):
            ui.button(icon="play_arrow", on_click=lambda: ui.run_javascript(
                f"window.dwWalk && dwWalk('{tag}','play',6)")).props(
                "dense flat")
            slider = ui.slider(min=0.0, max=1.0, step=0.01, value=0.0
                               ).classes("grow")
            slider.on("update:model-value",
                      lambda e: ui.run_javascript(
                          f"window.dwWalk && dwWalk('{tag}','t',{e.args})"),
                      throttle=0.05)


def transition_box(frm, la, to, lb, refresh_all):
    """The generated crossing, its own box below the walkthrough card: a
    Wan 2.2 first+last-frame video whose endpoints are the two aligned
    panoramas faced along the edge — the seconds reality hides from both
    photographs. The prompt defaults by what the map says the walk passes
    through (a door, a lift, open corridor), is editable, and is saved
    with the job."""
    vid = crossing.output(frm, la, to, lb)
    with ui.card().classes("w-full bg-[#11151c]"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.label("Video Transition").classes("font-bold")
            ui.label(f"{frm} → {to}").classes("text-xs text-gray-500")
        transition_body(frm, la, to, lb, refresh_all, vid)


def transition_body(frm, la, to, lb, refresh_all, vid):
    if vid:
        ui.html(f'<video src="{MOUNT}/files/.crossings/{vid.parent.name}/'
                f'crossing.mp4?t={int(vid.stat().st_mtime)}" controls loop '
                f'style="width:100%;border-radius:6px;background:{C_BG}">'
                f'</video>')

    pbox = ui.textarea("transition prompt",
                       value=crossing.saved_prompt(frm, la, to, lb)
                       or crossing.default_prompt(frm, to)).classes(
        "w-full").props("dense autogrow")

    wstat = crossing.status() or {}
    wq = [x.rstrip("/") for x in wstat.get("queue") or []]
    wmine = str(crossing.job_dir(frm, la, to, lb)).rstrip("/")
    wrun = (wstat.get("scene") or "").rstrip("/")

    if wstat.get("busy") and wrun == wmine:
        ui.linear_progress(show_value=False).props(
            "indeterminate").classes("w-full")
        note = "" if wstat.get("loaded") else \
            " (first job loads the model, ~2 minutes)"
        el_lb = ui.label(f"generating{note}").classes(
            "text-xs text-gray-500")

        def tick():
            s = crossing.status() or {}
            if not s.get("busy") or (s.get("scene") or "").rstrip("/") \
                    != wmine:
                refresh_all()
                return
            el_lb.set_text(f"generating — {s.get('elapsed', 0) // 60}m"
                           f"{s.get('elapsed', 0) % 60:02d}s{note}")
        ui.timer(5.0, tick)
        return

    if wmine in wq:
        ui.label(f"queued — position {wq.index(wmine) + 1} · generating "
                 f"now: {crossing.short(wrun)}" if wrun else
                 f"queued — position {wq.index(wmine) + 1}").classes(
            "text-xs text-[#f0a35e]")

        def tick_q():
            s = crossing.status() or {}
            if wmine not in [x.rstrip("/") for x in (s.get("queue") or [])]:
                refresh_all()
        ui.timer(5.0, tick_q)
        return

    done = wstat.get("done")
    if done and (done.get("scene") or "").rstrip("/") == wmine \
            and not done.get("ok"):
        ui.label(f"last generation failed: {done.get('error')}").classes(
            "text-xs text-[#f85149]")

    have_panos = bool(store.pano_of(frm, la) and store.pano_of(to, lb))
    with ui.row().classes("w-full items-center gap-2"):
        async def gen_video():
            prompt = (pbox.value or "").strip()
            if not prompt:
                ui.notify("say what the crossing should do",
                          type="negative")
                return
            if not crossing.ready():
                ui.notify("wangen is not ready — still starting, or not "
                          "running", type="negative")
                return
            try:
                doc = await run.io_bound(crossing.submit, frm, la, to, lb,
                                         prompt)
            except Exception as err:
                ui.notify(str(err), type="negative")
                return
            pos = doc.get("position", 1)
            ui.notify("crossing started — a few minutes" if pos <= 1
                      else f"queued at position {pos}")
            refresh_all()

        ui.button("regenerate transition" if vid else
                  "generate transition", color="primary",
                  on_click=gen_video).props(
            "dense" + ("" if have_panos else " disable"))
        if not have_panos:
            ui.label("both looks need panoramas first").classes(
                "text-xs text-gray-500")
        elif wstat.get("busy"):
            ui.label(f"will queue behind {crossing.short(wrun)}").classes(
                "text-xs text-gray-500")


# ---- the three vertex boxes ------------------------------------------------------

def vertex_card(name, v, sel, refresh_all):
    with ui.card().classes("w-full bg-[#11151c]"):
        ui.label("Vertex Name").classes("font-bold")
        with ui.row().classes("w-full items-center gap-2"):
            ui.html(f'<span style="width:12px;height:12px;border-radius:6px;'
                    f'display:inline-block;'
                    f'background:{store.state_color(v)}"></span>')
            with ui.label(name).classes("font-bold text-[#7aa2f7]"):
                ui.tooltip(STATE_TIP)
            ui.label(f"{v['level']} · ({v['x']:.0f}, {v['y']:.0f})"
                     ).classes("text-xs text-gray-500")
        if v.get("lift"):
            # a lift stop: place and name belong to the building map; the
            # same lift on another level is the same cabin
            ui.label(f"lift waypoint — {v['lift']}'s stop on {v['level']}. "
                     f"Named and placed by the building map; panoramas, "
                     f"variants, splats and edges work as anywhere."
                     ).classes("text-xs text-[#d24dcf]")
            return
        with ui.row().classes("w-full items-end gap-2"):
            field = ui.input("rename to", value=name).classes(
                "grow").props("dense")

            def rename():
                new = field.value.strip()
                if not new or new == name:
                    return
                if "/" in new or new.startswith("."):
                    ui.notify("not a usable name", type="negative")
                    return
                if (DREAM / new).exists():
                    ui.notify(f"{new} already exists", type="negative")
                    return
                store.rename_vertex(name, new)
                sel["vertex"] = new
                refresh_all()

            ui.button("rename", on_click=rename).props("dense")

            def delete():
                with ui.dialog() as dlg, ui.card():
                    ui.label(f"delete {name} and its edges?")
                    with ui.row():
                        def yes():
                            store.delete_vertex(name)
                            sel["vertex"] = None
                            if sel["mode"] == "move":
                                sel["mode"] = None   # nothing left to move
                            dlg.close()
                            refresh_all()
                        ui.button("delete", color="negative", on_click=yes)
                        ui.button("keep", on_click=dlg.close)
                dlg.open()

            ui.button("delete", color="negative",
                      on_click=delete).props("dense")


def pano_card(name, v, dream, scale, sel, refresh_all):
    with ui.card().classes("w-full bg-[#11151c]"):
        variants = store.variants_of(name)
        if sel["variant"] not in variants:
            sel["variant"] = None
        var = sel["variant"]
        with ui.row().classes("w-full items-center gap-2"):
            ui.label("panorama").classes("font-bold")
            applied = store.applied_of(name)
            if not v["pano"]:
                ui.label("none yet").classes("text-xs text-gray-500")
            elif applied is not None:
                ui.label(f"✓ {applied:.1f}° rolled in").classes(
                    "text-xs text-[#6c6]")
            else:
                ui.label("not yet aligned").classes("text-xs text-[#f0a35e]")
            ui.space()
            ui.label(var or "original").classes("text-xs text-[#7aa2f7]")

        if not v["pano"]:
            async def on_upload(e):
                suffix = Path(e.name).suffix.lower()
                if suffix not in (".jpg", ".jpeg", ".png"):
                    ui.notify("jpg or png only", type="negative")
                    return
                data = e.content.read()
                (DREAM / name / f"pano{suffix}").write_bytes(data)
                await run.io_bound(store.preview_of, name)
                ui.notify(f"panorama saved — {len(data) / 1e6:.1f} MB")
                refresh_all()

            ui.upload(on_upload=on_upload, auto_upload=True).props(
                'accept=".jpg,.jpeg,.png" flat').classes("w-full")
            ui.label("upload the equirectangular panorama shot at this "
                     "vertex").classes("text-xs text-gray-500")
            return

        p = store.preview_of(name, var)
        url = (f"{MOUNT}/files/{name}/{p.name}"
               f"?t={int(p.stat().st_mtime)}")
        brs = store.bearings_from(dream, name, scale)

        with ui.element("div").classes("w-full relative"):
            cv = ui.element("canvas").classes("w-full").style(
                f"height:300px;display:block;cursor:ew-resize;"
                f"touch-action:none;background:{C_BG};border-radius:6px")
            ui.html('<div style="position:absolute;left:50%;top:0;bottom:0;'
                    'border-left:2px dashed #4ea1ff;pointer-events:none">'
                    '</div>')
            facing = ui.label("").classes(
                "absolute top-2 left-1/2 ml-2 text-xs px-2 py-0.5 rounded "
                "z-10").style("background:#4ea1ff;color:#08121e")

        if brs:
            with ui.row().classes("w-full gap-1"):
                for b in brs:
                    d = f"{b['dist']:.1f}m" if scale else f"{b['dist']:.0f}px"

                    def face(b=b):
                        ui.run_javascript("window.dwp && "
                                          f"dwp('align','face',{b['bearing']})")
                        facing.set_text(f"should be {b['to']}")

                    ui.button(f"{b['to']} · {d}", on_click=face).props(
                        "dense flat no-caps").classes("text-xs")
            ui.label("face a neighbour, then drag the panorama until that "
                     "corridor sits on the dashed line").classes(
                "text-xs text-gray-500")
        else:
            ui.label("drop another vertex on this level to have something "
                     "to aim by").classes("text-xs text-[#f0a35e]")

        with ui.row().classes("w-full items-center gap-1"):
            for lbl, d in (("←5°", -5), ("←1°", -1)):
                ui.button(lbl, on_click=lambda d=d: ui.run_javascript(
                    f"window.dwp && dwp('align','nudge',{d})")).props(
                    "dense flat")
            off_lb = ui.label("0.0°").classes(
                "text-sm font-semibold tabular-nums")
            for lbl, d in (("1°→", 1), ("5°→", 5)):
                ui.button(lbl, on_click=lambda d=d: ui.run_javascript(
                    f"window.dwp && dwp('align','nudge',{d})")).props(
                    "dense flat")
            ui.space()

            async def save_alignment():
                off = float(await ui.run_javascript(
                    "window.dwp ? (dwp('align','off') || 0) : 0") or 0)
                if min(off, 360 - off) < 0.05 \
                        and store.applied_of(name) is not None:
                    ui.notify("no turn to save")
                    return
                px = await run.io_bound(store.apply_roll, name, off)
                ui.notify(f"rolled {px} px — original and variants together"
                          if px else "alignment confirmed as-is")
                refresh_all()

            ui.button("save alignment", on_click=save_alignment).props(
                "dense").classes("bg-[#2c6e3f]")

            with ui.button("reset", on_click=lambda: ui.run_javascript(
                    "window.dwp && dwp('align','reset')")).props(
                    "dense flat"):
                ui.tooltip("discard the unsaved turn — back to the last "
                           "saved alignment")

        first = brs[0] if brs else None
        if first:
            facing.set_text(f"should be {first['to']}")
        ui.timer(0.15, lambda: ui.run_javascript(
            f"dwPano({cv.id}, '{url}', {off_lb.id}, 'align', {{arrow:true}});"
            + (f"dwp('align','face',{first['bearing']})" if first else "")),
            once=True)


def variants_card(name, v, sel, refresh_all):
    """New looks for the same place, out of a prompt aimed with the viewer —
    main's perspective edit reborn on the dreamworld tree. The edit lands
    where the panorama box is facing. The original is the base truth: with
    it selected there is only new; a variant selected offers only edit and
    delete."""
    if not v["pano"]:
        return
    with ui.card().classes("w-full bg-[#11151c]"):
        variants = store.variants_of(name)
        with ui.row().classes("w-full items-center gap-2"):
            ui.label("variants").classes("font-bold")
            ui.space()
            ui.select(["original"] + variants,
                      value=sel["variant"] or "original", label="variant",
                      on_change=lambda e: (sel.update(
                          variant=None if e.value == "original"
                          else e.value), refresh_all())
                      ).classes("w-40").props("dense options-dense")
        if sel["variant"] is None:
            # the original: no prompt here — it is never edited. New mints
            # a variant as an exact copy and selects it, ready to edit.
            with ui.row().classes("w-full items-end gap-2"):
                name_box = ui.input("new variant name").classes(
                    "grow").props("dense")

                def new_variant():
                    target = (name_box.value or "").strip()
                    if not store.VARIANT_OK.fullmatch(target) \
                            or target == "original":
                        ui.notify("variant names: letters, digits, - or _",
                                  type="negative")
                        return
                    if target in store.variants_of(name):
                        ui.notify(f"{target} already exists",
                                  type="negative")
                        return
                    store.create_variant(name, target)
                    sel["variant"] = target
                    ui.notify(f"variant {target} created — a copy of the "
                              f"original, ready to edit")
                    refresh_all()

                ui.button("new", on_click=new_variant).props("dense")
            ui.label("new copies the original into a variant and selects "
                     "it · the original itself is never edited").classes(
                "text-xs text-gray-500")
            return

        # an edit in flight: the bar lives here, in the box, not a popup
        if sel["busy"]:
            ui.label(f"editing {sel['variant']} — the change will land "
                     f"inside the rectangle you set").classes(
                "text-xs text-gray-500")
            ui.linear_progress(show_value=False).props(
                "indeterminate").classes("w-full")
            elapsed = ui.label("0s elapsed").classes("text-xs text-gray-500")
            t0 = sel.get("busy_t0") or time.time()
            ui.timer(1.0, lambda: elapsed.set_text(
                f"{int(time.time() - t0)}s elapsed"))
            return

        # the variant's own viewer, on its own camera: aiming an edit here
        # never turns the alignment view above. Free-look — drag looks
        # around, pitch included; the rectangle IS the crop the edit gets.
        var = sel["variant"]
        p = store.preview_of(name, var)
        url = f"{MOUNT}/files/{name}/{p.name}?t={int(p.stat().st_mtime)}"
        with ui.element("div").classes("w-full relative"):
            cv = ui.element("canvas").classes("w-full").style(
                f"height:260px;display:block;cursor:grab;"
                f"touch-action:none;background:{C_BG};border-radius:6px")
        ui.timer(0.15, lambda: ui.run_javascript(
            f"dwPano({cv.id}, '{url}', -1, 'edit', "
            f"{{free:true, rect:true}})"), once=True)

        prompt_box = ui.textarea(
            "how to modify what this viewer is facing",
            placeholder="e.g. the door stands open, smoke along the "
                        "ceiling").classes("w-full").props("dense autogrow")

        async def edit_variant():
            var = sel["variant"]
            if sel["busy"]:
                ui.notify("an edit is already running")
                return
            prompt = (prompt_box.value or "").strip()
            if not prompt:
                ui.notify("say how to modify the panorama", type="negative")
                return
            if not restyle.ready():
                ui.notify("qwen is not ready — still loading, or not "
                          "running", type="negative")
                return
            view = await ui.run_javascript(
                "window.dwp ? dwp('edit','view') : null")
            if not view:
                ui.notify("the variant viewer is not up yet",
                          type="negative")
                return
            sel["busy"] = True
            sel["busy_t0"] = time.time()
            refresh_all()            # the box becomes the progress bar
            try:
                png, msg = await run.io_bound(
                    restyle.perspective_edit,
                    store.pano_of(name, var), prompt, view)
                if png is None:
                    ui.notify(f"edit failed: {msg}", type="negative")
                else:
                    store.save_variant(name, var, png)
                    ui.notify(f"variant {var} updated")
            except Exception as err:      # surfaced in the page, like main
                ui.notify(f"edit failed: {err}", type="negative")
            finally:
                sel["busy"] = False
            refresh_all()

        def delete_variant():
            var = sel["variant"]
            with ui.dialog() as dlg, ui.card():
                ui.label(f"delete variant {var} of {name}?")
                with ui.row():
                    def yes():
                        store.delete_variant(name, var)
                        sel["variant"] = None
                        dlg.close()
                        refresh_all()
                    ui.button("delete", color="negative", on_click=yes)
                    ui.button("keep", on_click=dlg.close)
            dlg.open()

        def undo():
            ok = store.undo_variant(name, sel["variant"])
            ui.notify("restored the previous look — undo again to swap back"
                      if ok else "nothing to undo")
            refresh_all()

        with ui.row().classes("w-full items-center"):
            ui.button("undo", on_click=undo).props(
                "dense flat" + ("" if store.has_undo(name, var)
                                else " disable"))
            ui.space()
            ui.button("edit", color="primary",
                      on_click=edit_variant).props("dense")
            ui.space()
            ui.button("delete", color="negative",
                      on_click=delete_variant).props("dense")
        ui.label("drag to look, scroll to zoom, place the rectangle over "
                 "the spot to change — the edit lands inside it, in place; "
                 "alignment belongs to the vertex and never moves here"
                 ).classes("text-xs text-gray-500")


def splat_card(name, v, sel, refresh_all):
    """The same layout as the variants box, for worlds: pick a look, view
    its splat if one is built, or send it to the generator — HY-World's six
    stages on four cards, about seventeen minutes a world, one at a time."""
    with ui.card().classes("w-full bg-[#11151c]"):
        variants = store.variants_of(name)
        if sel["splat_var"] not in variants:
            sel["splat_var"] = None
        svar = sel["splat_var"]
        scene = store.splat_dir(name, svar)
        ply = store.splat_of(name, svar)

        with ui.row().classes("w-full items-center gap-2"):
            ui.label("splat").classes("font-bold")
            if ply:
                ui.label("ready").classes("text-xs text-[#6c6]")
            else:
                ui.label("none yet").classes("text-xs text-gray-500")
            ui.space()
            ui.select(["original"] + variants, value=svar or "original",
                      label="variant",
                      on_change=lambda e: (sel.update(
                          splat_var=None if e.value == "original"
                          else e.value), refresh_all())
                      ).classes("w-40").props("dense options-dense")

        gen = splatgen.status() or {}
        queue = [q.rstrip("/") for q in (gen.get("queue") or [])]
        mine = str(scene).rstrip("/")
        running = (gen.get("scene") or "").rstrip("/")
        busy_here = bool(gen.get("busy") and running == mine)
        queued_here = mine in queue

        if ply:
            # the world itself: main's renderer, minimal. Drag looks,
            # wheel walks, shift-drag pans, main's keys after a click.
            # Served as world.splat — main's 32-byte records, 40% lighter
            # than the ply — built off the event loop on first ask.
            base = f"{MOUNT}/files/{name}/{scene.name}"
            t = int(ply.stat().st_mtime)
            with ui.element("div").classes("w-full relative"):
                cv = ui.element("canvas").classes("w-full").style(
                    f"height:320px;display:block;cursor:grab;"
                    f"touch-action:none;background:{C_BG};border-radius:6px")

                def open_big():
                    rec = store.splat_records(name, svar)   # cached by boot
                    u = (f"{base}/{rec.name}"
                         f"?t={int(rec.stat().st_mtime)}")
                    with ui.dialog().props("maximized") as dlg:
                        with ui.element("div").classes(
                                "w-full h-full relative").style(
                                f"background:{C_BG}"):
                            big = ui.element("canvas").classes(
                                "w-full h-full").style(
                                "display:block;cursor:grab;"
                                "touch-action:none")
                            ui.button(icon="close", on_click=dlg.close
                                      ).props("flat round dense").classes(
                                "absolute top-2 right-2 z-10")
                            with ui.button(
                                    icon="my_location",
                                    on_click=lambda: ui.run_javascript(
                                        "window._dws && "
                                        "window._dws.vsplat_big && "
                                        "window._dws.vsplat_big.reset()")
                                    ).props("flat round dense").classes(
                                    "absolute top-2 right-12 z-10"):
                                ui.tooltip("back to the spawn camera")
                            ui.timer(0.2, lambda: ui.run_javascript(
                                f"dwSplat({big.id}, '{u}', "
                                f"'{base}/world.cam.json?t={t}', "
                                f"'vsplat_big')"), once=True)
                    dlg.open()

                ui.button(icon="fullscreen", on_click=open_big).props(
                    "flat round dense").classes(
                    "absolute top-1 right-1 z-10")
                with ui.button(icon="my_location",
                               on_click=lambda: ui.run_javascript(
                                   "window._dws && window._dws.vsplat && "
                                   "window._dws.vsplat.reset()")).props(
                        "flat round dense").classes(
                        "absolute top-1 right-9 z-10"):
                    ui.tooltip("back to the spawn camera")
            ui.label("drag to look · wheel to walk · shift-drag to pan · "
                     "after a click: A/D turn, W/S tilt, Q/E roll, "
                     "arrows move").classes("text-xs text-gray-500")

            async def boot(svar=svar):
                rec = await run.io_bound(store.splat_records, name, svar)
                if rec is None:
                    return
                ui.run_javascript(
                    f"dwSplat({cv.id}, "
                    f"'{base}/{rec.name}?t={int(rec.stat().st_mtime)}', "
                    f"'{base}/world.cam.json?t={t}', 'vsplat')")
            ui.timer(0.15, boot, once=True)

        if busy_here:
            ui.label(f"generating {svar or 'original'} …").classes(
                "text-xs text-gray-500")
            ui.linear_progress(show_value=False).props(
                "indeterminate").classes("w-full")
            stage_lb = ui.label(gen.get("stage") or "starting").classes(
                "text-xs text-gray-500")
            el_lb = ui.label("").classes("text-xs text-gray-500")

            def tick():
                s = splatgen.status()
                if not s or not s.get("busy") \
                        or s.get("scene") != str(scene):
                    refresh_all()
                    return
                stage_lb.set_text(s.get("stage") or "starting")
                el_lb.set_text(f"{s.get('elapsed', 0) // 60}m"
                               f"{s.get('elapsed', 0) % 60:02d}s elapsed")
            ui.timer(5.0, tick)
            return

        if queued_here:
            # waiting its turn: say so, and say whose turn it is
            pos = queue.index(mine) + 1
            ui.label(f"queued — position {pos} of {len(queue)}").classes(
                "text-xs text-[#f0a35e]")
            if running:
                ui.label(f"generating now: {splatgen.short(running)} "
                         f"({gen.get('stage') or 'starting'})").classes(
                    "text-xs text-gray-500")

            def tick_q():
                s = splatgen.status() or {}
                if mine not in [q.rstrip("/")
                                for q in (s.get("queue") or [])]:
                    refresh_all()      # our turn came, or the queue died
            ui.timer(5.0, tick_q)
            return

        has_pano = store.pano_of(name, svar) is not None
        done = gen.get("done")
        if done and done.get("scene") == str(scene) and not done.get("ok"):
            ui.label(f"last generation failed: {done.get('error')}").classes(
                "text-xs text-[#f85149]")

        with ui.row().classes("w-full items-center gap-2"):
            async def generate():
                if not splatgen.ready():
                    ui.notify("the generator is not ready — still loading, "
                              "or not running", type="negative")
                    return
                try:
                    doc = await run.io_bound(splatgen.submit, name, svar)
                except Exception as err:
                    ui.notify(str(err), type="negative")
                    return
                pos = doc.get("position", 1)
                ui.notify("generation started — six stages, roughly "
                          "fifteen to twenty minutes" if pos <= 1 else
                          f"queued at position {pos}")
                refresh_all()

            busy_other = bool(gen.get("busy"))
            props = "dense" + ("" if has_pano else " disable")
            ui.button("regenerate" if ply else "generate splat",
                      color="primary", on_click=generate).props(props)
            if not has_pano:
                ui.label("this look has no panorama yet").classes(
                    "text-xs text-gray-500")
            elif busy_other:
                ui.label(f"will queue behind "
                         f"{splatgen.short(running)}").classes(
                    "text-xs text-gray-500")

        if busy_other:
            # a status() hiccup or a job submitted elsewhere can leave this
            # card idle while ITS OWN world starts — watch, and become the
            # progress bar the moment that happens
            def tick_i():
                s = splatgen.status() or {}
                now = (s.get("scene") or "").rstrip("/")
                if now == mine or not s.get("busy"):
                    refresh_all()
            ui.timer(5.0, tick_i)
