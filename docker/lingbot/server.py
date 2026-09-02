"""LingBot-World as a re-seedable live stream.

The dreamworld's other generators take a job and hand back a file. This one
holds a rollout open: seed it with an image and a prompt, and it streams
generated frames until it is seeded again. That is the whole trick behind
"animate what the walker is looking at" — the panorama crop the viewer is
showing IS the seed, so every rollout starts from ground truth and never
runs long enough to drift far from it.

    GET  /health   {"status": "ok"|"loading", "runner": ...}
    GET  /status   the live rollout: seeded at, prompt, blocks, fps
    POST /seed     {"image": "<base64 jpg/png>", "prompt": "...",
                    "fov": 1.2}  -> drop the cache and start again
    GET  /stream   multipart/x-mixed-replace MJPEG of the rollout

One rollout at a time, on purpose: the model holds ~120GB and the cache is
per-rollout, so a second concurrent viewer would not fit. Re-seeding is
cheap by comparison — the pipeline stays loaded and only the AR cache is
rebuilt, which is what makes turning the camera feel like a new stream
rather than a new session.
"""
import base64
import io
import json
import math
import os
import threading
import time
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import torch
from PIL import Image

RUNNER = os.environ.get("DW_LINGBOT_RUNNER",
                        "lingbot-world-fast-taehv-window15-sink3")
WIDTH = int(os.environ.get("DW_LINGBOT_WIDTH", "832"))
HEIGHT = int(os.environ.get("DW_LINGBOT_HEIGHT", "464"))
JPEG_Q = int(os.environ.get("DW_LINGBOT_JPEG_Q", "80"))
# The camera is held still by the POSE track (identity every frame, which
# the encoder turns into zero relative motion and anchors across blocks).
# The video model still carries its own motion prior, though, and the
# pipeline takes no negative prompt — so the only other lever is to say it
# in the positive one, on every rollout, whatever the viewer typed.
STILL = os.environ.get(
    "DW_LINGBOT_STILL",
    "static locked-off camera on a tripod, fixed viewpoint, "
    "no camera movement, no panning, no zooming; only the scene itself moves")
# Re-anchor: rebuild the cache from the SAME seed every N blocks, so drift
# cannot accumulate past a few seconds. The camera has not moved, so the
# seed is still the right view and the snap back is nearly invisible.
REANCHOR = int(os.environ.get("DW_LINGBOT_REANCHOR", "8"))
# A null pose track asks the model to hold perfectly still, and it drifts
# anyway — a camera-controllable model follows a DEFINITE trajectory far
# better than an absent one. So give it a small sway: a few degrees left
# and right about the up axis, returning through centre every period, so
# the net movement over a cycle is zero and the view never wanders off.
# Set SWAY_DEG=0 for a hard-locked camera.
SWAY_DEG = float(os.environ.get("DW_LINGBOT_SWAY_DEG", "0"))
SWAY_FRAMES = int(os.environ.get("DW_LINGBOT_SWAY_FRAMES", "64"))
# The pose track is a perfect "do not move" signal — relative poses are
# frame-to-frame, so identity everywhere is exactly zero motion — but the
# video model still has a prior of its own and wanders. So close the loop:
# measure how far the newest frame has slid from the SEED and, past this
# many pixels, rebuild the cache from the seed. Drift then cannot exceed
# the threshold, because exceeding it is what triggers the snap back.
DRIFT_PX = float(os.environ.get("DW_LINGBOT_DRIFT_PX", "10"))

STATE = {"loaded": False, "seeded_at": 0.0, "prompt": "", "blocks": 0,
         "fps": 0.0, "error": None, "drift": 0.0, "reanchors": 0}
LOCK = threading.Lock()
FRAME = {"jpeg": None, "n": 0}
NEW_FRAME = threading.Condition()
# the pending seed, picked up by the model thread at a block boundary
PENDING = {"image": None, "prompt": None, "fov": 1.2, "seq": 0}


@dataclass
class CameraControlInput:
    """What the pipeline reads off the camera each block. Mirrors
    cam2v.session's dataclass so the pipeline sees the shape it expects
    without this server depending on the interactive app."""
    intrinsics: torch.Tensor
    poses: torch.Tensor
    world_scale: float


def build_pipeline():
    from flashdreams.configs.runner_configs import all_runners
    cfg = all_runners().get(RUNNER)
    if cfg is None:
        raise SystemExit(f"unknown runner {RUNNER!r}")
    pipe = cfg.pipeline.setup().to("cuda").eval()
    return pipe


def seed_tensor(raw: bytes, device):
    """The seed image, sized to the model's frame and scaled to [-1, 1]
    CHW — the range every Wan-family checkpoint here works in. bfloat16,
    like the reference app: the weights are bf16 and a float32 frame
    fails at the first convolution's bias."""
    im = Image.open(io.BytesIO(raw)).convert("RGB").resize(
        (WIDTH, HEIGHT), Image.LANCZOS)
    a = torch.from_numpy(np.asarray(im)).to(device=device, dtype=torch.float32)
    return (a.permute(2, 0, 1) / 127.5 - 1.0).unsqueeze(0).to(torch.bfloat16)


def sway_camera(frames, fov, device, t0):
    """The camera the rollout is told to hold: no translation ever, and a
    gentle yaw that swings SWAY_DEG each way and back.

    The viewer owns real panning — it turns the PANORAMA and re-seeds — so
    the rollout never needs to travel. `t0` is the frame index the rollout
    has reached, which keeps the sway continuous across autoregressive
    blocks (the encoder takes each chunk relative to the last pose, so a
    phase jump at a block boundary would read as a lurch).

    Intrinsics come from the crop's own field of view, so what the model
    generates matches the framing the walker is actually looking at."""
    fx = 0.5 * WIDTH / math.tan(fov / 2.0)
    K = torch.tensor([[fx, fx, WIDTH / 2.0, HEIGHT / 2.0]],
                     device=device, dtype=torch.float32).repeat(frames, 1)
    poses = torch.eye(4, device=device,
                      dtype=torch.float32).unsqueeze(0).repeat(frames, 1, 1)
    if SWAY_DEG > 0 and SWAY_FRAMES > 0:
        i = torch.arange(t0, t0 + frames, device=device, dtype=torch.float32)
        a = math.radians(SWAY_DEG) * torch.sin(2 * math.pi * i / SWAY_FRAMES)
        c, s_ = torch.cos(a), torch.sin(a)
        # yaw about the up axis of the OpenCV camera frame (x right,
        # y down, z forward): translation stays exactly zero
        poses[:, 0, 0], poses[:, 0, 2] = c, s_
        poses[:, 2, 0], poses[:, 2, 2] = -s_, c
    return CameraControlInput(intrinsics=K, poses=poses, world_scale=1.0)


def _gray_small(arr):
    """Quarter-scale luma, the cheap basis for the drift measurement."""
    a = arr[::4, ::4, :].astype(np.float32)
    return a[:, :, 0] * 0.299 + a[:, :, 1] * 0.587 + a[:, :, 2] * 0.114


def drift_of(a, b):
    """Global displacement between two frames, by phase correlation, in
    full-resolution pixels. Robust to the content changing underneath —
    it is the SHIFT we care about, not the difference."""
    A, B = np.fft.rfft2(a), np.fft.rfft2(b)
    C = A * np.conj(B)
    C /= np.maximum(np.abs(C), 1e-9)
    r = np.fft.irfft2(C, s=a.shape)
    py, pxi = np.unravel_index(int(np.argmax(r)), r.shape)
    dy = py - a.shape[0] if py > a.shape[0] // 2 else py
    dx = pxi - a.shape[1] if pxi > a.shape[1] // 2 else pxi
    return abs(dx) * 4.0, abs(dy) * 4.0


def publish(frames):
    """Hand finished frames to whoever is streaming, newest wins, and
    return the last frame's luma so the loop can measure its drift."""
    last = None
    for f in frames:
        a = f.detach().float().clamp(-1, 1)
        if a.ndim == 4:
            a = a[0]
        if a.shape[0] in (1, 3):          # CHW -> HWC
            a = a.permute(1, 2, 0)
        arr = ((a + 1.0) * 127.5).round().clamp(0, 255).to(torch.uint8)
        rgb = arr.cpu().numpy()
        last = _gray_small(rgb)
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, "JPEG", quality=JPEG_Q)
        with NEW_FRAME:
            FRAME["jpeg"] = buf.getvalue()
            FRAME["n"] += 1
            NEW_FRAME.notify_all()
    return last


def model_loop():
    """Load once, then roll forever: seed, generate blocks, re-seed."""
    try:
        pipe = build_pipeline()
    except Exception as e:                                     # noqa: BLE001
        STATE["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        return
    STATE["loaded"] = True
    print(f"lingbot ready on :8000 — runner {RUNNER}, {WIDTH}x{HEIGHT}",
          flush=True)

    cache, step, seq, t0, done = None, 0, -1, 0.0, 0
    while True:
        with LOCK:
            pend = dict(PENDING)
        if pend["seq"] != seq and pend["image"] is not None:
            # a new view or a new prompt: the cache belongs to the old one
            seq, cache, step, done, t0 = pend["seq"], None, 0, 0, time.time()
            anchor = None
            STATE.update(seeded_at=t0, prompt=pend["prompt"] or "", blocks=0,
                         drift=0.0, reanchors=0)
        if seq < 0 or pend["image"] is None:
            time.sleep(0.05)
            continue
        try:
            # the whole rollout lives in inference mode: the cache the
            # pipeline builds holds inference tensors, and updating them
            # from outside that mode is refused a few blocks in — long
            # enough to look like a mid-stream failure rather than a
            # missing context manager
            if REANCHOR and step and step % REANCHOR == 0:
                cache, step = None, 0          # back to the photograph
            with torch.inference_mode():
                if cache is None:
                    text = ", ".join(x for x in (pend["prompt"], STILL) if x)
                    cache = pipe.initialize_cache(
                        text=[text],
                        image=seed_tensor(pend["image"], "cuda"))
                n = int(pipe.get_num_output_frames(step))
                frames = pipe.generate(
                    autoregressive_index=step, cache=cache,
                    input=sway_camera(n, float(pend["fov"] or 1.2),
                                      "cuda", done))
                pipe.finalize(autoregressive_index=step, cache=cache)
            luma = publish(frames)
            step += 1
            done += n
            if anchor is None:
                anchor = _gray_small(np.asarray(Image.open(
                    io.BytesIO(pend["image"])).convert("RGB").resize(
                        (WIDTH, HEIGHT), Image.LANCZOS)))
            if luma is not None:
                dx, dy = drift_of(anchor, luma)
                STATE["drift"] = round(max(dx, dy), 1)
                if DRIFT_PX and max(dx, dy) > DRIFT_PX:
                    # slid too far from the photograph — start again from it
                    cache, step = None, 0
                    STATE["reanchors"] = STATE.get("reanchors", 0) + 1
            STATE.update(blocks=step,
                         fps=round(done / max(time.time() - t0, 1e-3), 2))
        except Exception as e:                                 # noqa: BLE001
            STATE["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            cache, step = None, 0
            time.sleep(1.0)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, doc):
        body = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, {
                "status": "ok" if STATE["loaded"] else "loading",
                "runner": RUNNER, "error": STATE["error"]})
        if self.path.startswith("/status"):
            return self._send(200, {**STATE, "frames": FRAME["n"]})
        if self.path.startswith("/seed.jpg"):
            # the last conditioning frame, so "what is it actually seeded
            # with" is a question you can answer by looking
            with LOCK:
                raw = PENDING["image"]
            if not raw:
                return self._send(404, {"error": "nothing seeded yet"})
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path.startswith("/stream"):
            return self.stream()
        self._send(404, {"error": "?"})

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=dwframe")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        last = -1
        try:
            while True:
                with NEW_FRAME:
                    if not NEW_FRAME.wait_for(
                            lambda: FRAME["n"] != last and FRAME["jpeg"],
                            timeout=30):
                        continue
                    jpeg, last = FRAME["jpeg"], FRAME["n"]
                self.wfile.write(b"--dwframe\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: "
                                 + str(len(jpeg)).encode() + b"\r\n\r\n"
                                 + jpeg + b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass          # the viewer navigated away; the rollout continues

    def do_POST(self):
        if not self.path.startswith("/seed"):
            return self._send(404, {"error": "?"})
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        img = req.get("image") or ""
        if img.startswith("data:"):
            img = img.split(",", 1)[-1]
        try:
            raw = base64.b64decode(img) if img else None
        except Exception:                                      # noqa: BLE001
            return self._send(400, {"error": "image is not base64"})
        if not raw:
            return self._send(400, {"error": "need an image"})
        with LOCK:
            PENDING.update(image=raw, prompt=str(req.get("prompt") or ""),
                           fov=float(req.get("fov") or 1.2),
                           seq=PENDING["seq"] + 1)
        return self._send(200, {"ok": True, "seq": PENDING["seq"]})


threading.Thread(target=model_loop, daemon=True).start()
ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
