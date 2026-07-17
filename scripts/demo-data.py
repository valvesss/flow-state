#!/usr/bin/env python3
"""Seed a synthetic event log so you can see the dashboard before you've used it.

    FLOW_STATE_HOME=/tmp/fs-demo python3 scripts/demo-data.py
    FLOW_STATE_HOME=/tmp/fs-demo bin/flow-state dash

Never writes to the default home unless you point it there deliberately.
"""

import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from flowstate import config, state  # noqa: E402

SESSIONS = [
    ("web", "local"), ("api", "build-box"), ("infra", "build-box"),
    ("cli", "build-box"), ("docs", "build-box"),
]
TRACKS = [
    ("Track One", "Some Band"), ("Track Two", "Another Band"),
    ("Track Three", "A Third Band"), ("Track Four", "Yet Another Band"),
    ("Track Five", "The Fifth Band"), ("Track Six", "Band Six"),
]


def main():
    rnd = random.Random(7)
    now = time.time()
    start = now - 5 * 3600
    out, music_on, t = [], False, start

    lanes = {name: {"state": None, "t": start + rnd.uniform(0, 600)} for name, _ in SESSIONS}

    def emit(ts, **kw):
        out.append(dict(ts=round(ts, 3), **kw))

    events_q = []
    for name, host in SESSIONS:
        t0 = lanes[name]["t"]
        while t0 < now:
            events_q.append((t0, name, host, "busy"))
            t0 += rnd.uniform(60, 420)          # a turn
            if t0 >= now:
                break
            events_q.append((t0, name, host, "idle"))
            t0 += rnd.uniform(20, 900)          # you, getting back to it
    events_q.sort()

    live = {}
    for ts, name, host, st in events_q:
        prev = live.get(name)
        emit(ts, ev="transition", host=host, session="demo-" + name,
             project=name, **{"from": prev, "to": st})
        live[name] = st

        sessions = [
            {"session": n, "state": s, "since": ts, "project": n}
            for n, s in live.items()
        ]
        # recompute `since` honestly so parking behaves
        sessions = []
        for n, s in live.items():
            last = max((e["ts"] for e in out
                        if e.get("ev") == "transition" and e.get("project") == n), default=ts)
            sessions.append({"session": n, "state": s, "since": last, "project": n})

        play, reason, counts = state.decide(sessions, 300, now=ts)
        if play != music_on:
            tr, ar = rnd.choice(TRACKS)
            emit(ts, ev="music", action="play" if play else "pause",
                 reason=reason, volume=79, ok=True, track=tr, artist=ar, **counts)
            music_on = play

    root = config.ROOT
    os.makedirs(root, exist_ok=True)
    with open(config.EVENTS, "w") as f:
        for e in out:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    print("wrote %d events to %s" % (len(out), config.EVENTS))


if __name__ == "__main__":
    main()
