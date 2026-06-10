#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serve_dashboard.py — local bridge server for the tracking dashboard
===================================================================
Serves the static dashboard + feed AND exposes one endpoint, POST /api/execute,
that turns a dashboard "Execute" click into a call to execution.py. This is the
bridge that makes the read-only dashboard interactive.

Use this INSTEAD of `python -m http.server` when you want the Execute button to
work (the plain static server returns 404 on the POST).

SAFETY
------
- Binds to 127.0.0.1 only — never exposed off this machine.
- DRY-RUN by default: /api/execute calls execution.place_order WITHOUT confirm,
  so nothing is ever sent. The button just shows what would be submitted.
- Live mode requires the explicit env flag WEATHERBET_LIVE=1 AND the execution
  credentials from execution.py (POLY_PK etc.). Even armed, every order is one
  deliberate click — there is no auto-trading.

Usage
-----
    python serve_dashboard.py                       # dry-run bridge, port 8000
    WEATHERBET_LIVE=1 python serve_dashboard.py      # arm live (still click-per-order)
    DASH_PORT=8010 python serve_dashboard.py         # custom port
Then open http://localhost:8000/dashboard.html
"""

import os
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from execution import PolymarketExecutor

HOST = "127.0.0.1"
PORT = int(os.environ.get("DASH_PORT", "8000"))
LIVE = os.environ.get("WEATHERBET_LIVE") == "1"

# One executor for the process. dry_run unless explicitly armed.
_executor = PolymarketExecutor(dry_run=not LIVE)


class Handler(SimpleHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.split("?")[0] != "/api/execute":
            self._json(404, {"status": "error", "reason": "unknown endpoint"})
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            block = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._json(400, {"status": "error", "reason": f"bad request: {e}"})
            return
        # confirm only when live-armed; dry-run otherwise. The human already
        # clicked + confirmed in the browser, so this is one deliberate order.
        result = _executor.place_order(block, confirm=LIVE)
        self._json(200, result)

    def log_message(self, *args):
        pass  # keep the console quiet


def main():
    mode = "LIVE (armed — real orders!)" if LIVE else "DRY-RUN (safe)"
    print(f"  dashboard bridge: http://{HOST}:{PORT}/dashboard.html")
    print(f"  execute endpoint: POST /api/execute  | mode: {mode}")
    if LIVE and not _executor.can_go_live:
        print("  [warn] WEATHERBET_LIVE=1 but no usable credentials — falls back to dry-run")
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")


if __name__ == "__main__":
    main()
