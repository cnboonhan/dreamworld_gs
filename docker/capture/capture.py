#!/usr/bin/env python3
"""capture — one surface for getting a 360 into the building.

The flow used to be four tools: name the file by hand, copy it into panos/,
turn it in the aligner on :8085, and run `just generate` in a terminal. This
is that flow as one page: click the waypoint, drop the photo, drag it until
the corridors sit under their markers, press generate. Iteration is the
point — reshoot a waypoint, or add a variant look, without leaving the page.

    python capture.py --base /projects/<project> --level L11 --port 8089

Alignment is the aligner's own semantics, ported: saving ROLLS the image
(a rolled equirect is still an equirect, so every reader downstream needs no
correction), and .aligned/<id>.json records the accumulated degrees so a
second pass adds to the roll rather than starting over.
"""

import argparse
import glob
import io
import json
import os
import re
import shutil
import time

import numpy as np
import requests
import yaml
from flask import Flask, Response, jsonify, request, send_file
from PIL import Image

Image.MAX_IMAGE_PIXELS = None
app = Flask(__name__)
G = {}
PREVIEW_W = 2048
EXTS = (".JPG", ".jpg", ".jpeg", ".JPEG", ".png", ".PNG")


# ---- the building, in both frames -------------------------------------------
def load_level(base, level):
    """Vertices in drawing pixels (for the floorplan) and metres (for
    bearings), plus lanes — building.yaml and capture_plan.json each hold one
    half, matched through the vertices both name."""
    byaml = next(glob.iglob(os.path.join(base, "maps", "*.building.yaml")))
    b = yaml.safe_load(open(byaml))
    plan = json.loads(open(next(glob.iglob(
        os.path.join(base, "worlds", "*", "capture_plan.json")))).read())
    BL = b["levels"][level]
    metres = {v["id"].split(".", 1)[1]: np.array([v["x"], v["y"]])
              for v in plan["levels"][level]["vertices"]}
    lanes = [(e["a"].split(".", 1)[1], e["b"].split(".", 1)[1])
             for e in plan["levels"][level]["edges"]]
    px_named = {v[3]: np.array(v[:2], float) for v in BL["vertices"]
                if len(v) > 3 and v[3]}
    both = sorted(set(px_named) & set(metres))
    fit = np.linalg.lstsq(
        np.c_[np.stack([px_named[k] for k in both]), np.ones(len(both))],
        np.stack([metres[k] for k in both]), rcond=None)[0]
    to_m = lambda p: (np.r_[np.asarray(p, float), 1.0] @ fit)
    # nav ids: the capture plan is the naming authority (v<i> for unnamed)
    nav = {}
    for v in plan["levels"][level]["vertices"]:
        short = v["id"].split(".", 1)[1]
        nav[short] = np.array([v["x"], v["y"]])
    # drawing position for each nav vertex: nearest building vertex in metres
    bpx = [(np.array(v[:2], float), to_m(v[:2])) for v in BL["vertices"]]
    verts = []
    for short, m in nav.items():
        px = min(bpx, key=lambda t: np.linalg.norm(t[1] - m))
        verts.append({"id": short, "x": float(m[0]), "y": float(m[1]),
                      "px": float(px[0][0]), "py": float(px[0][1])})
    drawing = os.path.join(os.path.dirname(byaml),
                           (BL.get("drawing") or {}).get("filename", ""))
    return verts, lanes, (drawing if os.path.isfile(drawing) else None)


def bearings_of(short):
    here = next((np.array([v["x"], v["y"]]) for v in G["verts"]
                 if v["id"] == short), None)
    if here is None:
        return []
    out = []
    for a, b in G["lanes"]:
        other = b if a == short else (a if b == short else None)
        if other is None:
            continue
        to = next((np.array([v["x"], v["y"]]) for v in G["verts"]
                   if v["id"] == other), None)
        if to is None:
            continue
        d = to - here
        out.append({"to": other,
                    "bearing": float(np.degrees(np.arctan2(d[1], d[0]))) % 360,
                    "metres": round(float(np.linalg.norm(d)), 2)})
    return sorted(out, key=lambda o: o["bearing"])


# ---- files -------------------------------------------------------------------
def ident(short, variant=""):
    return f"{G['level']}.{short}" + (f"@{variant}" if variant else "")


def pano_of(short, variant=""):
    stem = os.path.join(G["base"], "panos", ident(short, variant))
    for e in EXTS:
        if os.path.isfile(stem + e):
            return stem + e
    return None


def variants_of(short):
    names = [""] if pano_of(short) else []
    for p in glob.glob(os.path.join(G["base"], "panos",
                                    ident(short) + "@*")):
        n = os.path.splitext(os.path.basename(p))[0].split("@", 1)[1]
        if n and n not in names:
            names.append(n)
    return names


def applied_to(name):
    """Degrees already rolled into panos/<name>, or None if never aligned."""
    rec = os.path.join(G["base"], "panos", ".aligned", name + ".json")
    if os.path.isfile(rec):
        try:
            return float(json.loads(open(rec).read())["degrees"])
        except (OSError, ValueError, KeyError):
            return None
    return None


def built(short, variant=""):
    return os.path.isfile(os.path.join(
        G["base"], "splats", ident(short, variant), "world.ply"))


def preview_path(pano):
    d = os.path.join(G["base"], "panos", ".previews")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, os.path.splitext(os.path.basename(pano))[0] + ".jpg")
    if not os.path.isfile(p) or os.path.getmtime(p) < os.path.getmtime(pano):
        Image.open(pano).convert("RGB").resize(
            (PREVIEW_W, PREVIEW_W // 2), Image.LANCZOS).save(p, quality=88)
    return p


# ---- routes -------------------------------------------------------------------
@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(__file__), "index.html"))


@app.route("/floorplan.png")
def floorplan():
    return (send_file(G["drawing"]) if G["drawing"]
            else Response("no floorplan", status=404))


@app.route("/graph")
def graph():
    out = []
    for v in G["verts"]:
        s = v["id"]
        pano = pano_of(s)
        out.append({**v,
                    "pano": bool(pano),
                    "aligned": pano is not None
                        and applied_to(os.path.basename(pano)) is not None,
                    "built": built(s),
                    "variants": variants_of(s)})
    return jsonify(level=G["level"], verts=out)


@app.route("/lanes")
def lanes():
    short = request.args["id"]
    variant = request.args.get("variant", "")
    pano = pano_of(short, variant)
    return jsonify(bearings=bearings_of(short),
                   applied=applied_to(os.path.basename(pano)) if pano else None,
                   pano=bool(pano), built=built(short, variant))


@app.route("/preview")
def preview():
    pano = pano_of(request.args["id"], request.args.get("variant", ""))
    if not pano:
        return Response("no pano", status=404)
    return send_file(preview_path(pano))


@app.route("/upload", methods=["POST"])
def upload():
    short = request.form["id"]
    variant = str(request.form.get("variant") or "").strip()
    if variant and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", variant):
        return jsonify(ok=False, error="variant names are letters, digits, "
                                       "- and _ (up to 32)")
    f = request.files.get("pano")
    if f is None or not f.filename:
        return jsonify(ok=False, error="no file in the upload")
    try:
        im = Image.open(f.stream)
        im.load()
    except Exception:
        return jsonify(ok=False, error="that file is not an image")
    if abs(im.width / max(1, im.height) - 2.0) > 0.05:
        return jsonify(ok=False, error=f"{im.width}x{im.height} is not an "
                       "equirect (needs 2:1) — export the stitched 360, not "
                       "a lens view")
    stem = ident(short, variant)
    old = pano_of(short, variant)
    if old:
        kept = os.path.join(G["base"], "panos", ".replaced")
        os.makedirs(kept, exist_ok=True)
        shutil.move(old, os.path.join(
            kept, f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}"
                  f"{os.path.splitext(old)[1]}"))
    dest = os.path.join(G["base"], "panos", stem + ".JPG")
    im.convert("RGB").save(dest, quality=95)
    # A new photograph arrives un-turned: whatever alignment the OLD file had
    # is baked into pixels this file does not have.
    rec = os.path.join(G["base"], "panos", ".aligned",
                       os.path.basename(dest) + ".json")
    if os.path.isfile(rec):
        os.remove(rec)
    pv = os.path.join(G["base"], "panos", ".previews", stem + ".jpg")
    if os.path.isfile(pv):
        os.remove(pv)
    return jsonify(ok=True, saved=f"panos/{stem}.JPG",
                   replaced=bool(old),
                   message=f"{stem}: photo saved — now align it")


@app.route("/align", methods=["POST"])
def align():
    b = request.get_json(force=True) or {}
    short, degrees = b["id"], float(b.get("degrees") or 0)
    variant = str(b.get("variant") or "")
    pano = pano_of(short, variant)
    if not pano:
        return jsonify(ok=False, error="no panorama to align — upload first")
    im = np.asarray(Image.open(pano).convert("RGB"))
    # Negative on purpose: the preview shows content at longitude L appearing
    # at L + corr; np.roll by +shift moves it from L to L - delta. The aligner
    # shipped with the sign flipped once, and a panorama turned until it
    # looked right was saved turned the other way.
    shift = -int(round(degrees / 360.0 * im.shape[1]))
    Image.fromarray(np.roll(im, shift, axis=1)).save(pano, quality=95)
    done = os.path.join(G["base"], "panos", ".aligned")
    os.makedirs(done, exist_ok=True)
    total = ((applied_to(os.path.basename(pano)) or 0.0) + degrees) % 360.0
    with open(os.path.join(done, os.path.basename(pano) + ".json"), "w") as f:
        f.write(json.dumps({"degrees": round(total, 2),
                            "last": round(degrees, 2)}, indent=1))
    pv = os.path.join(G["base"], "panos", ".previews",
                      os.path.splitext(os.path.basename(pano))[0] + ".jpg")
    if os.path.isfile(pv):
        os.remove(pv)
    return jsonify(ok=True, rolled=shift,
                   message=f"aligned — rolled {abs(shift)} px")


@app.route("/generate", methods=["POST"])
def generate():
    b = request.get_json(force=True) or {}
    short = b["id"]
    variant = str(b.get("variant") or "")
    stem = ident(short, variant)
    pano = pano_of(short, variant)
    if not pano:
        return jsonify(ok=False, error="no panorama — upload one first")
    if applied_to(os.path.basename(pano)) is None:
        return jsonify(ok=False, error="align it first — the world inherits "
                       "the panorama's rotation, and regenerating is the only "
                       "fix afterwards")
    out = os.path.join(G["base"], "splats", stem)
    if os.path.isfile(os.path.join(out, "world.ply")) and not b.get("force"):
        return jsonify(ok=False, error=f"{stem} already has a world — delete "
                       "splats/" + stem + " (or pass force) to rebuild")
    os.makedirs(out, exist_ok=True)
    Image.open(pano).convert("RGB").save(os.path.join(out, "panorama.png"))
    # Straight to Prefect: deployment generate-world/dreamworld, the same one
    # `just generate` submits. The scene path is the GENERATOR's view of the
    # same tree.
    api = G["prefect"]
    try:
        r = requests.get(f"{api}/deployments/name/generate-world/dreamworld",
                         timeout=10)
        r.raise_for_status()
        dep = r.json()["id"]
        r = requests.post(f"{api}/deployments/{dep}/create_flow_run",
                          json={"parameters": {
                                    "scene": f"/workspace/projects/"
                                             f"{G['project']}/splats/{stem}",
                                    "gpus": G["gpus"], "steps": G["steps"]},
                                "state": {"type": "SCHEDULED"}},
                          timeout=10)
        r.raise_for_status()
        run = r.json()
    except requests.RequestException as e:
        return jsonify(ok=False, error=f"could not submit: {e}")
    return jsonify(ok=True, run=run.get("id"), name=run.get("name"),
                   message=f"generating {stem} (~20 min) — watch it at :4200",
                   watch=f"/runs/flow-run/{run.get('id')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--level", default="L11")
    ap.add_argument("--port", type=int, default=8089)
    a = ap.parse_args()
    base = os.path.abspath(a.base)
    verts, lanes, drawing = load_level(base, a.level)
    G.update(base=base, level=a.level, verts=verts, lanes=lanes,
             drawing=drawing, project=os.path.basename(base),
             prefect=os.environ.get("PREFECT_API_URL",
                                    "http://prefect:4200/api"),
             gpus=int(os.environ.get("GPUS", "4")),
             steps=int(os.environ.get("STEPS", "2000")))
    print(f"capture: {G['project']} {a.level} ({len(verts)} waypoints) "
          f"on :{a.port}", flush=True)
    app.run(host="0.0.0.0", port=a.port, threaded=True)


if __name__ == "__main__":
    main()
