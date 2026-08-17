"""One splat job at a time, over HTTP — main's generator without Prefect.

The image is main's splat-generator unchanged; this wrapper replaces the
Prefect deployment with three endpoints, because v2 has no Prefect server
and one building grows one splat at a time anyway:

    GET  /health              {"status": "ok", "busy": bool}
    GET  /status              the running job: scene, stage, elapsed — or
                              the last finished one
    POST /generate            {"scene": "/projects/.../splat", "steps": N}
                              409 while a job runs; the scene dir must
                              already hold panorama.png

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


def run_job(scene: str, steps: int) -> None:
    code = ("import sys; sys.path.insert(0, '/opt'); "
            "from flow import generate_world; "
            f"generate_world({scene!r}, gpus={GPUS}, steps={steps}, "
            f"llm_addr={LLM_ADDR!r}, llm_port={LLM_PORT!r})")
    with open(Path(scene) / "generate.log", "w") as log:
        proc = subprocess.run(["python", "-c", code], cwd="/opt",
                              stdout=log, stderr=subprocess.STDOUT)
    ok = (Path(scene) / "world.ply").is_file()
    with LOCK:
        STATE["done"] = {"scene": scene, "ok": ok,
                         "seconds": round(time.time() - STATE["started"]),
                         "error": None if ok else
                         f"exit {proc.returncode} — see generate.log"}
        STATE["busy"] = False
        STATE["scene"] = None


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
            return self._send(200, {"status": "ok", "busy": STATE["busy"]})
        if self.path == "/status":
            doc = {k: STATE[k] for k in ("busy", "scene", "done")}
            if STATE["busy"] and STATE["scene"]:
                doc["elapsed"] = round(time.time() - STATE["started"])
                doc["stage"] = stage_of(STATE["scene"])
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
            if STATE["busy"]:
                return self._send(409, {"error": "a generation is already "
                                        "running", "scene": STATE["scene"]})
            STATE.update(busy=True, scene=scene,
                         started=time.time(), done=None)
        threading.Thread(target=run_job, args=(scene, steps),
                         daemon=True).start()
        self._send(200, {"ok": True, "scene": scene})


srv = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
print(f"splatgen ready on :8000 — {GPUS} gpus, vlm at "
      f"{LLM_ADDR}:{LLM_PORT}", flush=True)
srv.serve_forever()
