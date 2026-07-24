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

        if u.path == "/logo.svg":
            try:
                with open(os.path.join(HERE, "logo.svg"), "rb") as f:
                    return self._send(200, f.read(), "image/svg+xml")
            except OSError:
                return self._send(404, "no logo", "text/plain")

        if u.path == "/api/stats":
            qs = parse_qs(u.query)
            cfg = config.load()
            m = metrics.compute(
                since=_since(qs), park_after_s=cfg.get("park_after_s", 90)
            )
            sessions = state.read_all(host="local")
            play, reason, counts = state.decide(sessions, cfg.get("park_after_s", 90))
            m["now"] = {
                "play": play,
                "reason": reason,
                "counts": counts,
                "enabled": cfg.get("enabled", True),
                "spotify": spotify.player_state(),
                "track": spotify.now_playing(),
                "park_after_s": cfg.get("park_after_s", 90),
            }
            return self._send(
                200, json.dumps(m), "application/json; charset=utf-8"
            )

        self._send(404, "not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/enabled":
            # The big on/off switch. Writes the same `enabled` flag `flow-state
            # on/off` sets; the conductor reads it on its next poll and either
            # takes over the slider or leaves Spotify entirely alone.
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                enabled = bool(body["enabled"])
            except (ValueError, KeyError, json.JSONDecodeError):
                return self._send(
                    400, '{"error":"expected {\\"enabled\\": bool}"}',
                    "application/json; charset=utf-8")
            cfg = config.load()
            cfg["enabled"] = enabled
            config.save(cfg)
            return self._send(
                200, json.dumps({"enabled": enabled}),
                "application/json; charset=utf-8")
        self._send(404, "not found", "text/plain")


def serve_background(port=7777):
    """Start the dashboard + control server on a daemon thread and return it.

    Used by the conductor so the panel is up for the whole life of the daemon.
    Raises OSError if the port is taken -- the caller decides whether that is
    fatal (it isn't, for the conductor)."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


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
