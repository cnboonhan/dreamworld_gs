"""dreamworld_core — the stack's state holder.

The seam between the walk and the building lives here, in a service with
no other job: the viewer pushes where it stands, and anyone — the future
harness first of all — asks. The state itself is born in a browser tab,
which can only push; this is the fixed address it pushes to.

    GET  /health                {"status": "ok"}
    POST /position              {"at": ..., "look": ...} — move the walker.
                                The harness's lever, and the viewer's own
                                go button: ONE writer of position.
    GET  /position              {"position": ..., "seq": N, "stamp": t}
    POST /viewer/state          the viewer's report; the response carries
                                the current position and seq, so following
                                costs no extra request
    GET  /viewer/state          {"state": ..., "age": s, "live": bool}

Stdlib only, memory only: a restart forgets, the next heartbeat and the
next command restore.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"viewer": None, "stamp": 0.0,
         # seq seeds from the clock: a restart must jump FORWARD, never
         # back to zero — a viewer holding yesterday's seq would otherwise
         # gate out every command until its tab reloads
         "position": None, "seq": int(time.time()), "pos_stamp": 0.0}
LOCK = threading.Lock()


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
            return self._send(200, {"status": "ok"})
        if self.path == "/viewer/state":
            with LOCK:
                state, stamp = STATE["viewer"], STATE["stamp"]
            age = (time.time() - stamp) if state else None
            return self._send(200, {"state": state, "age": age,
                                    "live": age is not None and age < 2.0})
        if self.path == "/position":
            with LOCK:
                return self._send(200, {"position": STATE["position"],
                                        "seq": STATE["seq"],
                                        "stamp": STATE["pos_stamp"]})
        self._send(404, {"error": "?"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            doc = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "not json"})
        if self.path == "/viewer/state":
            with LOCK:
                STATE["viewer"] = doc
                STATE["stamp"] = time.time()
                pos, seq = STATE["position"], STATE["seq"]
            return self._send(200, {"ok": True, "position": pos,
                                    "seq": seq})
        if self.path == "/position":
            if not doc.get("at"):
                return self._send(400, {"error": "no at"})
            pos = {"at": doc["at"], "look": doc.get("look", "original")}
            if doc.get("yaw_deg") is not None:
                pos["yaw_deg"] = float(doc["yaw_deg"])
            with LOCK:
                STATE["position"] = pos
                STATE["seq"] += 1
                STATE["pos_stamp"] = time.time()
                seq = STATE["seq"]
            return self._send(200, {"ok": True, "seq": seq})
        self._send(404, {"error": "?"})


srv = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
print("dreamworld_core ready on :8000", flush=True)
srv.serve_forever()
