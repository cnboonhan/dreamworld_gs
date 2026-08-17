"""The dreamworld tree and the map it grows against.

Everything the flow produces lives under the project's dreamworld/ tree —
one folder per vertex (vertex.json, pano.jpg, aligned.json, splat later)
plus edges.json — and this module is that tree's only writer. building.yaml
stays the traffic editor's file: walls, doors and scale are read from it,
never written.
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
                      if len(v) > 3 and isinstance(v[3], str) and v[3]],
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
        if not stem.endswith(".preview"):
            out.add(stem)
    return sorted(out)


def save_variant(name: str, variant: str, png: bytes) -> None:
    """A fresh restyle replaces the variant entire: its old image, preview
    and alignment record all describe a picture that no longer exists."""
    delete_variant(name, variant)
    (DREAM / name / f"{_stem(variant)}.png").write_bytes(png)
    preview_of(name, variant)


def delete_variant(name: str, variant: str) -> None:
    d = DREAM / name
    for ext in (".jpg", ".jpeg", ".png"):
        (d / f"{_stem(variant)}{ext}").unlink(missing_ok=True)
    (d / f"{_stem(variant)}.preview.jpg").unlink(missing_ok=True)
    (d / f"aligned@{variant}.json").unlink(missing_ok=True)


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
        splat = d / "splat"
        out[d.name] = {"level": v.get("level", ""), "x": float(v["x"]),
                       "y": float(v["y"]), "pano": pano_of(d.name) is not None,
                       "applied": applied,
                       "splat": splat.is_dir() and any(splat.iterdir())}
    return out


def state_color(v: dict) -> str:
    """Red until the panorama is up, green once everything is done —
    aligned and a splat generated — yellow while work remains."""
    if not v["pano"]:
        return C_RED
    if v["applied"] is not None and v["splat"]:
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
    return name


def move_vertex(name: str, x: float, y: float) -> None:
    vj = DREAM / name / "vertex.json"
    v = json.loads(vj.read_text())
    v["x"], v["y"] = round(x, 2), round(y, 2)
    vj.write_text(json.dumps(v, indent=1))


def rename_vertex(old: str, new: str) -> None:
    (DREAM / old).rename(DREAM / new)
    rename_in_edges(old, new)


def delete_vertex(name: str) -> None:
    shutil.rmtree(DREAM / name)
    drop_from_edges(name)


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
    return f"connected {a} — {b}"


def rename_in_edges(old: str, new: str) -> None:
    save_edges([(new if a == old else a, new if b == old else b)
                for a, b in load_edges()])


def drop_from_edges(name: str) -> None:
    save_edges([(a, b) for a, b in load_edges() if name not in (a, b)])


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
# Every operation takes an optional variant and works on that file alone:
# a variant aligns exactly the way the original does, with its own record.

def _aligned_rec(name: str, variant: str | None) -> Path:
    return DREAM / name / (f"aligned@{variant}.json" if variant
                           else "aligned.json")


def applied_of(name: str, variant: str | None = None):
    rec = _aligned_rec(name, variant)
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


def apply_roll(name: str, degrees: float, variant: str | None = None) -> int:
    """Roll the panorama by `degrees` and record the running total.

    The shift is NEGATIVE, and that sign is the whole bug main already paid
    for: the preview shows content at longitude L appearing at L + corr,
    while np.roll by +shift moves it from L to L - delta. Opposite signs —
    save with the wrong one and a panorama turned until it looked right
    comes back with the corridor at twice the angle on the wrong side.
    """
    f = pano_of(name, variant)
    im = np.asarray(Image.open(f).convert("RGB"))
    shift = -int(round(degrees / 360.0 * im.shape[1]))
    Image.fromarray(np.roll(im, shift, axis=1)).save(
        f, **({"quality": 95} if f.suffix.lower() != ".png" else {}))
    (DREAM / name / f"{_stem(variant)}.preview.jpg").unlink(missing_ok=True)
    preview_of(name, variant)
    rec = _aligned_rec(name, variant)
    old = applied_of(name, variant) or 0.0
    rec.write_text(json.dumps({"degrees": round((old + degrees) % 360.0, 2),
                               "last": round(degrees, 2)}, indent=1))
    return abs(shift)


def reset_roll(name: str, variant: str | None = None) -> float:
    """Undo the saved roll entire: the panorama returns to the orientation
    it was uploaded in, and the record of having been turned goes with it.
    Possible only because aligned.json keeps the cumulative degrees —
    the file itself carries no memory of the turn."""
    applied = applied_of(name, variant)
    if not applied:
        return 0.0
    f = pano_of(name, variant)
    im = np.asarray(Image.open(f).convert("RGB"))
    shift = int(round(applied / 360.0 * im.shape[1]))    # apply_roll, negated
    Image.fromarray(np.roll(im, shift, axis=1)).save(
        f, **({"quality": 95} if f.suffix.lower() != ".png" else {}))
    (DREAM / name / f"{_stem(variant)}.preview.jpg").unlink(missing_ok=True)
    preview_of(name, variant)
    _aligned_rec(name, variant).unlink(missing_ok=True)
    return applied


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
