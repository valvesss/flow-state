#!/usr/bin/env python3
"""Seed a synthetic event log so you can see the dashboard before you've used it.

    FLOW_STATE_HOME=/tmp/fs-demo python3 scripts/demo-data.py
    FLOW_STATE_HOME=/tmp/fs-demo bin/flow-state dash

This is an event-driven simulation: every session keeps its own next-event time
and they are processed in time order, so turns genuinely overlap the way five
parallel sessions do. Advancing one global clock per session in a loop would
serialise them -- the last session in each round would sit idle for as long as
all the others took, which both looks wrong and badly understates flow time.

Music events are produced by calling the real `state.decide`, not by guessing,
so the demo cannot drift away from the shipped rule.

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
    ("web", "local"),
    ("api", "build-box"),
    ("infra", "build-box"),
    ("cli", "build-box"),
    ("docs", "build-box"),
]

TRACKS = [
    ("Take It Easy", "Eagles"),
    ("The Weight", "The Band"),
    ("Rhiannon", "Fleetwood Mac"),
    ("Ramble On", "Led Zeppelin"),
    ("Southern Cross", "Crosby, Stills & Nash"),
    ("Tuesday's Gone", "Lynyrd Skynyrd"),
]

HOURS = 6
PARK_AFTER = 90

TURN_S = (150, 700)       # how long a turn runs
ANSWER_S = (20, 200)      # how fast you get back to a session
PARK_CHANCE = 0.15        # sometimes you leave one and focus elsewhere
PARK_S = (200, 600)


def _guard_home():
    """Refuse to write demo events into a real install.

    config.EVENTS defaults to ~/.flow-state when FLOW_STATE_HOME is unset, so a
    bare `python3 scripts/demo-data.py` would clobber a real log with mock
    tracks. Require an explicit, non-default home.
    """
    home = os.environ.get("FLOW_STATE_HOME")
    real = os.path.join(os.path.expanduser("~"), ".flow-state")
    if not home or os.path.abspath(home) == os.path.abspath(real):
        sys.stderr.write(
            "refusing to write demo data into your real log.\n"
            "set a throwaway home first, e.g.:\n"
            "    FLOW_STATE_HOME=/tmp/fs-demo python3 scripts/demo-data.py\n")
        raise SystemExit(2)


def main():
    _guard_home()
    rnd = random.Random(11)
    now = time.time()
    start = now - HOURS * 3600
    host_of = dict(SESSIONS)

    out = []
    live, since = {}, {}
    music_on = False

    def emit(ts, **kw):
        out.append(dict(ts=round(ts, 3), **kw))

    def settle(ts):
        """Ask the real rule what the music should be doing, and log it."""
        nonlocal music_on
        sessions = [
            {"session": n, "state": s, "since": since[n], "project": n}
            for n, s in live.items()
        ]
        play, reason, counts = state.decide(sessions, PARK_AFTER, now=ts)
        if play != music_on:
            tr, ar = rnd.choice(TRACKS)
            emit(ts, ev="music", action="play" if play else "pause",
                 reason=reason, volume=79, ok=True, track=tr, artist=ar, **counts)
            music_on = play

    emit(start, ev="conductor", action="start", remotes=["build-box"])

    # every session starts idle, staggered, with its own next-event clock
    next_at = {}
    for name, _ in SESSIONS:
        t0 = start + rnd.uniform(0, 90)
        live[name], since[name] = "idle", t0
        emit(t0, ev="transition", host=host_of[name], session="demo-" + name,
             project=name, **{"from": None, "to": "idle"})
        next_at[name] = t0 + rnd.uniform(*ANSWER_S)

    # process in time order: sessions overlap, as they actually do
    while True:
        name = min(next_at, key=next_at.get)
        t = next_at[name]
        if t >= now:
            break

        nxt = "busy" if live[name] == "idle" else "idle"
        emit(t, ev="transition", host=host_of[name], session="demo-" + name,
             project=name, **{"from": live[name], "to": nxt})
        live[name], since[name] = nxt, t
        settle(t)

        if nxt == "busy":
            next_at[name] = t + rnd.uniform(*TURN_S)
        elif rnd.random() < PARK_CHANCE:
            next_at[name] = t + rnd.uniform(*PARK_S)     # you leave it parked
        else:
            next_at[name] = t + rnd.uniform(*ANSWER_S)   # you answer it

        # a parked session crossing its boundary changes the decision even
        # though no session transitioned -- give the rule a chance to notice
        for other, ts in list(since.items()):
            if live[other] == "idle" and t < ts + PARK_AFTER < min(next_at.values()):
                settle(ts + PARK_AFTER)

    settle(now)

    os.makedirs(config.ROOT, exist_ok=True)
    out.sort(key=lambda e: e["ts"])
    with open(config.EVENTS, "w") as f:
        for e in out:
            if start <= e["ts"] <= now:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")
    print("wrote %d events to %s" % (len(out), config.EVENTS))


if __name__ == "__main__":
    main()
