"""The editor's line to the qwen server — perspective panorama edits.

The server is main's, ported verbatim: Qwen-Image-Edit-2509 behind POST
/restyle. So is the flow around it, from main's pano editor: extract the
rectilinear view the user is FACING (same camera basis as the WebGL
viewer — forward +X, up +Z), let the model edit that undistorted photo,
reproject it back into the equirect, and composite only the pixels the
edit actually changed, faded toward the frustum edge so a heavily-redrawn
view doesn't paste a hard seam. Regions the instruction didn't touch stay
pixel-identical to the base.

One adaptation: the crop is taken at yaw = look - off, so an unsaved
alignment turn still edits exactly what is on screen. The working size is
main's 1536x768 — a variant is a look, decided at model resolution.
"""

import io
import math
import os

import numpy as np
import requests
from PIL import Image, ImageFilter

QWEN = os.environ.get("QWEN_URL", "http://qwen:8000")


def ready() -> bool:
    try:
        return (requests.get(f"{QWEN}/health", timeout=3)
                .json().get("status") == "ok")
    except (requests.RequestException, ValueError):
        return False


def _cam_basis(yaw, pitch):
    F = np.array([math.cos(pitch) * math.cos(yaw),
                  math.cos(pitch) * math.sin(yaw), math.sin(pitch)])
    R = np.cross(F, [0.0, 0.0, 1.0])
    R = R / (np.linalg.norm(R) or 1)
    U = np.cross(R, F)
    return F, R, U


def _bilinear(arr, u, vv, wrap_x=False):
    He, We = arr.shape[:2]
    u0 = np.floor(u).astype(int)
    v0 = np.floor(vv).astype(int)
    fu = (u - u0)[..., None]
    fv = (vv - v0)[..., None]
    if wrap_x:
        u0m, u1m = u0 % We, (u0 + 1) % We
    else:
        u0m, u1m = np.clip(u0, 0, We - 1), np.clip(u0 + 1, 0, We - 1)
    v0m, v1m = np.clip(v0, 0, He - 1), np.clip(v0 + 1, 0, He - 1)
    a, b = arr[v0m, u0m], arr[v0m, u1m]
    c, d = arr[v1m, u0m], arr[v1m, u1m]
    return (a * (1 - fu) + b * fu) * (1 - fv) + (c * (1 - fu) + d * fu) * fv


def _extract_perspective(equi, yaw, pitch, fov, Wp, Hp):
    arr = np.asarray(equi.convert("RGB")).astype(np.float32)
    He, We = arr.shape[:2]
    F, R, U = _cam_basis(yaw, pitch)
    t = math.tan(fov / 2)
    gx, gy = np.meshgrid(np.arange(Wp), np.arange(Hp))
    cx = (gx + 0.5 - 0.5 * Wp) / (0.5 * Wp)
    cy = ((Hp - 1 - gy) + 0.5 - 0.5 * Hp) / (0.5 * Wp)   # gl_FragCoord is y-up
    d = (F[None, None] + (cx * t)[..., None] * R[None, None]
         + (cy * t)[..., None] * U[None, None])
    d /= np.linalg.norm(d, axis=2, keepdims=True)
    lon = np.arctan2(d[..., 1], d[..., 0])
    lat = np.arcsin(np.clip(d[..., 2], -1, 1))
    u = (lon / (2 * math.pi) + 0.5) * We
    vv = (0.5 - lat / math.pi) * He
    return Image.fromarray(
        np.clip(_bilinear(arr, u, vv, wrap_x=True), 0, 255).astype(np.uint8))


def _reproject_into_equi(base, edited_crop, yaw, pitch, fov):
    """Sample the edited crop back onto every in-frustum equirect pixel;
    return (reproj, coverage)."""
    base_a = np.asarray(base.convert("RGB")).astype(np.float32)
    crop_a = np.asarray(edited_crop.convert("RGB")).astype(np.float32)
    Hp, Wp = crop_a.shape[:2]
    He, We = base_a.shape[:2]
    F, R, U = _cam_basis(yaw, pitch)
    t = math.tan(fov / 2)
    lon = ((np.arange(We) + 0.5) / We - 0.5) * 2 * math.pi
    lat = (0.5 - (np.arange(He) + 0.5) / He) * math.pi
    LON, LAT = np.meshgrid(lon, lat)
    dx = np.cos(LAT) * np.cos(LON)
    dy = np.cos(LAT) * np.sin(LON)
    dz = np.sin(LAT)
    cf = dx * F[0] + dy * F[1] + dz * F[2]
    cfx = np.where(cf > 1e-3, cf, 1.0)
    scx = (dx * R[0] + dy * R[1] + dz * R[2]) / cfx / t
    scy = (dx * U[0] + dy * U[1] + dz * U[2]) / cfx / t
    px = scx * 0.5 * Wp + 0.5 * Wp - 0.5
    py = (Hp - 1) - (scy * 0.5 * Wp + 0.5 * Hp - 0.5)
    inframe = ((cf > 1e-3) & (px >= 0) & (px <= Wp - 1)
               & (py >= 0) & (py <= Hp - 1))
    sampled = _bilinear(crop_a, np.clip(px, 0, Wp - 1), np.clip(py, 0, Hp - 1))
    reproj = base_a.copy()
    reproj[inframe] = sampled[inframe]
    return reproj, inframe.astype(np.float32)


def perspective_edit(src_path, prompt: str, view: dict, seed: int = 0):
    """(png bytes, "ok") on success, (None, why) otherwise."""
    base = Image.open(src_path).convert("RGB").resize((1536, 768))
    # the crop must match the screen: the viewer previews the pending
    # alignment turn, so the file is sampled at look minus that turn
    yaw = float(view["yaw"]) - math.radians(float(view.get("off", 0)))
    pitch = float(view["pitch"])
    # looser than main's clamps: the edit rectangle can name a sub-frustum
    # much narrower or taller than a whole viewport
    fov = min(max(float(view.get("fov", 1.4)), 0.2), 2.3)
    aspect = min(max(float(view.get("aspect", 0.66)), 0.3), 2.0)
    Wp = 1024
    Hp = int(round(Wp * aspect / 16) * 16)
    crop = _extract_perspective(base, yaw, pitch, fov, Wp, Hp)
    buf = io.BytesIO()
    crop.save(buf, "PNG")
    buf.seek(0)
    p = (f"This is a photo of a room interior. Edit it in place: {prompt}. "
         f"Keep the walls, floor, ceiling, windows, camera viewpoint and "
         f"lighting exactly as shown, changing only what this instruction "
         f"describes; render it sharp and photorealistic.")
    r = requests.post(
        f"{QWEN}/restyle",
        files=[("image", ("view.png", buf, "image/png"))],
        data={"prompt": p, "steps": 40, "guidance": 4.0, "seed": seed,
              "width": Wp, "height": Hp, "seamless": "false",
              "void_fill": "false"},
        timeout=1800)
    if r.status_code != 200 or len(r.content) < 1000:
        return None, f"qwen {r.status_code}"
    edited = Image.open(io.BytesIO(r.content)).convert("RGB").resize((Wp, Hp))
    reproj, cov = _reproject_into_equi(base, edited, yaw, pitch, fov)
    # object mask = where the edit changed pixels, grown + feathered ...
    diff = np.abs(reproj - np.asarray(base, np.float32)).max(axis=2)
    dm = Image.fromarray(((diff > 22) * 255).astype(np.uint8))
    dm = dm.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.GaussianBlur(4))
    # ... faded toward the view edge so a heavily-redrawn frustum doesn't
    # paste a hard seam
    cs = Image.fromarray((cov * 255).astype(np.uint8)).filter(
        ImageFilter.MinFilter(15)).filter(ImageFilter.GaussianBlur(18))
    m = (np.asarray(dm, np.float32) / 255
         * np.asarray(cs, np.float32) / 255 * 255).astype(np.uint8)
    mimg = Image.fromarray(m).filter(ImageFilter.GaussianBlur(3))
    if mimg.getextrema()[1] < 8:
        return None, ("no change (try rephrasing, or zoom so the target "
                      "fills the view)")
    out = io.BytesIO()
    Image.composite(Image.fromarray(np.clip(reproj, 0, 255).astype(np.uint8)),
                    base, mimg).save(out, "PNG")
    return out.getvalue(), "ok"
