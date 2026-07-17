#!/usr/bin/env python3
"""Regenerate the README screenshots. Reproducible, so docs can't rot silently.

    python3 scripts/capture-docs.py

Seeds demo data in a throwaway home, serves the real dashboard against it, and
drives headless Chrome over it in both themes. Nothing here touches your real
log, your real Spotify, or your real config.

Spotify is stubbed rather than queried: the capture must not depend on what
happens to be playing on the machine building the docs, and the maintainer's
listening history has no business in a public screenshot.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

DEMO_HOME = os.path.join(tempfile.gettempdir(), "flow-state-docs")
PORT = 7801
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]


def chrome():
    for c in CHROME_CANDIDATES:
        if c and os.path.exists(c):
            return c
    sys.exit("no Chrome/Chromium found — install one or capture by hand")


def main():
    shutil.rmtree(DEMO_HOME, ignore_errors=True)
    os.environ["FLOW_STATE_HOME"] = DEMO_HOME

    from flowstate import config
    config.ROOT = DEMO_HOME
    config.RUN = os.path.join(DEMO_HOME, "run")
    config.SESSIONS = os.path.join(config.RUN, "sessions")
    config.EVENTS = os.path.join(DEMO_HOME, "events.jsonl")
    config.CONFIG = os.path.join(DEMO_HOME, "config.json")

    print("→ seeding demo log")
    subprocess.run(
        [sys.executable, os.path.join(HERE, "demo-data.py")],
        env={**os.environ, "FLOW_STATE_HOME": DEMO_HOME},
        check=True,
    )

    # A live "in flow" state, so the pill shows the interesting case rather
    # than "no sessions working" over a full day of history. pid is null on
    # purpose: it degrades to the TTL, which keeps these alive for the capture.
    os.makedirs(config.SESSIONS, exist_ok=True)
    now = time.time()
    for name in ("web", "api", "infra", "cli", "docs"):
        with open(os.path.join(config.SESSIONS, "demo-%s.json" % name), "w") as f:
            json.dump({
                "session": "demo-" + name, "state": "busy",
                "since": now - 40, "updated": now, "pid": None,
                "cwd": "/src/" + name, "project": name,
            }, f)

    # Stub Spotify: never query the machine building the docs.
    from flowstate import spotify
    spotify.is_running = lambda: True
    spotify.player_state = lambda: "playing"
    spotify.volume = lambda: 79
    spotify.now_playing = lambda: {"track": "Tuesday's Gone", "artist": "Lynyrd Skynyrd"}

    from flowstate import server
    srv_thread = threading.Thread(
        target=lambda: server.serve(port=PORT, open_browser=False), daemon=True
    )
    srv_thread.start()
    time.sleep(1.2)

    out_dir = os.path.join(ROOT, "docs")
    os.makedirs(out_dir, exist_ok=True)
    exe = chrome()
    for theme in ("dark", "light"):
        dest = os.path.join(out_dir, "dashboard-%s.png" % theme)
        print("→ capturing %s" % os.path.relpath(dest, ROOT))
        subprocess.run([
            exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-color-profile=srgb", "--virtual-time-budget=3000",
            "--window-size=1240,1000",
            "--screenshot=" + dest,
            "http://127.0.0.1:%d/?theme=%s&since=1h" % (PORT, theme),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    shutil.rmtree(DEMO_HOME, ignore_errors=True)
    print("→ done")


if __name__ == "__main__":
    main()
