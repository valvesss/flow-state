"""Local dashboard. Binds to loopback only -- this is your day, not the world's."""

import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, metrics, spotify, state

HERE = os.path.dirname(os.path.abspath(__file__))


def _since(qs):
    v = (qs.get("since") or ["24h"])[0]
    if v == "all":
        return None
    units = {"m": 60, "h": 3600, "d": 86400}
    try:
        if v and v[-1] in units:
            return time.time() - float(v[:-1]) * units[v[-1]]
    except ValueError:
        pass
    return time.time() - 86400


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # a metrics dashboard that spams the terminal is a metrics dashboard you close

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")

        if u.path == "/api/stats":
            qs = parse_qs(u.query)
            cfg = config.load()
            m = metrics.compute(
                since=_since(qs), park_after_s=cfg.get("park_after_s", 300)
            )
            sessions = state.read_all(host="local")
            play, reason, counts = state.decide(sessions, cfg.get("park_after_s", 300))
            m["now"] = {
                "play": play,
                "reason": reason,
                "counts": counts,
                "enabled": cfg.get("enabled", True),
                "spotify": spotify.player_state(),
                "track": spotify.now_playing(),
                "park_after_s": cfg.get("park_after_s", 300),
            }
            return self._send(
                200, json.dumps(m), "application/json; charset=utf-8"
            )

        self._send(404, "not found", "text/plain")


def serve(port=7777, open_browser=True):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print("flow-state dashboard → %s   (ctrl-c to stop)" % url)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        srv.server_close()
