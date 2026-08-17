"""The dreamworld tree and the map it grows against.

Everything the flow produces lives under the project's dreamworld/ tree —
one folder per vertex (vertex.json, pano.jpg, aligned.json, splat later)
plus edges.json — and this module is that tree's only writer.

building.yaml is shared with the traffic editor along a clean seam: its
structure — walls, doors, floors, lifts, measurements, every unnamed
vertex — is the traffic editor's and is only ever read here; the NAV
layer — named vertices and graph-0 lanes — is authored here and written
back by sync_nav() after every vertex or edge mutation, so RMF's tools
read the same graph this page draws.
"""

import json
import math
import re
import shutil
from pathlib import Path

import numpy as np
import yaml
from PIL import Image

from config import C_GRN, C_RED, C_YEL, DREAM, PREVIEW_W, PROJ

Image.MAX_IMAGE_PIXELS = None

EDGES = DREAM / "edges.json"


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
                      if len(v) > 3 and isinstance(v[3], str) and v[3]
                      and not (len(v) > 4 and isinstance(v[4], dict)
                               and "lift_cabin" in v[4])],
            # lift cabin waypoints: traffic editor writes one per lift per
            # level, unnamed, identified by the lift_cabin param. The SAME
            # lift name on two levels is the same cabin — the building's
            # vertical edges, carried by the map rather than edges.json
            "lifts": [(v[4]["lift_cabin"][1], v[0], v[1]) for v in V
                      if len(v) > 4 and isinstance(v[4], dict)
                      and "lift_cabin" in v[4]],
            "scale": scale,
        }
    return levels


# ---- vertices ----------------------------------------------------------------

# main's convention, kept: the unsuffixed file is the original, a variant is
# pano@<name> beside it — a variant is a look, not a new place
VARIANT_OK = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _stem(variant: str | None) -> str:
    return f"pano@{variant}" if variant else "pano"


def pano_of(name: str, variant: str | None = None):
    d = DREAM / name
    for ext in (".jpg", ".jpeg", ".png"):
        if (d / f"{_stem(variant)}{ext}").is_file():
            return d / f"{_stem(variant)}{ext}"
    return None


def variants_of(name: str) -> list:
    d = DREAM / name
    if not d.is_dir():
        return []
    out = set()
    for f in d.glob("pano@*"):
        stem = f.stem.split("@", 1)[1]
        # valid names carry no dot, so a dotted stem is machinery:
        # .preview, .undo
        if "." not in stem:
            out.add(stem)
    return sorted(out)


def create_variant(name: str, variant: str) -> None:
    """A new variant IS the original, copied. It shares the vertex's one
    alignment — a variant is a look, not a place, so there is nothing of
    its own to record. It only becomes a different look when edited."""
    src = pano_of(name)
    shutil.copy2(src, DREAM / name / f"{_stem(variant)}{src.suffix}")
    preview_of(name, variant)


def save_variant(name: str, variant: str, png: bytes) -> None:
    """An edit replaces the variant's pixels and stashes the previous image
    as the undo step. Orientation is untouched: the edit was built on the
    variant's rolled pixels, and the vertex's one alignment record already
    describes them."""
    d = DREAM / name
    prev = pano_of(name, variant)
    if prev:
        for old in d.glob(f"{_stem(variant)}.undo.*"):
            old.unlink()
        prev.rename(d / f"{_stem(variant)}.undo{prev.suffix}")
    delete_variant(name, variant, keep_undo=True)
    (d / f"{_stem(variant)}.png").write_bytes(png)
    preview_of(name, variant)


def has_undo(name: str, variant: str) -> bool:
    return any((DREAM / name).glob(f"{_stem(variant)}.undo.*"))


def undo_variant(name: str, variant: str) -> bool:
    """Swap the variant with its pre-edit self — so undo twice is redo.
    Nothing of alignment moves: rolls turn the undo stash along with
    everything else, so both sides of the swap share the vertex's frame."""
    d = DREAM / name
    und = next(iter(d.glob(f"{_stem(variant)}.undo.*")), None)
    if und is None:
        return False
    cur = pano_of(name, variant)
    if cur:
        cur.rename(d / f"{_stem(variant)}.swap{cur.suffix}")
    und.rename(d / f"{_stem(variant)}{und.suffix}")
    for s in d.glob(f"{_stem(variant)}.swap.*"):
        s.rename(d / f"{_stem(variant)}.undo{s.suffix}")
    (d / f"{_stem(variant)}.preview.jpg").unlink(missing_ok=True)
    preview_of(name, variant)
    return True


def delete_variant(name: str, variant: str, keep_undo: bool = False) -> None:
    d = DREAM / name
    for ext in (".jpg", ".jpeg", ".png"):
        (d / f"{_stem(variant)}{ext}").unlink(missing_ok=True)
    (d / f"{_stem(variant)}.preview.jpg").unlink(missing_ok=True)
    (d / f"aligned@{variant}.json").unlink(missing_ok=True)
    if not keep_undo:
        for f in d.glob(f"{_stem(variant)}.undo.*"):
            f.unlink()
        (d / f"aligned@{variant}.undo.json").unlink(missing_ok=True)


def materialize_lifts() -> None:
    """Every lift cabin stop becomes a dream vertex named <level>.<lift>,
    so a lift stop can carry panoramas, variants, splats and edges like any
    other place. Its position and identity stay the building map's: the
    folder is (re)written from building.yaml whenever they differ, and the
    UI refuses to move, rename or delete it. The same lift's stops on
    different levels are the same cabin — the vertical edges routing will
    ride."""
    for lvl, L in load_levels().items():
        for lift, x, y in L.get("lifts") or []:
            name = f"{lvl}.{lift}"
            vj = DREAM / name / "vertex.json"
            want = {"level": lvl, "x": round(float(x), 2),
                    "y": round(float(y), 2), "lift": lift}
            try:
                if vj.is_file() and json.loads(vj.read_text()) == want:
                    continue
            except (OSError, ValueError):
                pass
            vj.parent.mkdir(parents=True, exist_ok=True)
            vj.write_text(json.dumps(want, indent=1))


def load_dream() -> dict:
    materialize_lifts()
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
        looks = [None] + variants_of(d.name)
        out[d.name] = {"level": v.get("level", ""), "x": float(v["x"]),
                       "y": float(v["y"]), "pano": pano_of(d.name) is not None,
                       "applied": applied, "lift": v.get("lift"),
                       "splat": splat_of(d.name) is not None,
                       "all_splats": all(splat_of(d.name, lk) is not None
                                         for lk in looks)}
    return out


def splat_dir(name: str, variant: str | None = None) -> Path:
    return DREAM / name / (f"splat@{variant}" if variant else "splat")


def splat_of(name: str, variant: str | None = None):
    """The built world, or None — world.ply is the honest readiness test,
    since a running generation fills the directory long before it."""
    p = splat_dir(name, variant) / "world.ply"
    return p if p.is_file() else None


def edge_view(name: str, variant: str | None, bearing: float):
    """A camera standing at this world's capture point, facing a BUILDING
    bearing — the walkthrough camera for an edge, without marks.

    Ported from main's make_spawn_cam and leaning on what v2 knows that
    main didn't use: the panorama was aligned to the building BEFORE
    generation, so HY-World's facing_direction (the pano centre, in ply
    coordinates) is building east, its up_direction is building up, and
    center_point is the capture position. A bearing then rotates east
    toward north (up cross east) in the ply's own frame. The camera rows
    are main's exactly — [right, fwd x right, fwd], the y-down frame the
    viewer projects with. Scale stays unknown (main measured a 4.5x
    spread), so walk distances remain nominal; direction is what this
    fixes."""
    meta_p = splat_dir(name, variant) / "gs_result" / "ply" \
        / "position_meta_info.json"
    if not meta_p.is_file():
        return None
    meta = json.loads(meta_p.read_text())

    def unit(v):
        v = np.asarray(v, dtype=np.float64)
        return v / max(np.linalg.norm(v), 1e-9)

    up = unit(meta["up_direction"])
    east = unit(meta["facing_direction"])
    north = unit(np.cross(up, east))
    fwd = math.cos(bearing) * east + math.sin(bearing) * north
    eye = np.asarray(meta["center_point"], dtype=np.float64)
    right = unit(np.cross(fwd, up))
    R = np.stack([right, np.cross(fwd, right), fwd])
    m = np.eye(4)
    m[:3, :3] = R
    m[:3, 3] = -R @ eye
    return [round(float(v), 6) for v in m.T.flatten()]


def splat_meta(name: str, variant: str | None = None):
    """The world's own compass, for the viewer: building east and up in ply
    coordinates, and the capture point — what edge_view() is built from."""
    p = splat_dir(name, variant) / "gs_result" / "ply" \
        / "position_meta_info.json"
    if not p.is_file():
        return None
    m = json.loads(p.read_text())
    return {"east": m["facing_direction"], "up": m["up_direction"],
            "center": m["center_point"]}


def graph_doc() -> dict:
    """Everything the walkthrough viewer needs, in one JSON: the levels'
    walls and scale, every vertex with its looks and their built worlds
    (each with its compass), the edges, and which crossings have videos.
    Building world.splat here keeps the viewer's first fetch from paying
    the conversion."""
    doc = {"levels": {}, "vertices": {}, "edges": load_edges(),
           "crossings": []}
    for lname, L in load_levels().items():
        doc["levels"][lname] = {"walls": L["walls"], "scale": L["scale"]}
    for name, v in load_dream().items():
        looks = {}
        for lk in [None] + variants_of(name):
            if splat_of(name, lk) is None:
                continue
            meta = splat_meta(name, lk)
            rec = splat_records(name, lk)
            looks[lk or "original"] = {
                "dir": splat_dir(name, lk).name,
                "records": rec.name if rec else None,
                "meta": meta,
            }
        doc["vertices"][name] = {
            "level": v["level"], "x": v["x"], "y": v["y"],
            "lift": v.get("lift"), "looks": looks,
        }
    cross = DREAM / ".crossings"
    if cross.is_dir():
        doc["crossings"] = sorted(
            d.name for d in cross.iterdir()
            if (d / "crossing.mp4").is_file())
    return doc


def splat_records(name: str, variant: str | None = None):
    """world.splat beside world.ply: the viewer's 32-byte records (main's
    format — position, scale, rgba, quaternion), importance-sorted, ~40%
    smaller than the ply and free of client-side parsing. Built on first
    ask, rebuilt when the ply is newer."""
    ply = splat_of(name, variant)
    if ply is None:
        return None
    out = ply.with_name("world.splat")
    if out.is_file() and out.stat().st_mtime >= ply.stat().st_mtime:
        return out
    raw = ply.read_bytes()
    end = raw.index(b"end_header\n") + len(b"end_header\n")
    header = raw[:end].decode("ascii", "ignore")
    n = int(re.search(r"element vertex (\d+)", header).group(1))
    props = re.findall(r"property (\w+) (\w+)", header)
    if any(ty != "float" for ty, _ in props):
        raise ValueError("unexpected non-float property in world.ply")
    v = np.frombuffer(raw, dtype=np.dtype([(nm, "<f4") for _, nm in props]),
                      count=n, offset=end)
    SH_C0 = 0.28209479177387814
    scale = np.exp(np.stack([v["scale_0"], v["scale_1"], v["scale_2"]], 1))
    opacity = 1.0 / (1.0 + np.exp(-v["opacity"]))
    order = np.argsort(-(scale.prod(axis=1) * opacity))
    rgb = np.stack([v["f_dc_0"], v["f_dc_1"], v["f_dc_2"]], 1)[order]
    rot = np.stack([v["rot_0"], v["rot_1"], v["rot_2"], v["rot_3"]], 1)[order]
    rot /= np.maximum(np.linalg.norm(rot, axis=1, keepdims=True), 1e-9)
    rec = np.empty(n, np.dtype([("p", "<f4", 3), ("s", "<f4", 3),
                                ("c", "u1", 4), ("q", "u1", 4)]))
    rec["p"] = np.stack([v["x"], v["y"], v["z"]], 1)[order]
    rec["s"] = scale[order]
    rec["c"][:, :3] = np.clip(np.rint((0.5 + SH_C0 * rgb) * 255), 0, 255)
    rec["c"][:, 3] = np.clip(np.rint(opacity[order] * 255), 0, 255)
    rec["q"] = np.clip(np.rint(rot * 128 + 128), 0, 255)
    out.write_bytes(rec.tobytes())
    return out


def state_color(v: dict) -> str:
    """Red until the panorama is ALIGNED — an unsaved alignment is not an
    alignment — green once every look, the original and each variant, has
    its splat built; yellow for the work in between."""
    if not v["pano"] or v["applied"] is None:
        return C_RED
    if v["all_splats"]:
        return C_GRN
    return C_YEL


def new_vertex(level: str, x: float, y: float) -> str:
    # one past the highest, never the smallest gap: a number freed by a
    # deletion stays retired, so names keep meaning what they meant
    used = [int(m.group(1)) for name in load_dream()
            if (m := re.fullmatch(re.escape(level) + r"\.v(\d+)", name))]
    name = f"{level}.v{max(used) + 1 if used else 0}"
    d = DREAM / name
    d.mkdir(parents=True)
    (d / "vertex.json").write_text(json.dumps(
        {"level": level, "x": round(x, 2), "y": round(y, 2)}, indent=1))
    sync_nav()
    return name


def move_vertex(name: str, x: float, y: float) -> None:
    vj = DREAM / name / "vertex.json"
    v = json.loads(vj.read_text())
    v["x"], v["y"] = round(x, 2), round(y, 2)
    vj.write_text(json.dumps(v, indent=1))
    sync_nav()


def rename_vertex(old: str, new: str) -> None:
    (DREAM / old).rename(DREAM / new)
    rename_in_edges(old, new)
    sync_nav()


def delete_vertex(name: str) -> None:
    shutil.rmtree(DREAM / name)
    drop_from_edges(name)
    sync_nav()


# ---- edges, kept in one file beside the vertex folders ----------------------

def load_edges() -> list:
    try:
        return [tuple(e) for e in json.loads(EDGES.read_text())]
    except (OSError, ValueError):
        return []


def save_edges(edges: list) -> None:
    EDGES.write_text(json.dumps([list(e) for e in edges], indent=1))


def add_edge(a: str, b: str) -> str:
    edges = load_edges()
    if (a, b) in edges or (b, a) in edges:
        return f"{a} and {b} are already connected"
    edges.append((a, b))
    save_edges(edges)
    sync_nav()
    return f"connected {a} — {b}"


def rename_in_edges(old: str, new: str) -> None:
    save_edges([(new if a == old else a, new if b == old else b)
                for a, b in load_edges()])


def drop_from_edges(name: str) -> None:
    save_edges([(a, b) for a, b in load_edges() if name not in (a, b)])


def remove_edge(a: str, b: str) -> None:
    save_edges([e for e in load_edges() if set(e) != {a, b}])
    sync_nav()


def _segments_cross(ax, ay, bx, by, cx, cy, dx, dy) -> bool:
    def o(px, py, qx, qy, rx, ry):
        v = (qx - px) * (ry - py) - (qy - py) * (rx - px)
        return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)
    return (o(ax, ay, bx, by, cx, cy) != o(ax, ay, bx, by, dx, dy)
            and o(cx, cy, dx, dy, ax, ay) != o(cx, cy, dx, dy, bx, by))


def edge_kind(frm: str, to: str) -> str:
    """What a walk from frm to to passes through, read from the map: into
    or out of a lift cabin, across a door segment, or nothing at all —
    the difference between crossing prompts."""
    dream = load_dream()
    if dream.get(to, {}).get("lift"):
        return "lift_in"
    if dream.get(frm, {}).get("lift"):
        return "lift_out"
    a, b = dream.get(frm), dream.get(to)
    if not (a and b):
        return "open"
    lv = load_levels().get(a["level"]) or {}
    for x1, y1, x2, y2 in lv.get("doors") or []:
        if _segments_cross(a["x"], a["y"], b["x"], b["y"], x1, y1, x2, y2):
            return "door"
    return "open"


def bearings_from(dream: dict, name: str, scale) -> list:
    """Where to aim the panorama: the vertices this one is connected to, as
    compass bearings — main aimed by lanes, and an edge is this tree's lane.
    Before any edges exist here, every vertex on the level is a landmark."""
    me = dream[name]
    linked = {b if a == name else a
              for a, b in load_edges() if name in (a, b)}
    out = []
    for other, v in dream.items():
        if other == name or v["level"] != me["level"]:
            continue
        if linked and other not in linked:
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


# ---- the panorama itself ------------------------------------------------------
# Alignment belongs to the VERTEX, not to a file: one record, and a roll
# turns the original, every variant and every undo stash together, so the
# offsets can never drift apart.

def _aligned_rec(name: str) -> Path:
    return DREAM / name / "aligned.json"


def applied_of(name: str):
    rec = _aligned_rec(name)
    if rec.is_file():
        try:
            return float(json.loads(rec.read_text())["degrees"])
        except (OSError, ValueError, KeyError):
            pass
    return None


def preview_of(name: str, variant: str | None = None) -> Path:
    """The downscaled copy the browser gets, made on first ask — main's
    pattern, sized to main's width."""
    src = pano_of(name, variant)
    p = DREAM / name / f"{_stem(variant)}.preview.jpg"
    if src and (not p.is_file() or p.stat().st_mtime < src.stat().st_mtime):
        Image.open(src).convert("RGB").resize(
            (PREVIEW_W, PREVIEW_W // 2), Image.LANCZOS).save(p, quality=88)
    return p


def apply_roll(name: str, degrees: float) -> int:
    """Roll the PLACE by `degrees`: the original, every variant, and every
    undo stash turn together, each by its own pixel count for the same
    angle, and one record keeps the running total for them all.

    The shift is NEGATIVE, and that sign is the whole bug main already paid
    for: the preview shows content at longitude L appearing at L + corr,
    while np.roll by +shift moves it from L to L - delta. Opposite signs —
    save with the wrong one and a panorama turned until it looked right
    comes back with the corridor at twice the angle on the wrong side.

    Zero degrees is a legal save: it rolls nothing but writes the record —
    how a panorama that was shot already facing right gets MARKED aligned,
    which the vertex's color rides on.
    """
    d = DREAM / name
    if abs(degrees) < 0.05 or abs(degrees - 360) < 0.05:
        old = applied_of(name) or 0.0
        _aligned_rec(name).write_text(
            json.dumps({"degrees": round(old, 2), "last": 0.0}, indent=1))
        return 0
    rolled = 0
    for f in sorted(d.iterdir()):
        if not f.name.startswith("pano") or ".preview" in f.name \
                or f.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        im = np.asarray(Image.open(f).convert("RGB"))
        shift = -int(round(degrees / 360.0 * im.shape[1]))
        Image.fromarray(np.roll(im, shift, axis=1)).save(
            f, **({"quality": 95} if f.suffix.lower() != ".png" else {}))
        if f == pano_of(name):
            rolled = abs(shift)
    for p in d.glob("pano*.preview.jpg"):
        p.unlink()
    preview_of(name)
    for v in variants_of(name):
        preview_of(name, v)
    old = applied_of(name) or 0.0
    _aligned_rec(name).write_text(
        json.dumps({"degrees": round((old + degrees) % 360.0, 2),
                    "last": round(degrees, 2)}, indent=1))
    return rolled


def sync_nav() -> None:
    """Write the nav layer back into building.yaml, where RMF's tools read
    it: every dream vertex as a named vertex on its own level, every edge
    as a bidirectional graph-0 lane.

    The nav layer is this tree's to own — the clean start began at zero
    vertices, and this editor is where nav content is authored — so all
    NAMED vertices and ALL lanes are regenerated wholesale on every sync.
    Walls, doors, floors, holes, measurements, lifts and their unnamed
    vertices are never touched; their indices are remapped around the
    removals, and if a named vertex turns out to anchor structure the sync
    refuses rather than guess. Last writer wins on the file itself: keep
    the traffic editor closed while tracing nav here, or re-open it after.
    """
    f = building_file()
    if not f:
        return
    B = yaml.safe_load(f.read_text())
    dream = load_dream()
    edges = load_edges()
    for lname, L in (B.get("levels") or {}).items():
        V = L.get("vertices") or []
        # lift cabins are named IN PLACE: the cabin vertex is the traffic
        # editor's (position, lift_cabin param), so it keeps its row and
        # only its name comes and goes with the dream vertex that wears it
        for v in V:
            if len(v) > 4 and isinstance(v[4], dict) and "lift_cabin" in v[4]:
                v[3] = ""
        named = {i for i, v in enumerate(V)
                 if len(v) > 3 and isinstance(v[3], str) and v[3]}
        refs = set()
        for key in ("walls", "doors", "measurements"):
            refs |= {i for e in L.get(key) or [] for i in e[:2]}
        for key in ("floors", "holes"):
            for poly in L.get(key) or []:
                refs |= set(poly["vertices"])
        if refs & named:
            raise RuntimeError(f"{lname}: a named vertex anchors structure "
                               f"— not rewriting the nav layer")
        keep = [i for i in range(len(V)) if i not in named]
        remap = {old: new for new, old in enumerate(keep)}
        L["vertices"] = [V[i] for i in keep]
        for key in ("walls", "doors", "measurements"):
            for e in L.get(key) or []:
                e[0], e[1] = remap[e[0]], remap[e[1]]
        for key in ("floors", "holes"):
            for poly in L.get(key) or []:
                poly["vertices"] = [remap[i] for i in poly["vertices"]]
        cabin_idx = {v[4]["lift_cabin"][1]: i
                     for i, v in enumerate(L["vertices"])
                     if len(v) > 4 and isinstance(v[4], dict)
                     and "lift_cabin" in v[4]}
        index = {}
        for nm in sorted(n for n, v in dream.items() if v["level"] == lname):
            v = dream[nm]
            if v.get("lift") in cabin_idx:
                i = cabin_idx[v["lift"]]
                L["vertices"][i][3] = nm
                index[nm] = i
            else:
                index[nm] = len(L["vertices"])
                L["vertices"].append([v["x"], v["y"], 0, nm])
        # traffic editor's lane record: endpoint indices and typed params —
        # 4 is bool, 2 is int, 1 is string; graph_idx 0 is the nav graph
        # everything downstream reads
        L["lanes"] = [[index[a], index[b],
                       {"bidirectional": [4, True], "graph_idx": [2, 0],
                        "orientation": [1, ""]}]
                      for a, b in edges if a in index and b in index]
    f.write_text(yaml.safe_dump(B, default_flow_style=None, sort_keys=True,
                                width=10**9))


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
            else:
                sig.append(f"{d.name}:{d.stat().st_mtime}")
    return hash(tuple(sig))
