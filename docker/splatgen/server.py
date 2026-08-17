"""Splat jobs over HTTP, one running and the rest queued — main's
generator without Prefect.

The image is main's splat-generator unchanged; this wrapper replaces the
Prefect deployment. One job runs at a time — the stages want all four
cards to themselves, main's own concurrency limit — and submissions made
meanwhile wait in a FIFO the worker drains:

    GET  /health              {"status": "ok", "busy": bool, "queued": N}
    GET  /status              the running job (scene, stage, elapsed),
                              the queue, and the last finished job
    POST /generate            {"scene": "/projects/.../splat", "steps": N}
                              enqueued unless that scene is already
                              running or waiting; the scene dir must hold
                              panorama.png

The queue lives in memory: a restart forgets it, which is honest — the
jobs' inputs are all on disk and resubmission costs one click.

The job subprocess drives main's flow.py directly — the same six stages,
Prefect running them ephemerally — with the vLLM reached by service name.
Success is judged the honest way: world.ply exists when the process ends.
The full stage log lands in <scene>/generate.log beside the result.
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GPUS = int(os.environ.get("DW_GEN_NPROC", "4"))
LLM_ADDR = os.environ.get("DW_LLM_ADDR", "vlm")
LLM_PORT = os.environ.get("DW_LLM_PORT", "8000")

STAGES = ["1. trajectory planning", "2. trajectory rendering",
          "3. world expansion", "4. gaussian training data",
          "5. 3DGS training", "6. export"]

STATE = {"busy": False, "scene": None, "started": 0.0, "done": None}
QUEUE = []
LOCK = threading.Lock()


def stage_of(scene: str):
    try:
        txt = (Path(scene) / "generate.log").read_text(errors="ignore")
    except OSError:
        return None
    current = None
    for s in STAGES:
        if s in txt:
            current = s
    return current


def run_job(scene: str, steps: int) -> dict:
    code = ("import sys; sys.path.insert(0, '/opt'); "
            "from flow import generate_world; "
            f"generate_world({scene!r}, gpus={GPUS}, steps={steps}, "
            f"llm_addr={LLM_ADDR!r}, llm_port={LLM_PORT!r})")
    with open(Path(scene) / "generate.log", "w") as log:
        proc = subprocess.run(["python", "-c", code], cwd="/opt",
                              stdout=log, stderr=subprocess.STDOUT)
    ok = (Path(scene) / "world.ply").is_file()
    return {"scene": scene, "ok": ok,
            "seconds": round(time.time() - STATE["started"]),
            "error": None if ok else
            f"exit {proc.returncode} — see generate.log"}


def worker() -> None:
    while True:
        with LOCK:
            job = QUEUE.pop(0) if QUEUE else None
            if job:
                STATE.update(busy=True, scene=job["scene"],
                             started=time.time())
        if job is None:
            time.sleep(1)
            continue
        result = run_job(job["scene"], job["steps"])
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
                                    "queued": len(QUEUE)})
        if self.path == "/status":
            with LOCK:
                doc = {k: STATE[k] for k in ("busy", "scene", "done")}
                doc["queue"] = [j["scene"] for j in QUEUE]
            if doc["busy"] and doc["scene"]:
                doc["elapsed"] = round(time.time() - STATE["started"])
                doc["stage"] = stage_of(doc["scene"])
            return self._send(200, doc)
        self._send(404, {"error": "?"})

    def do_POST(self):
        if self.path != "/generate":
            return self._send(404, {"error": "?"})
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        scene = req.get("scene", "")
        steps = int(req.get("steps", 2000))
        if not (Path(scene) / "panorama.png").is_file():
            return self._send(400, {"error": f"{scene}/panorama.png missing"})
        with LOCK:
            if scene == STATE["scene"] or scene in (j["scene"]
                                                    for j in QUEUE):
                return self._send(409, {"error": "that scene is already "
                                        "running or queued", "scene": scene})
            QUEUE.append({"scene": scene, "steps": steps})
            position = len(QUEUE) + (1 if STATE["busy"] else 0)
        self._send(200, {"ok": True, "scene": scene, "position": position})


srv = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
print(f"splatgen ready on :8000 — {GPUS} gpus, vlm at "
      f"{LLM_ADDR}:{LLM_PORT}", flush=True)
srv.serve_forever()
