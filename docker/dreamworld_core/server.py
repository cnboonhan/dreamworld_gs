"""dreamworld_core — the stack's state holder.

The seam between the walk and the building lives here, in a service with
no other job: the viewer pushes where it stands, and anyone — the future
harness first of all — asks. The state itself is born in a browser tab,
which can only push; this is the fixed address it pushes to.

    GET  /health                {"status": "ok"}
    POST /viewer/state          the viewer's report, verbatim
    GET  /viewer/state          {"state": ..., "age": s, "live": bool}

Stdlib only, memory only: a restart forgets the last report and the next
heartbeat (the viewer sends one every second) restores it.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"viewer": None, "stamp": 0.0}
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
        self._send(404, {"error": "?"})

    def do_POST(self):
        if self.path != "/viewer/state":
            return self._send(404, {"error": "?"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            doc = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "not json"})
        with LOCK:
            STATE["viewer"] = doc
            STATE["stamp"] = time.time()
        self._send(200, {"ok": True})


srv = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
print("dreamworld_core ready on :8000", flush=True)
srv.serve_forever()
