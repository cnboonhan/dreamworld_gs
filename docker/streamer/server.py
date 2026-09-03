"""The live layer: a streaming image-to-video model, re-seedable.

The dreamworld's other generators take a job and hand back a file. This one
holds a rollout open: seed it with an image and a prompt, and it streams
generated frames until it is seeded again. That is the whole trick behind
"animate what the walker is looking at" — the panorama crop the viewer is
showing IS the seed, so every rollout starts from ground truth and never
runs long enough to drift far from it.

    GET  /health   {"status": "ok"|"loading", "runner": ...}
    GET  /status   the live rollout: seeded at, prompt, blocks, fps
    POST /seed     {"image": "<base64 jpg/png>", "prompt": "...",
                    }  -> drop the cache and start again
    GET  /stream   multipart/x-mixed-replace MJPEG of the rollout

One rollout at a time, on purpose: the cache is per-rollout, so a second
concurrent viewer would need its own. Re-seeding is cheap by comparison —
the pipeline stays loaded and only the AR cache is rebuilt, which is what
makes turning the camera feel like a new stream rather than a new session.

The model steers no camera: it is plain image-to-video, so the view holds
because there is nothing to move it, and the panorama the viewer is
showing supplies the frame. The drift watchdog below still measures how
far the rollout has slid from that frame and rebuilds the cache when it
goes too far.
"""
import base64
import collections
import io
import json
import math
import os
import inspect
import threading
import time
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import torch
from PIL import Image

RUNNER = os.environ.get("DW_STREAMER_RUNNER",
                        "causal-forcing-wan2.1-i2v-1.3b-framewise")
WIDTH = int(os.environ.get("DW_STREAMER_WIDTH", "832"))
HEIGHT = int(os.environ.get("DW_STREAMER_HEIGHT", "480"))
JPEG_Q = int(os.environ.get("DW_STREAMER_JPEG_Q", "80"))
# The camera is held still by the POSE track (identity every frame, which
# the encoder turns into zero relative motion and anchors across blocks).
# The video model still carries its own motion prior, though, and the
# pipeline takes no negative prompt — so the only other lever is to say it
# in the positive one, on every rollout, whatever the viewer typed.
STILL = os.environ.get(
    "DW_STREAMER_STILL",
    "static locked-off camera on a tripod, fixed viewpoint, "
    "no camera movement, no panning, no zooming; only the scene itself moves")
# Re-anchor: rebuild the cache from the SAME seed every N FRAMES, so drift
# cannot accumulate past a few seconds. Counted in frames, not blocks,
# because a framewise runner emits ONE frame per block and a block-based
# count then rebuilt the cache twice a second — which costs far more than
# the drift it was preventing.
REANCHOR = int(os.environ.get("DW_STREAMER_REANCHOR", "480"))
# and a wall-clock bound on the same thing, because frames are not
# seconds: the runner's rate moves with the prompt and the card, so a
# frame count alone makes the loop's LENGTH drift even when the picture
# does not. Whichever bound falls first sends the rollout back to the
# photograph, so what you watch is a ~15s loop of the view you are
# standing in rather than an ever-lengthening improvisation away from it.
LOOP_S = float(os.environ.get("DW_STREAMER_LOOP_S", "15"))
# The pose track is a perfect "do not move" signal — relative poses are
# frame-to-frame, so identity everywhere is exactly zero motion — but the
# video model still has a prior of its own and wanders. So close the loop:
# measure how far the newest frame has slid from the SEED and, past this
# many pixels, rebuild the cache from the seed. Drift then cannot exceed
# the threshold, because exceeding it is what triggers the snap back.
DRIFT_PX = float(os.environ.get("DW_STREAMER_DRIFT_PX", "48"))

STATE = {"loaded": False, "seeded_at": 0.0, "prompt": "", "blocks": 0,
         "fps": 0.0, "error": None, "drift": 0.0, "reanchors": 0,
         "loops": 0, "loop_s": 0.0, "warm": False, "warm_s": None}
LOCK = threading.Lock()
# Frames leave the model in BLOCKS — a dozen at once, then a pause while
# the next block computes — and handing them to the browser as they land
# plays as a burst followed by a freeze. So the model thread only queues
# them, and a publisher thread releases them at a steady cadence it
# adapts from the backlog. The JPEG encoding moves there too, off the
# thread that could be generating.
PENDING_FRAMES = collections.deque()
PACE = threading.Condition()
MAX_BACKLOG = 48          # a couple of seconds; older frames are dropped
# `seq` tags every frame with the rollout that produced it, so a
# stream opened for a NEW seed can never be handed the last frame
# of the old one — which is what made "warming up" flash the
# previous location before the first real frame arrived.
FRAME = {"jpeg": None, "n": 0, "seq": 0}
NEW_FRAME = threading.Condition()
# the pending seed, picked up by the model thread at a block boundary
PENDING = {"image": None, "prompt": None, "seq": 0}


def build_pipeline():
    from flashdreams.configs.runner_configs import all_runners
    cfg = all_runners().get(RUNNER)
    if cfg is None:
        raise SystemExit(f"unknown runner {RUNNER!r}; have "
                         f"{sorted(all_runners())}")
    pcfg = cfg.pipeline
    # the causal-forcing pipelines ship with enable_sync_and_profile=True,
    # whose own comment in upstream reads "Warning: This will slow down the
    # e2e latency" — it synchronises the device every step to time it. We
    # are serving, not benchmarking.
    if getattr(pcfg, "enable_sync_and_profile", False):
        from flashdreams.infra.config import derive_config
        pcfg = derive_config(pcfg, enable_sync_and_profile=False)
        print("disabled per-step sync/profiling", flush=True)
    pipe = pcfg.setup().to("cuda").eval()
    return pipe


@dataclass
class CameraControlInput:
    """What a camera-steering pipeline reads each block. Mirrors
    cam2v.session's dataclass so the pipeline sees the shape it expects
    without this server depending on the interactive app."""
    intrinsics: torch.Tensor
    poses: torch.Tensor
    world_scale: float


def still_camera(frames, device):
    """The camera held exactly still: an identity pose for every frame.

    Relative poses are computed frame-to-frame upstream, so identity
    everywhere is precisely zero rotation and zero translation — the same
    thing the action mapping emits for "no keys held". Intrinsics are the
    frame's own, and world_scale is moot when translation is nil."""
    fx = 0.5 * WIDTH / math.tan(0.6)
    K = torch.tensor([[fx, fx, WIDTH / 2.0, HEIGHT / 2.0]],
                     device=device, dtype=torch.float32).repeat(frames, 1)
    poses = torch.eye(4, device=device,
                      dtype=torch.float32).unsqueeze(0).repeat(frames, 1, 1)
    return CameraControlInput(intrinsics=K, poses=poses, world_scale=1.0)


def steers_camera(pipe):
    """Whether this pipeline takes a camera track at all.

    LingBot is camera-controllable and wants a pose per frame; the causal
    Wan streamers are plain image-to-video and take no camera input. Asked
    by signature rather than by runner name, so a new runner slug needs no
    change here."""
    try:
        return "input" in inspect.signature(pipe.generate).parameters
    except (TypeError, ValueError):
        return True


def seed_tensor(raw: bytes, device):
    """The seed image, sized to the model's frame and scaled to [-1, 1]
    CHW — the range every Wan-family checkpoint here works in. bfloat16,
    like the reference app: the weights are bf16 and a float32 frame
    fails at the first convolution's bias."""
    im = Image.open(io.BytesIO(raw)).convert("RGB").resize(
        (WIDTH, HEIGHT), Image.LANCZOS)
    a = torch.from_numpy(np.asarray(im)).to(device=device, dtype=torch.float32)
    return (a.permute(2, 0, 1) / 127.5 - 1.0).unsqueeze(0).to(torch.bfloat16)


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


def publish(frames, seq):
    """Queue a block's frames for paced release, and return the last
    frame's luma so the loop can measure its drift."""
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
        with PACE:
            PENDING_FRAMES.append((rgb, seq))
            while len(PENDING_FRAMES) > MAX_BACKLOG:
                PENDING_FRAMES.popleft()
            PACE.notify_all()
    return last


def pacer():
    """Release queued frames evenly, at the rate they are being made.

    The interval tracks the backlog rather than a fixed target: a growing
    queue means the model is ahead, so drain faster; an empty one means it
    is behind, so stretch. Bounded either side, because neither a stall
    nor a flood is worth showing."""
    interval = 1.0 / 8
    while True:
        with PACE:
            if not PENDING_FRAMES:
                PACE.wait(timeout=1.0)
                continue
            rgb, seq = PENDING_FRAMES.popleft()
            backlog = len(PENDING_FRAMES)
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, "JPEG", quality=JPEG_Q)
        with NEW_FRAME:
            FRAME["jpeg"] = buf.getvalue()
            FRAME["n"] += 1
            FRAME["seq"] = seq
            NEW_FRAME.notify_all()
        if backlog > 12:
            interval *= 0.82          # the model is ahead of the viewer
        elif backlog < 3:
            interval *= 1.15          # do not outrun what is being made
        interval = min(max(interval, 1.0 / 24), 0.4)
        STATE["pace_fps"] = round(1.0 / interval, 1)
        STATE["backlog"] = backlog
        time.sleep(interval)


def warm_up(pipe, cam):
    """Run a rollout nobody watches, so the first one somebody does is fast.

    Even with the kernel caches on disk there is ~19s of first-generate
    warmup — allocator, cuda graphs, the shapes this runner only meets
    once it is actually generating. Paying it here, against a flat grey
    frame, means the container reports ready when it IS ready rather than
    when it has merely finished loading weights. The frames are thrown
    away: nothing from this reaches a viewer."""
    t0 = time.time()
    try:
        buf = io.BytesIO()
        Image.new("RGB", (WIDTH, HEIGHT), (32, 36, 44)).save(
            buf, "JPEG", quality=90)
        with torch.inference_mode():
            cache = pipe.initialize_cache(
                text=[STILL], image=seed_tensor(buf.getvalue(), "cuda"))
            # two blocks: the first block and the steady-state block are
            # different code paths, and both are wanted warm
            for step in range(2):
                n = int(pipe.get_num_output_frames(step))
                (pipe.generate(autoregressive_index=step, cache=cache,
                               input=still_camera(n, "cuda"))
                 if cam else
                 pipe.generate(autoregressive_index=step, cache=cache))
                pipe.finalize(autoregressive_index=step, cache=cache)
        del cache
        torch.cuda.empty_cache()
        STATE["warm_s"] = round(time.time() - t0, 1)
    except Exception as e:                                     # noqa: BLE001
        # a failed warmup is not a failed server: say so and serve anyway
        print(f"warmup failed after {time.time() - t0:.1f}s: "
              f"{type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
    STATE["warm"] = True


def model_loop():
    """Load once, then roll forever: seed, generate blocks, re-seed."""
    try:
        pipe = build_pipeline()
    except Exception as e:                                     # noqa: BLE001
        STATE["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        return
    cam = steers_camera(pipe)
    STATE["loaded"] = True
    STATE["camera"] = cam
    print(f"streamer loaded — {RUNNER}, {WIDTH}x{HEIGHT}, "
          f"camera: {'held still' if cam else 'none to steer'}; warming",
          flush=True)
    warm_up(pipe, cam)
    print(f"streamer ready on :8000 — warmed in {STATE.get('warm_s')}s",
          flush=True)

    cache, step, seq, t0, done = None, 0, -1, 0.0, 0
    anchored_at, blk = 0.0, 0.0
    while True:
        with LOCK:
            pend = dict(PENDING)
        if pend["seq"] != seq and pend["image"] is not None:
            # a new view or a new prompt: the cache belongs to the old one
            seq, cache, step, done, t0 = pend["seq"], None, 0, 0, time.time()
            anchor = None
            STATE.update(seeded_at=t0, prompt=pend["prompt"] or "", blocks=0,
                         drift=0.0, reanchors=0, loops=0)
        if seq < 0 or pend["image"] is None:
            time.sleep(0.05)
            continue
        try:
            # the whole rollout lives in inference mode: the cache the
            # pipeline builds holds inference tensors, and updating them
            # from outside that mode is refused a few blocks in — long
            # enough to look like a mid-stream failure rather than a
            # missing context manager
            if cache is not None and (
                    (REANCHOR and done >= REANCHOR)
                    or (LOOP_S and time.time() - anchored_at + blk / 2
                        >= LOOP_S)):
                cache, step, done = None, 0, 0     # back to the photograph
                STATE["loops"] = STATE.get("loops", 0) + 1
            with torch.inference_mode():
                if cache is None:
                    if anchored_at:
                        STATE["loop_s"] = round(time.time() - anchored_at, 1)
                    anchored_at = time.time()
                    text = ", ".join(x for x in (pend["prompt"], STILL) if x)
                    cache = pipe.initialize_cache(
                        text=[text],
                        image=seed_tensor(pend["image"], "cuda"))
                n = int(pipe.get_num_output_frames(step))
                tblk = time.time()
                frames = (pipe.generate(autoregressive_index=step,
                                        cache=cache,
                                        input=still_camera(n, "cuda"))
                          if cam else
                          pipe.generate(autoregressive_index=step,
                                        cache=cache))
                pipe.finalize(autoregressive_index=step, cache=cache)
                blk = time.time() - tblk
            luma = publish(frames, seq)
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
                # ok means READY TO GENERATE, not merely holding
                # weights: a viewer that seeds the moment it sees "ok"
                # should not then wait out the first compile
                "status": ("ok" if STATE["warm"]
                           else "warming" if STATE["loaded"] else "loading"),
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
        want = 0
        if "?" in self.path:
            from urllib.parse import parse_qs
            want = int((parse_qs(self.path.split("?", 1)[1]).get("s")
                        or ["0"])[0] or 0)
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=dwframe")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        last, opened, sent = -1, time.time(), 0
        try:
            while True:
                with NEW_FRAME:
                    # wake for a frame of OUR rollout, or for the news that
                    # ours has been superseded
                    if not NEW_FRAME.wait_for(
                            lambda: (want and FRAME["seq"] > want)
                                    or (FRAME["n"] != last and FRAME["jpeg"]
                                        and (not want or
                                             FRAME["seq"] == want)),
                            timeout=30):
                        # a rollout that never arrives must not hold this
                        # connection for the life of the process: the
                        # browser only gets a handful per host, and a
                        # stuck one blocks the next /seed
                        if not sent and time.time() - opened > 180:
                            return
                        continue
                    # the view moved on. This response belongs to a rollout
                    # nobody is watching any more; ending it frees the
                    # socket instead of parking a thread on a condition
                    # that can never again be true.
                    if want and FRAME["seq"] > want:
                        return
                    jpeg, last = FRAME["jpeg"], FRAME["n"]
                sent += 1
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
                                   seq=PENDING["seq"] + 1)
        # the queue still holds the old rollout's frames. The stream
        # filters them out by seq so none reach the screen, but paced
        # release would spend seconds draining them before the new
        # rollout's first frame got its turn — which reads as the prompt
        # being ignored. Drop them.
        with PACE:
            PENDING_FRAMES.clear()
        return self._send(200, {"ok": True, "seq": PENDING["seq"]})


threading.Thread(target=model_loop, daemon=True).start()
threading.Thread(target=pacer, daemon=True).start()
ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
