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

STATE = {"loaded": False, "seeded_at": 0.0, "prompt": "", "blocks": 0,
         "fps": 0.0, "error": None}
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
    """The seed image, letterboxed to the model's frame and scaled to
    [-1, 1] CHW — the range every Wan-family checkpoint here works in."""
    im = Image.open(io.BytesIO(raw)).convert("RGB").resize(
        (WIDTH, HEIGHT), Image.LANCZOS)
    a = torch.from_numpy(np.asarray(im)).to(device=device, dtype=torch.float32)
    return (a.permute(2, 0, 1) / 127.5 - 1.0).unsqueeze(0)


def still_camera(frames, fov, device):
    """A camera that does not move: the viewer owns panning (it turns the
    panorama and re-seeds), so the rollout's job is to animate the scene,
    not to fly through it. Intrinsics come from the crop's own field of
    view, so the generated motion matches the framing the walker sees."""
    fx = 0.5 * WIDTH / math.tan(fov / 2.0)
    K = torch.tensor([[fx, fx, WIDTH / 2.0, HEIGHT / 2.0]],
                     device=device, dtype=torch.float32).repeat(frames, 1)
    poses = torch.eye(4, device=device, dtype=torch.float32)
    return CameraControlInput(intrinsics=K,
                              poses=poses.unsqueeze(0).repeat(frames, 1, 1),
                              world_scale=1.0)


def publish(frames):
    """Hand finished frames to whoever is streaming, newest wins."""
    for f in frames:
        a = f.detach().float().clamp(-1, 1)
        if a.ndim == 4:
            a = a[0]
        if a.shape[0] in (1, 3):          # CHW -> HWC
            a = a.permute(1, 2, 0)
        arr = ((a + 1.0) * 127.5).round().clamp(0, 255).to(torch.uint8)
        buf = io.BytesIO()
        Image.fromarray(arr.cpu().numpy()).save(buf, "JPEG", quality=JPEG_Q)
        with NEW_FRAME:
            FRAME["jpeg"] = buf.getvalue()
            FRAME["n"] += 1
            NEW_FRAME.notify_all()


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
            STATE.update(seeded_at=t0, prompt=pend["prompt"] or "", blocks=0)
        if seq < 0 or pend["image"] is None:
            time.sleep(0.05)
            continue
        try:
            if cache is None:
                cache = pipe.initialize_cache(
                    text=[pend["prompt"] or ""],
                    image=seed_tensor(pend["image"], "cuda"))
            n = int(pipe.get_num_output_frames(step))
            frames = pipe.generate(
                autoregressive_index=step, cache=cache,
                input=still_camera(n, float(pend["fov"] or 1.2), "cuda"))
            pipe.finalize(autoregressive_index=step, cache=cache)
            publish(frames)
            step += 1
            done += n
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
