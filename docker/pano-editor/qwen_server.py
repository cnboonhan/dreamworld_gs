"""Qwen-Image-Edit panorama restyle server (v6 of the panorama stack).

Qwen-Image-Edit-2509 (20B, Apache-2.0) edits with up to THREE input images
conditioned through attention. We spend the slots as:
  1. the sim panorama         — the edit base (structure/layout)
  2. the geometry wireframe   — exact depth-derived edges reinforcing layout
  3. a neighbour collage      — already-restyled nav-graph neighbours side by
     side, so adjacent views agree about shared walls/doors/materials
     (attention-level conditioning; the FLUX.1 stack's Redux embeddings could
     only transfer a style summary).

Runs bf16 (full precision) — needs ~45GB VRAM (H100/H200 class), one server
per card, a parallel fleet.

Endpoints
  GET  /health   -> {"status": "ok" | "loading"}
  POST /restyle  (multipart/form-data)
    image      : (file) sim panorama PNG                          [required]
    depth      : (file) 16-bit range panorama (mm) -> wireframe   [optional]
    reference  : (file) restyled neighbour PNG, repeatable (collaged) [optional]
    prompt     : (str)  instruction prompt
    steps      : (int)  denoising steps               (default 40)
    guidance   : (float) true_cfg_scale               (default 4.0)
    seed       : (int)  RNG seed                      (default 0)
    width,height : (int) output size                  (default 1536x768)
    seamless   : (bool) wrap-blend the 360 seam       (default true)
    -> image/png
"""

import io
import math
import os
import traceback

import cv2
import numpy as np
import torch
import uvicorn
from diffusers import QwenImageEditPlusPipeline
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response
from huggingface_hub import snapshot_download
from PIL import Image

MODEL = os.environ.get("QWEN_EDIT_MODEL", "Qwen/Qwen-Image-Edit-2509")
PORT = int(os.environ.get("PORT", "8000"))
DEFAULT_PROMPT = os.environ.get(
    "QWEN_EDIT_PROMPT",
    "Restyle image 1 (a flat-shaded interior render) into a photorealistic "
    "modern office interior. Keep every wall, door and opening exactly where "
    "image 1 and the wireframe in image 2 show them. Match the materials, "
    "floor, palette and lighting of the reference photos in image 3 exactly.",
)


def _local_snapshot(repo_id):
    if os.path.isdir(repo_id):
        return repo_id
    return snapshot_download(repo_id, local_files_only=True)


app = FastAPI()
pipe = None
_ready = False


@app.on_event("startup")
def load():
    global pipe, _ready
    print(f"Loading {MODEL} ...", flush=True)
    pipe = QwenImageEditPlusPipeline.from_pretrained(
        _local_snapshot(MODEL), torch_dtype=torch.bfloat16)
    pipe.to("cuda")
    _ready = True
    print("ready", flush=True)


@app.get("/health")
def health():
    return {"status": "ok" if _ready else "loading"}


def _geom_edges(depth_bytes, w, h, disc_frac=0.04, crease_thresh=0.05):
    """16-bit range panorama -> geometry wireframe (white on black): occlusion
    boundaries + creases from exact geometry. Sparse edges are projection-safe
    in equirect; dense depth is not."""
    raw = cv2.imdecode(np.frombuffer(depth_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise ValueError("could not decode depth PNG")
    if raw.ndim == 3:
        raw = raw[..., 0]
    z = raw.astype(np.float32) / 1000.0
    z = np.where(z > 0.05, z, 60.0)
    gx = np.abs(np.roll(z, -1, axis=1) - z)
    gy = np.abs(np.diff(z, axis=0, append=z[-1:, :]))
    disc = np.maximum(gx, gy) > disc_frac * z
    lap = np.abs(cv2.Laplacian(cv2.GaussianBlur(z, (5, 5), 0), cv2.CV_32F, ksize=5))
    crease = (lap / np.maximum(z, 0.3)) > crease_thresh
    edges = ((disc | crease) * 255).astype(np.uint8)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8))
    edges = cv2.resize(edges, (w, h), interpolation=cv2.INTER_AREA)
    edges = ((edges > 64) * 255).astype(np.uint8)
    return Image.fromarray(np.stack([edges] * 3, axis=-1))


def _fill_void(img, thresh=222, spread=8, fill=180):
    """Repaint near-uniform blinding-white pixels (a sim capture's empty sky
    and beyond-opening void) as flat mid-grey. Featureless white void is what
    triggers hallucinated windows/backlit glass in the restyle — a toned
    surface reads as wall/ceiling instead."""
    a = np.asarray(img).astype(np.int16)
    mn = a.min(-1)
    mx = a.max(-1)
    a[(mn > thresh) & (mx - mn < spread)] = fill
    return Image.fromarray(a.astype(np.uint8))


def _collage(images, w, h):
    """Stack up to 3 reference images side by side into one conditioning
    image (the pipeline takes max 3 inputs; slots 1-2 are spoken for)."""
    imgs = [im.resize((w // len(images), h)) for im in images[:3]]
    out = Image.new("RGB", (sum(i.width for i in imgs), h))
    x = 0
    for im in imgs:
        out.paste(im, (x, 0))
        x += im.width
    return out


def _seam_blend(a, b):
    a = np.asarray(a, np.float32)
    b = np.asarray(b, np.float32)
    w = a.shape[1]
    x = np.arange(w)
    t = np.clip(np.minimum(x, w - 1 - x) / max(1.0, w * 0.05), 0.0, 1.0)
    wa = (0.5 - 0.5 * np.cos(math.pi * t))[None, :, None]
    return Image.fromarray((a * wa + b * (1.0 - wa)).clip(0, 255).astype(np.uint8))


def _generate(cond_images, prompt, steps, guidance, seed, w, h):
    gen = torch.Generator("cpu").manual_seed(seed)
    return pipe(image=cond_images, prompt=prompt, negative_prompt=" ",
                true_cfg_scale=guidance, num_inference_steps=steps,
                width=w, height=h, generator=gen).images[0]


@app.post("/restyle")
async def restyle(
    image: UploadFile = File(...),
    depth: UploadFile = File(None),
    reference: list[UploadFile] | None = None,
    prompt: str = Form(DEFAULT_PROMPT),
    steps: int = Form(40),
    guidance: float = Form(4.0),
    seed: int = Form(0),
    width: int = Form(1536),
    height: int = Form(768),
    seamless: bool = Form(True),
    void_fill: bool = Form(False),
):
    try:
        w = max(16, round(width / 16) * 16)
        h = max(16, round(height / 16) * 16)
        src = Image.open(io.BytesIO(await image.read())).convert("RGB").resize((w, h))
        if void_fill:
            src = _fill_void(src)
        conds = [src]
        wire = []  # extra-cond role phrases, image 2 onward
        if depth is not None:
            conds.append(_geom_edges(await depth.read(), w, h))
            wire.append("an edge wireframe of image 1's exact geometry — the "
                        "output must follow it")
        if reference:
            refs = [Image.open(io.BytesIO(await r.read())).convert("RGB")
                    for r in reference]
            # References enter at HALF resolution (a quarter of the tokens):
            # full-size references visually similar to the target style
            # dominate attention and the model reproduces them instead of
            # editing image 1 (observed: BFS chains collapse to near-copies
            # of the anchor, style noise compounding view over view).
            rw, rh = w // 2, h // 2
            if depth is None and len(refs) > 1:
                # No wireframe -> a slot is free: the first reference (the
                # anchor) gets its own; only the rest collage.
                conds.append(refs[0].resize((rw, rh)))
                wire.append("a style reference only — copy its materials, "
                            "floor, palette and lighting, never its layout")
                refs = refs[1:]
            conds.append(_collage(refs, rw, rh))
            wire.append("a style reference only — copy its materials, floor, "
                        "palette and lighting, never its layout")
        # Multi-image edits need explicit roles or the model may reproduce a
        # reference instead of editing the base (observed: BFS chains collapse
        # to copies of the anchor). Composed HERE because only the server
        # knows which slot holds what.
        if wire:
            roles = " ".join(f"Image {i + 2} is {r}." for i, r in enumerate(wire))
            prompt = (f"{prompt} Image 1 is the picture to edit: keep its "
                      f"camera viewpoint and every wall and opening exactly "
                      f"where image 1 shows them, changing only its style and "
                      f"materials. {roles}")
        result = _generate(conds, prompt, steps, guidance, seed, w, h)

        if seamless:
            roll = w // 2
            # Roll only the panorama-aligned conds (src, and the wireframe if
            # present) — style references aren't spatially tied to image 1.
            n_al = 2 if depth is not None else 1
            conds_r = ([Image.fromarray(np.roll(np.asarray(c), roll, axis=1))
                        for c in conds[:n_al]] + conds[n_al:])
            res_r = _generate(conds_r, prompt, steps, guidance, seed, w, h)
            res_r = Image.fromarray(np.roll(np.asarray(res_r), -roll, axis=1))
            result = _seam_blend(result, res_r)

        buf = io.BytesIO()
        result.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")
    except Exception:
        tb = traceback.format_exc()
        print(tb, flush=True)
        return JSONResponse(status_code=500, content={"error": tb})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
