"""Edge-crossing videos over HTTP, one running and the rest queued.

Wan 2.2 first+last-frame generation behind the same three endpoints the
splat generator wears, because the editor already speaks that shape:

    GET  /health              {"status": "ok", "busy": bool, "queued": N}
    GET  /status              the running job (scene = the crossing dir,
                              elapsed), the queue, the last finished job
    POST /generate            {"dir": "/projects/.../.crossings/<a>__<b>",
                              "prompt": "..."} — the dir must already hold
                              first.png and last.png; enqueued unless that
                              crossing is already running or waiting

The pipeline loads lazily on the first job (~2 minutes) and then stays in
VRAM — this card is its home. Success is judged the honest way:
crossing.mp4 exists when the job ends, with generate.log beside it.
"""

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = os.environ.get("DW_WAN_MODEL", "Wan-AI/Wan2.2-I2V-A14B-Diffusers")

STATE = {"busy": False, "scene": None, "started": 0.0, "done": None,
         "loaded": False}
QUEUE = []
LOCK = threading.Lock()

PIPE = None


def pipeline():
    global PIPE
    if PIPE is None:
        import torch
        from diffusers import WanImageToVideoPipeline
        print(f"loading {MODEL} …", flush=True)
        PIPE = WanImageToVideoPipeline.from_pretrained(
            MODEL, torch_dtype=torch.bfloat16)
        PIPE.to("cuda")
        STATE["loaded"] = True
        print("pipeline ready", flush=True)
    return PIPE


def run_job(scene: str, prompt: str) -> dict:
    d = Path(scene)
    log = d / "generate.log"
    try:
        import torch
        from diffusers.utils import export_to_video, load_image

        pipe = pipeline()
        first = load_image(str(d / "first.png"))
        last = load_image(str(d / "last.png"))
        negative = ("blurry, warped geometry, distortion, text artifacts, "
                    "jump cut, flicker, people")
        out = pipe(image=first, last_image=last, prompt=prompt,
                   negative_prompt=negative, height=480, width=832,
                   num_frames=81, num_inference_steps=40,
                   guidance_scale=3.5,
                   generator=torch.Generator("cpu").manual_seed(7)).frames[0]
        export_to_video(out, str(d / "crossing.mp4"), fps=16)
        log.write_text(f"ok — {len(out)} frames\n")
    except Exception:
        log.write_text(traceback.format_exc())
    ok = (d / "crossing.mp4").is_file()
    return {"scene": scene, "ok": ok,
            "seconds": round(time.time() - STATE["started"]),
            "error": None if ok else "failed — see generate.log"}


def worker() -> None:
    while True:
        with LOCK:
            job = QUEUE.pop(0) if QUEUE else None
            if job:
                STATE.update(busy=True, scene=job["dir"],
                             started=time.time())
        if job is None:
            time.sleep(1)
            continue
        result = run_job(job["dir"], job["prompt"])
        with LOCK:
            STATE.update(done=result, busy=False, scene=None)


threading.Thread(target=worker, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, doc):
        body = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok", "busy": STATE["busy"],
                                    "queued": len(QUEUE),
                                    "loaded": STATE["loaded"]})
        if self.path == "/status":
            with LOCK:
                doc = {k: STATE[k] for k in ("busy", "scene", "done",
                                             "loaded")}
                doc["queue"] = [j["dir"] for j in QUEUE]
            if doc["busy"]:
                doc["elapsed"] = round(time.time() - STATE["started"])
            return self._send(200, doc)
        self._send(404, {"error": "?"})

    def do_POST(self):
        if self.path != "/generate":
            return self._send(404, {"error": "?"})
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        scene = req.get("dir", "")
        prompt = req.get("prompt", "").strip()
        d = Path(scene)
        if not ((d / "first.png").is_file() and (d / "last.png").is_file()):
            return self._send(400, {"error": f"{scene} lacks first/last "
                                    f"frames"})
        if not prompt:
            return self._send(400, {"error": "no prompt"})
        with LOCK:
            if scene == STATE["scene"] or scene in (j["dir"] for j in QUEUE):
                return self._send(409, {"error": "that crossing is already "
                                        "running or queued"})
            QUEUE.append({"dir": scene, "prompt": prompt})
            position = len(QUEUE) + (1 if STATE["busy"] else 0)
        self._send(200, {"ok": True, "dir": scene, "position": position})


srv = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
print(f"wangen ready on :8000 — {MODEL}", flush=True)
srv.serve_forever()
