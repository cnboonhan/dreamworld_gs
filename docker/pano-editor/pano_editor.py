#!/usr/bin/env python3
"""pano_editor — edit a waypoint's panorama in place, by looking at it and saying
what to change.

Ported from dreamworld/docker/dream_editor. The editing is the same editing: face
a direction in the 360 viewer, type an instruction, and _perspective_edit crops
what you are facing, edits that undistorted crop through the Qwen server,
reprojects it back into the equirect and composites only what changed. Stack
several edits, then Save.

What differs is the shelf either side of it. There the panorama was one artifact
of a built library, so Save re-ran build_library to propagate the change into
every clip that showed that vertex. Here the panorama IS the input: one file per
waypoint under panos/, so Save writes that file and the propagation is a
`just generate <id>`, which builds the splat world from it again.

    python pano_editor.py --base /projects/<project> --level L11 --port 8087
"""

import argparse
import glob
import json
import os
import re
import shutil
import struct
import sys
import threading

import requests
import yaml
from flask import Flask, Response, jsonify, request, send_file



# Inlined from dreamworld/pipeline/nav.py — two functions, and vendoring the module
# would drag in the whole pipeline package for them.
def nav_level_names(nav_yaml):
    """Ordered level names present in the nav graph (e.g. ['L1', 'L11'])."""
    return list(yaml.safe_load(open(nav_yaml))["levels"].keys())


def load_nav(nav_yaml, level=None):
    """(verts [(name, x, y)], adjacency {i: set(j)}, directed edges [(i, j)]) for one
    level. Levels are independent — no inter-level (lift) edges — so each is a
    self-contained graph."""
    levels = yaml.safe_load(open(nav_yaml))["levels"]
    lvl = levels[level] if level is not None else next(iter(levels.values()))
    verts = []
    for v in lvl.get("vertices", []):
        p = v[2] if len(v) > 2 and isinstance(v[2], dict) else {}
        verts.append((p.get("name", ""), float(v[0]), float(v[1])))
    edges = [(int(l[0]), int(l[1])) for l in lvl.get("lanes", []) if l[0] != l[1]]
    adj = {i: set() for i in range(len(verts))}
    for i, j in edges:
        adj[i].add(j)
        adj[j].add(i)
    return verts, adj, edges

app = Flask(__name__)
G = {}                                   # verts, adj, level, base, gen, m2px, fp_w, fp_h, qwen, cand_dir


# ── floorplan projection (same as dream_interactive) ─────────────────────────────
def _fit_affine(src, dst):
    n = len(src)
    sx = sum(p[0] for p in src); sy = sum(p[1] for p in src); s1 = n
    sxx = sum(p[0] * p[0] for p in src); sxy = sum(p[0] * p[1] for p in src); syy = sum(p[1] * p[1] for p in src)
    bx = sum(src[i][0] * dst[i] for i in range(n)); by = sum(src[i][1] * dst[i] for i in range(n))
    bs = sum(dst)
    # solve the 3x3 normal equations [[sxx,sxy,sx],[sxy,syy,sy],[sx,sy,s1]] [a,b,c]^T = [bx,by,bs]
    m = [[sxx, sxy, sx, bx], [sxy, syy, sy, by], [sx, sy, s1, bs]]
    for i in range(3):
        p = m[i][i] or 1e-9
        for j in range(i, 4):
            m[i][j] /= p
        for k in range(3):
            if k != i:
                f = m[k][i]
                for j in range(i, 4):
                    m[k][j] -= f * m[i][j]
    return m[0][3], m[1][3], m[2][3]


def build_floorplan_map(base, level, verts):
    # maps/<name>.building.yaml, with its drawing beside it — this repo keeps both
    # under maps/ rather than at the project root.
    byaml = (glob.glob(os.path.join(base, "maps", "*.building.yaml")) or [None])[0]
    if not byaml:
        return None, 0, 0, None
    B = yaml.safe_load(open(byaml))
    BL = B.get("levels", {}).get(level)
    if not BL:
        return None, 0, 0, None
    png = os.path.join(os.path.dirname(byaml),
                       (BL.get("drawing") or {}).get("filename", ""))
    if not os.path.isfile(png):
        return None, 0, 0, None
    w = h = 0
    try:
        with open(png, "rb") as f:
            f.read(16); w, h = struct.unpack(">II", f.read(8))
    except Exception:
        pass
    bname = {v[3]: (v[0], v[1]) for v in BL["vertices"] if len(v) > 3 and isinstance(v[3], str) and v[3]}
    nname = {name: (x, y) for (name, x, y) in verts if name}
    shared = [k for k in bname if k in nname]
    if len(shared) < 3:
        return png, w, h, None
    src = [nname[k] for k in shared]
    ax = _fit_affine(src, [bname[k][0] for k in shared])
    ay = _fit_affine(src, [bname[k][1] for k in shared])
    return png, w, h, (lambda x, y: (ax[0] * x + ax[1] * y + ax[2], ay[0] * x + ay[1] * y + ay[2]))


# ── paths + graph helpers ────────────────────────────────────────────────────────
def vid(v):
    """The id a waypoint's panorama is filed under: <level>.<name or v-index>."""
    name = G["verts"][v][0]
    return f"{G['level']}.{name or f'v{v}'}"


def _stem(v, variant=""):
    """The filename stem a (waypoint, variant) pair is filed under:
    <id> for the original, <id>@<name> for a variant of it."""
    return vid(v) + ("@" + variant if variant else "")


def _clean_variant(name):
    n = str(name or "").strip()
    return n if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", n) else None


def live_pano(v, variant=""):
    """The panorama on disk for waypoint v, whatever extension the camera wrote."""
    base = os.path.join(G["base"], "panos", _stem(v, variant))
    for ext in (".JPG", ".jpg", ".jpeg", ".JPEG", ".png", ".PNG"):
        if os.path.isfile(base + ext):
            return base + ext
    return None


def pano_variants(v):
    """This waypoint's looks: "" is the original photograph, every other name
    is a panos/<id>@<name>.<ext> beside it."""
    names = [""] if live_pano(v) else []
    for p in glob.glob(os.path.join(G["base"], "panos", vid(v) + "@*")):
        n = os.path.splitext(os.path.basename(p))[0].split("@", 1)[1]
        if n and n not in names:
            names.append(n)
    return names


def pano_path(v, kind, variant=""):
    """`current` is the file the pipeline reads; `candidate` is the pending edit.

    There is no `gz` here — that was the simulated panorama the dream restyled
    from, and this repo edits the photograph itself.
    """
    if kind in ("restyle", "current"):
        return live_pano(v, variant) or os.path.join(
            G["base"], "panos", _stem(v, variant) + ".JPG")
    if kind == "candidate":
        return os.path.join(G["cand_dir"], _stem(v, variant) + ".png")
    return None


# ── Qwen re-restyle → candidate (edit modes) ──────────────────────────────────────
def _composite_inpaint(base_path, gen_bytes, mask_b64, out_path):
    """Keep ONLY the brushed region of the restyle: candidate = base outside the mask, the
    fresh restyle inside it, with a feathered edge — so an inpaint changes just that region."""
    import base64
    import io
    from PIL import Image, ImageFilter
    W, H = 1536, 768
    base = Image.open(base_path).convert("RGB").resize((W, H))
    gen = Image.open(io.BytesIO(gen_bytes)).convert("RGB").resize((W, H))
    m = Image.open(io.BytesIO(base64.b64decode(mask_b64.split(",", 1)[-1]))).convert("L")
    m = m.resize((W, H)).filter(ImageFilter.GaussianBlur(8))       # feather the brush edge
    Image.composite(gen, base, m).save(out_path)


def _composite_autodiff(base_path, gen_bytes, out_path, thresh=22):
    """No brush needed: keep only the pixels the model actually CHANGED. Diff the edit against
    the base, threshold, dilate + feather -> an automatic mask. Regions the instruction didn't
    touch stay pixel-identical to the original, so a whole-pano edit can't drift the rest."""
    import io
    from PIL import Image, ImageChops, ImageFilter
    W, H = 1536, 768
    base = Image.open(base_path).convert("RGB").resize((W, H))
    gen = Image.open(io.BytesIO(gen_bytes)).convert("RGB").resize((W, H))
    diff = ImageChops.difference(base, gen).convert("L")
    m = diff.point(lambda p: 255 if p > thresh else 0)             # where the edit landed
    m = m.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(6))  # grow + feather
    if m.getextrema()[1] < 8:                                      # model changed ~nothing
        gen.save(out_path); return
    Image.composite(gen, base, m).save(out_path)


# ── perspective (view-focused) edit ───────────────────────────────────────────────
# Extract the rectilinear view the user is FACING (same basis as the WebGL viewer:
# forward +X, up +Z, horizontal fov), let Qwen-Image-Edit edit that undistorted photo,
# then reproject it back into the equirect so the object lands exactly where they looked.
def _cam_basis(yaw, pitch):
    import math
    import numpy as np
    F = np.array([math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)])
    R = np.cross(F, [0.0, 0.0, 1.0]); R = R / (np.linalg.norm(R) or 1)
    U = np.cross(R, F)
    return F, R, U


def _bilinear(arr, u, vv, wrap_x=False):
    import numpy as np
    He, We = arr.shape[:2]
    u0 = np.floor(u).astype(int); v0 = np.floor(vv).astype(int)
    fu = (u - u0)[..., None]; fv = (vv - v0)[..., None]
    if wrap_x:
        u0m, u1m = u0 % We, (u0 + 1) % We
    else:
        u0m, u1m = np.clip(u0, 0, We - 1), np.clip(u0 + 1, 0, We - 1)
    v0m, v1m = np.clip(v0, 0, He - 1), np.clip(v0 + 1, 0, He - 1)
    a, b = arr[v0m, u0m], arr[v0m, u1m]; c, d = arr[v1m, u0m], arr[v1m, u1m]
    return (a * (1 - fu) + b * fu) * (1 - fv) + (c * (1 - fu) + d * fu) * fv


def _extract_perspective(equi, yaw, pitch, fov, Wp, Hp):
    import math
    import numpy as np
    from PIL import Image
    arr = np.asarray(equi.convert("RGB")).astype(np.float32)
    He, We = arr.shape[:2]
    F, R, U = _cam_basis(yaw, pitch); t = math.tan(fov / 2)
    gx, gy = np.meshgrid(np.arange(Wp), np.arange(Hp))
    cx = (gx + 0.5 - 0.5 * Wp) / (0.5 * Wp)
    cy = ((Hp - 1 - gy) + 0.5 - 0.5 * Hp) / (0.5 * Wp)       # gl_FragCoord is y-up
    d = F[None, None] + (cx * t)[..., None] * R[None, None] + (cy * t)[..., None] * U[None, None]
    d /= np.linalg.norm(d, axis=2, keepdims=True)
    lon = np.arctan2(d[..., 1], d[..., 0]); lat = np.arcsin(np.clip(d[..., 2], -1, 1))
    u = (lon / (2 * math.pi) + 0.5) * We; vv = (0.5 - lat / math.pi) * He
    return Image.fromarray(np.clip(_bilinear(arr, u, vv, wrap_x=True), 0, 255).astype(np.uint8))


def _reproject_into_equi(base, edited_crop, yaw, pitch, fov):
    """Sample the edited crop back onto every in-frustum equirect pixel; return (reproj, coverage)."""
    import math
    import numpy as np
    base_a = np.asarray(base.convert("RGB")).astype(np.float32)
    crop_a = np.asarray(edited_crop.convert("RGB")).astype(np.float32)
    Hp, Wp = crop_a.shape[:2]; He, We = base_a.shape[:2]
    F, R, U = _cam_basis(yaw, pitch); t = math.tan(fov / 2)
    lon = ((np.arange(We) + 0.5) / We - 0.5) * 2 * math.pi
    lat = (0.5 - (np.arange(He) + 0.5) / He) * math.pi
    LON, LAT = np.meshgrid(lon, lat)
    dx = np.cos(LAT) * np.cos(LON); dy = np.cos(LAT) * np.sin(LON); dz = np.sin(LAT)
    cf = dx * F[0] + dy * F[1] + dz * F[2]
    cfx = np.where(cf > 1e-3, cf, 1.0)
    scx = (dx * R[0] + dy * R[1] + dz * R[2]) / cfx / t
    scy = (dx * U[0] + dy * U[1] + dz * U[2]) / cfx / t
    px = scx * 0.5 * Wp + 0.5 * Wp - 0.5
    py = (Hp - 1) - (scy * 0.5 * Wp + 0.5 * Hp - 0.5)
    inframe = (cf > 1e-3) & (px >= 0) & (px <= Wp - 1) & (py >= 0) & (py <= Hp - 1)
    sampled = _bilinear(crop_a, np.clip(px, 0, Wp - 1), np.clip(py, 0, Hp - 1))
    reproj = base_a.copy(); reproj[inframe] = sampled[inframe]
    return reproj, inframe.astype(np.float32)


def _perspective_edit(base_path, url, prompt, seed, view, out_path):
    """View-focused edit: crop what the viewer faces, edit it undistorted, reproject + diff-composite."""
    import io
    import math
    import numpy as np
    from PIL import Image, ImageFilter
    base = Image.open(base_path).convert("RGB").resize((1536, 768))
    yaw, pitch = float(view["yaw"]), float(view["pitch"])
    fov = min(max(float(view.get("fov", 1.4)), 0.5), 2.3)                # clamp extreme zoom
    aspect = min(max(float(view.get("aspect", 0.66)), 0.4), 1.4)
    Wp = 1024; Hp = int(round(Wp * aspect / 16) * 16)
    crop = _extract_perspective(base, yaw, pitch, fov, Wp, Hp)
    buf = io.BytesIO(); crop.save(buf, "PNG"); buf.seek(0)
    p = (f"This is a photo of a room interior. Edit it in place: {prompt}. Keep the walls, "
         f"floor, ceiling, windows, camera viewpoint and lighting exactly as shown, changing "
         f"only what this instruction describes; render it sharp and photorealistic.")
    r = requests.post(f"{url}/restyle", files=[("image", ("view.png", buf, "image/png"))],
                      data={"prompt": p, "steps": 40, "guidance": 4.0, "seed": seed,
                            "width": Wp, "height": Hp, "seamless": "false", "void_fill": "false"},
                      timeout=1800)
    if r.status_code != 200 or len(r.content) < 1000:
        return False, f"qwen {r.status_code}"
    edited = Image.open(io.BytesIO(r.content)).convert("RGB").resize((Wp, Hp))
    reproj, cov = _reproject_into_equi(base, edited, yaw, pitch, fov)
    # object mask = where the edit changed pixels, grown + feathered ...
    diff = np.abs(reproj - np.asarray(base, np.float32)).max(axis=2)
    dm = Image.fromarray(((diff > 22) * 255).astype(np.uint8))
    dm = dm.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.GaussianBlur(4))
    # ... faded toward the view edge so a heavily-redrawn frustum doesn't paste a hard seam.
    cs = Image.fromarray((cov * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(15)).filter(ImageFilter.GaussianBlur(18))
    m = (np.asarray(dm, np.float32) / 255 * np.asarray(cs, np.float32) / 255 * 255).astype(np.uint8)
    mimg = Image.fromarray(m).filter(ImageFilter.GaussianBlur(3))
    if mimg.getextrema()[1] < 8:
        return False, "no change (try rephrasing, or zoom so the target fills the view)"
    Image.composite(Image.fromarray(np.clip(reproj, 0, 255).astype(np.uint8)), base, mimg).save(out_path)
    return True, "ok"


def _cand_base(v, from_candidate, variant=""):
    """Accumulate: successive edits build on the last candidate when asked, else on the saved
    restyle. So you can stack several edits (add a table, then a lamp, then a rug) before Save."""
    cand = pano_path(v, "candidate", variant)
    return cand if (from_candidate and os.path.isfile(cand)) \
        else pano_path(v, "restyle", variant)


def _snapshot_candidate(v, variant=""):
    """Push the current candidate onto a history stack so an edit can be reverted."""
    import shutil
    cand = pano_path(v, "candidate", variant)
    if os.path.isfile(cand):
        key = _stem(v, variant) if variant else str(v)
        n = len(glob.glob(os.path.join(G["cand_dir"], f"{key}.h*.png")))
        shutil.copy2(cand, os.path.join(G["cand_dir"], f"{key}.h{n}.png"))


def _clear_hist(v, variant=""):
    """Drop an edit history (a fresh edit session starts clean)."""
    key = _stem(v, variant) if variant else str(v)
    for p in glob.glob(os.path.join(G["cand_dir"], f"{key}.h*.png")):
        os.remove(p)


def revert_candidate(v, variant=""):
    """Undo the last accumulated edit: pop the newest history snapshot back to the candidate.
    Returns True if a candidate still remains (compare view), False if we're back to the base."""
    import shutil
    key = _stem(v, variant) if variant else str(v)
    hist = sorted(glob.glob(os.path.join(G["cand_dir"], f"{key}.h*.png")),
                  key=lambda p: int(p.rsplit(".h", 1)[1].split(".")[0]))
    cand = pano_path(v, "candidate", variant)
    if hist:
        shutil.move(hist[-1], cand); return True
    if os.path.isfile(cand):
        os.remove(cand)
    return False


def edit_candidate(v, prompt, seed, view=None, mask_b64=None, from_candidate=False,
                   variant=""):
    """Edit waypoint v's panorama -> candidate PNG.

    One mode, where the dream had four. `restyle` and `reference` re-rendered the
    whole panorama from the simulator's flat geometry toward a style phrase — there
    is no simulated pano here and no style to impose: this repo's panoramas are
    photographs of a real building, and the job is to change one thing in one and
    leave the rest of the photograph alone. `inpaint` brushed a region of such a
    restyle back in, which only exists to undo the same problem.

    So what is left is the edit: crop what the viewer faces, edit that undistorted
    crop, reproject it, and composite only the pixels that changed.
    """
    url = G["qwen"][0]
    base = _cand_base(v, from_candidate, variant)
    if not base or not os.path.isfile(base):
        return False, f"no panorama for v{v}" + (f" variant '{variant}'" if variant
                                                 else "") + " to edit"
    os.makedirs(G["cand_dir"], exist_ok=True)
    if from_candidate:
        _snapshot_candidate(v, variant)    # stacking -> make this edit revertible
    else:
        _clear_hist(v, variant)            # fresh edit session on this look
    if view and not mask_b64:
        # The view-focused path: the crop is undistorted, so the model sees a
        # photograph of a room rather than a warped band of one.
        return _perspective_edit(base, url, prompt, seed, view,
                                 pano_path(v, "candidate", variant))
    # No facing given (or a brushed mask) -> edit the whole equirect. Framed as an
    # in-place edit and with void_fill off, so the model changes what was asked and
    # does not drift the rest of the photograph toward the phrase.
    p = (f"Image 1 is a photograph of a room interior. Edit it in place: {prompt}. "
         f"Keep the camera viewpoint, walls, floor, ceiling, windows and lighting "
         f"exactly as shown in image 1 — change only what this instruction describes, "
         f"and render it sharp and photorealistic, matching the existing lighting.")
    r = requests.post(f"{url}/restyle", files=[("image", open(base, "rb"))],
                      data={"prompt": p, "steps": 40, "guidance": 4.0, "seed": seed,
                            "width": 1536, "height": 768, "seamless": "true",
                            "void_fill": "false"}, timeout=1800)
    if r.status_code != 200 or len(r.content) < 1000:
        return False, f"qwen {r.status_code}"
    os.makedirs(G["cand_dir"], exist_ok=True)
    out = pano_path(v, "candidate", variant)
    if mask_b64:
        _composite_inpaint(base, r.content, mask_b64, out)   # brush: keep that region
    else:
        _composite_autodiff(base, r.content, out)            # else: keep changed pixels
    return True, "ok"


# ── style anchor (editable RESTYLE_PROMPT) ────────────────────────────────────────
# multilevel_office has no anchor IMAGES, so appearance is prompt-driven: this is the
# building's canonical look. Stored per-project; forwarded to build_library on regen and
# used as the default prompt for from-geometry restyles.
DEFAULT_STYLE = ("Restyle image 1 (a flat-shaded 360 panorama of an interior) into a photorealistic "
                 "modern empty office corridor. Smooth matte white plaster walls, polished concrete "
                 "floor, flat continuous white ceiling with flush recessed lights, soft even indoor "
                 "lighting, deserted with no people, fully enclosed with no windows.")


# ── routes ────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(__file__), "index.html"))


@app.route("/levels")
def levels():
    return jsonify(levels=G["levels"], current=G["level"])


@app.route("/graph")
def graph():
    m2px = G["m2px"]
    # A waypoint is marked green once its panorama exists — the editor is for
    # fixing one before it is generated, so which have one is the useful overlay.
    verts = [{"id": i, "name": n, "px": round((m2px(x, y) if m2px else (x, y))[0], 1),
              "py": round((m2px(x, y) if m2px else (x, y))[1], 1),
              "door": bool(live_pano(i)), "shot": bool(live_pano(i))}
             for i, (n, x, y) in enumerate(G["verts"])]
    edges, seen = [], set()
    for u in G["adj"]:
        for v in G["adj"][u]:
            k = tuple(sorted((u, v)))
            if k not in seen:
                seen.add(k); edges.append({"u": u, "v": v})
    return jsonify(w=G["fp_w"], h=G["fp_h"], level=G["level"], has_fp=bool(G["fp_png"]),
                   verts=verts, edges=edges)


@app.route("/floorplan.png")
def floorplan():
    return send_file(G["fp_png"]) if G["fp_png"] else Response("no floorplan", status=404)


@app.route("/pano")
def pano():
    v, kind = int(request.args["v"]), request.args.get("kind", "restyle")
    p = pano_path(v, kind, request.args.get("variant", ""))
    return send_file(p) if p and os.path.isfile(p) else Response("no pano", status=404)


@app.route("/variants")
def r_variants():
    """This waypoint's looks: "" is the original, the rest are its variants."""
    return jsonify(variants=pano_variants(int(request.args["v"])))


def _rebuild_scenes_index():
    """scenes.json is normally edge_walks' to write; deleting a variant must
    heal it too, or the viewer's dropdown goes on offering a world that is
    gone. Same entries, same source of truth: the splat dirs on disk."""
    root = os.path.join(G["base"], "splats")
    index = []
    for d in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        ply = os.path.join(root, d, "world.ply")
        paths = os.path.join(root, d, "world.paths.json")
        if not (os.path.isfile(ply) and os.path.isfile(paths)):
            continue
        try:
            p = json.load(open(paths))
        except (OSError, ValueError):
            continue
        index.append({"scene": d, "lanes": len(p.get("lanes", [])),
                      "walks": len(p.get("walks", [])),
                      "placed": len(p.get("placed", {}))})
    with open(os.path.join(root, "scenes.json"), "w") as f:
        json.dump(index, f)


@app.route("/variant_delete", methods=["POST"])
def r_variant_delete():
    """Remove one look of a waypoint: its panorama, its splat world, its
    marks, its pending edits. The ORIGINAL is not a variant and cannot be
    deleted from here — it is the photograph everything else is made from."""
    b = request.get_json(force=True) or {}
    v = int(b["v"])
    variant = _clean_variant(b.get("variant"))
    if not variant:
        return jsonify(ok=False, error="the original cannot be deleted — "
                                       "only variants can")
    if variant not in pano_variants(v):
        return jsonify(ok=False, error=f"v{v} has no variant '{variant}'")
    ident = vid(v) + "@" + variant
    removed = []
    p = live_pano(v, variant)
    if p:
        os.remove(p); removed.append(os.path.relpath(p, G["base"]))
    world = os.path.join(G["base"], "splats", ident)
    if os.path.isdir(world):
        shutil.rmtree(world); removed.append(f"splats/{ident}/")
    mark = os.path.join(G["base"], "splats", ".aligned", ident + ".json")
    if os.path.isfile(mark):
        os.remove(mark); removed.append(f"splats/.aligned/{ident}.json")
    cand = pano_path(v, "candidate", variant)
    if os.path.isfile(cand):
        os.remove(cand)
    _clear_hist(v, variant)
    _rebuild_scenes_index()
    return jsonify(ok=True, removed=removed,
                   message=f"deleted variant '{variant}' of {vid(v)}"
                           f" ({', '.join(removed) or 'no files found'})")


@app.route("/edit", methods=["POST"])
@app.route("/restyle", methods=["POST"])          # the name the ported page posts to
def edit():
    b = request.get_json(force=True) or {}
    v, prompt = int(b["v"]), (b.get("prompt") or "").strip()
    if not prompt:
        return jsonify(ok=False, error="say what to change")
    ok, msg = edit_candidate(v, prompt, int(b.get("seed", v)),
                             view=b.get("view"), mask_b64=b.get("mask"),
                             from_candidate=bool(b.get("from_candidate")),
                             variant=str(b.get("variant") or ""))
    return jsonify(ok=ok, error=None if ok else msg, v=v)


@app.route("/level", methods=["POST"])
def switch_level():
    """Switch the editor to another level of the building."""
    lv = (request.get_json(force=True) or {}).get("level")
    if lv not in G["levels"]:
        return jsonify(ok=False, error=f"no level {lv}; have {G['levels']}"), 400
    set_level(lv)
    return jsonify(ok=True, level=lv)


@app.route("/save", methods=["POST"])
def save():
    """Adopt the candidate as the waypoint's panorama.

    It writes the file and stops. It does NOT rebuild the splat world: that is
    `just generate <id>`, twenty minutes on four GPUs, which belongs on the queue
    at :4200 where it can be watched and retried — not inside a click here. The
    response says which command to run.

    The panorama it replaces is kept beside it, because it is a photograph of a
    real place and an edit is not obviously an improvement until it is looked at.
    """
    b = request.get_json(force=True) or {}
    v = int(b["v"])
    variant = str(b.get("variant") or "")
    cand = pano_path(v, "candidate", variant)
    if not os.path.isfile(cand):
        return jsonify(ok=False, error="nothing to save — make an edit first")
    from PIL import Image
    as_new = b.get("as_new")
    if as_new is not None:
        # Save the candidate as a NEW look of this waypoint, beside the one it
        # was edited from. Nothing is replaced, so nothing goes to
        # .before-edit; the original photograph is untouched by construction.
        name = _clean_variant(as_new)
        if name is None:
            return jsonify(ok=False, error="variant names are letters, digits, "
                                           "- and _ (up to 32)")
        if name in pano_variants(v):
            return jsonify(ok=False, error=f"variant '{name}' already exists — "
                                           "select it and Save to overwrite it")
        src = live_pano(v, variant) or live_pano(v)
        ext = os.path.splitext(src)[1] if src else ".JPG"
        ident = vid(v) + "@" + name
        dest = os.path.join(G["base"], "panos", ident + ext)
        im = Image.open(cand).convert("RGB")
        im.save(dest, quality=95) if dest.lower().endswith((".jpg", ".jpeg")) \
            else im.save(dest)
        os.remove(cand)
        _clear_hist(v, variant)
        return jsonify(ok=True, saved=os.path.relpath(dest, G["base"]), id=ident,
                       variant=name, next=f"just generate {ident}",
                       message=f"saved new variant '{name}' — build its world "
                               f"with `just generate {ident}`")
    live = live_pano(v, variant)
    ident = _stem(v, variant)
    dest = live or os.path.join(G["base"], "panos", ident + ".JPG")
    if live:
        kept = os.path.join(G["base"], "panos", ".before-edit")
        os.makedirs(kept, exist_ok=True)
        shutil.copy2(live, os.path.join(kept, os.path.basename(live)))
    # Write in the extension already on disk: the pipeline finds the file by stem
    # and PIL sniffs content, but a .JPG holding a PNG confuses anything reading
    # it by name.
    im = Image.open(cand).convert("RGB")
    im.save(dest, quality=95) if dest.lower().endswith((".jpg", ".jpeg")) else im.save(dest)
    os.remove(cand)
    _clear_hist(v, variant)
    # The alignment record is about how the panorama was TURNED, and an edit does
    # not turn it — so it stays, and a regenerated world keeps the same heading.
    return jsonify(ok=True, saved=os.path.relpath(dest, G["base"]), id=ident,
                   kept=".before-edit/" + os.path.basename(dest),
                   next=f"just generate {ident}",
                   message=f"saved {ident} — its splat world is now out of date; "
                           f"run `just generate {ident}` to rebuild it")


@app.route("/revert", methods=["POST"])
def revert():
    b = request.get_json(force=True) or {}
    v = int(b["v"])
    has_cand = revert_candidate(v, str(b.get("variant") or ""))
    return jsonify(ok=True, v=v, has_candidate=has_cand)


def set_level(level):
    """(Re)load the CURRENT level's nav graph + floorplan projection + candidate dir into G,
    so the whole editor (graph / pano / save) switches to that level."""
    verts, adj, _ = load_nav(G["nav"], level)
    fp_png, fp_w, fp_h, m2px = build_floorplan_map(G["base"], level, verts)
    G.update(level=level, verts=verts, adj=adj, fp_png=fp_png, fp_w=fp_w, fp_h=fp_h, m2px=m2px,
             cand_dir=os.path.join(G["base"], "panos", ".candidates"))
    os.makedirs(G["cand_dir"], exist_ok=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True, help="project dir (assets/projects/<name>)")
    ap.add_argument("--level", default=None, help="start level (default: the first nav level)")
    ap.add_argument("--nav", default=None, help="nav_graphs/0.yaml (default: <base>/outputs/generate_gz/...)")
    ap.add_argument("--port", type=int, default=8087)
    a = ap.parse_args()
    base = os.path.abspath(a.base)
    gen = os.path.join(base, "worlds")
    nav = a.nav or (glob.glob(os.path.join(gen, "*", "nav_graphs", "0.yaml")) or [""])[0]
    byaml = (glob.glob(os.path.join(base, "maps", "*.building.yaml")) or [None])[0]
    G.update(base=base, gen=gen, nav=nav, byaml=byaml, levels=nav_level_names(nav),
             qwen=[u for u in os.environ.get("QWEN_URLS", "http://127.0.0.1:8100").split(",") if u],
             repo=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    set_level(a.level or G["levels"][0])
    print(f"pano_editor: levels={G['levels']}, start {G['level']} ({len(G['verts'])} verts); "
          f"qwen={G['qwen']}; http://localhost:{a.port}", flush=True)
    app.run(host="0.0.0.0", port=a.port, threaded=True)


if __name__ == "__main__":
    main()
