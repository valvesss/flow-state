"""The daemon. Merges session state from every host, decides, moves the slider.

Runs on the Mac, because that is where Spotify is. Owns the event log so that
every metric shares one clock.
"""

import json
import os
import sys
import time

from . import config, events, spotify, state
from .remote import RemoteWatcher

LEARNED = os.path.join(config.RUN, "learned_volume")

# Spotify quantises its volume scale: write 79, read back 78; write 78, read
# back 77. Every set/read round trip loses a point. `target_volume: "auto"`
# relearns the resting volume on each pause, so naively believing that readback
# would ratchet your volume down to silence over a day.
#
# So: only relearn when the reading is further from the learned value than the
# quantisation error can explain -- i.e. when *you* moved the slider, not when
# we did. The cost is that nudging Spotify by one or two points won't register;
# that is the right trade against a volume that quietly walks to zero.
QUANTISATION_SLOP = 2


def _log(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        os.makedirs(config.ROOT, exist_ok=True)
        with open(config.LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if os.environ.get("FLOW_STATE_FOREGROUND"):
        print(line, flush=True)


def _read_learned():
    try:
        with open(LEARNED) as f:
            v = int(f.read().strip())
        return v if 0 < v <= 100 else None
    except (OSError, ValueError):
        return None


def _write_learned(v):
    try:
        os.makedirs(config.RUN, exist_ok=True)
        with open(LEARNED, "w") as f:
            f.write(str(int(v)))
    except OSError:
        pass


def resting_volume(cfg):
    """The volume we fade up to and hand back on pause.

    'auto' means: whatever you were listening at when we last took over.
    """
    tv = cfg.get("target_volume", "auto")
    if isinstance(tv, (int, float)) and 0 < tv <= 100:
        return int(tv)
    learned = _read_learned()
    if learned:
        return learned
    cur = spotify.volume()
    return cur if cur and cur > 5 else 60


def relearn_volume(cfg, fader=None):
    """Update the learned resting volume iff *you* moved the slider.

    Called just before we ramp to zero, since that is the last moment the
    volume still reflects what you were listening at.

    Two ways a naive read lies, both learned the hard way on real hardware:

      * Mid-fade. If a fade is still ramping, the reading is a point on the
        curve, not a resting level -- a third of the way into a fade-in that is
        ~9. Believing it walks the resting volume down to single digits over a
        burst of rapid pause/play flips. So we refuse to learn while in flight.
      * Quantisation. Spotify's scale loses a point on every set/read round
        trip, so a reading within QUANTISATION_SLOP of the learned value is our
        own rounding noise, not a decision you made.
    """
    if cfg.get("target_volume", "auto") != "auto":
        return None
    if fader is not None and fader.in_flight():
        return _read_learned()  # a fade is ramping; this read is not a resting level
    cur = spotify.volume()
    if not cur or cur <= 5:
        return _read_learned()
    learned = _read_learned()
    if learned is not None and abs(cur - learned) <= QUANTISATION_SLOP:
        return learned
    _write_learned(cur)
    return cur


def _key(s):
    return "%s/%s" % (s.get("host", "local"), s.get("session"))


def _diff(prev, cur):
    """Yield (session, from_state, to_state) transitions between snapshots."""
    for k, s in cur.items():
        old = prev.get(k)
        if old is None:
            yield s, None, s.get("state")
        elif old.get("state") != s.get("state"):
            yield s, old.get("state"), s.get("state")
    for k, s in prev.items():
        if k not in cur:
            yield s, s.get("state"), "gone"


def run(once=False):
    cfg = config.load()
    fader = spotify.Fader()

    watchers = []
    for r in cfg.get("remotes", []):
        w = RemoteWatcher(
            r.get("name") or r["ssh"],
            r["ssh"],
            r.get("cmd", "~/.flow-state/bin/flow-state watch"),
            on_log=_log,
        )
        w.start()
        watchers.append(w)

    events.emit("conductor", action="start", remotes=[w.name for w in watchers])
    _log("conductor up; remotes=%s" % ([w.name for w in watchers] or "none"))

    prev = {}
    playing = None  # our belief about whether *we* have it playing
    last_enabled = None

    try:
        while True:
            cfg = config.load()
            poll = max(cfg.get("poll_ms", 250), 50) / 1000.0

            if not cfg.get("enabled", True):
                if last_enabled is not False:
                    _log("disabled")
                    if playing:
                        fader.fade_out(resting_volume(cfg), cfg.get("fade_out_ms", 800))
                        events.emit("music", action="pause", reason="flow-state disabled")
                        playing = False
                    last_enabled = False
                if once:
                    return
                time.sleep(poll)
                continue
            if last_enabled is False:
                _log("enabled")
            last_enabled = True

            sessions = state.read_all(host="local")
            for w in watchers:
                sessions.extend(w.snapshot())
            cur = {_key(s): s for s in sessions}

            for s, frm, to in _diff(prev, cur):
                events.emit(
                    "transition",
                    host=s.get("host", "local"),
                    session=s.get("session"),
                    project=s.get("project", ""),
                    **{"from": frm, "to": to}
                )
            prev = cur

            play, reason, counts = state.decide(
                sessions, cfg.get("park_after_s", 300)
            )

            if play != playing:
                rest = resting_volume(cfg)
                if play:
                    ok = fader.fade_in(rest, cfg.get("fade_in_ms", 1200))
                    np = spotify.now_playing() or {}
                    events.emit(
                        "music", action="play", reason=reason, volume=rest,
                        ok=ok, **counts, **np
                    )
                    _log("play (%s) -> vol %d%s"
                         % (reason, rest, "" if ok else " [spotify unavailable]"))
                else:
                    # Relearn before ramping to zero: this is the last moment
                    # the slider still reflects what you were listening at.
                    rest = relearn_volume(cfg, fader) or rest
                    np = spotify.now_playing() or {}
                    ok = fader.fade_out(rest, cfg.get("fade_out_ms", 800))
                    events.emit(
                        "music", action="pause", reason=reason, volume=rest,
                        ok=ok, **counts, **np
                    )
                    _log("pause (%s)" % reason)
                playing = play

            if once:
                return
            time.sleep(poll)
    except KeyboardInterrupt:
        pass
    finally:
        for w in watchers:
            w.stop()
        fader.stop()
        events.emit("conductor", action="stop")
        _log("conductor down")


def watch(poll_ms=250, heartbeat=10):
    """Run on a REMOTE host. Print this host's sessions as JSON on change.

    Line-buffered so the Mac sees a change the moment it happens rather than on
    a poll interval. The heartbeat lets the reader distinguish "nothing is
    happening" from "the link died".
    """
    last_payload = None
    last_sent = 0.0
    while True:
        sessions = state.read_all()
        payload = json.dumps(
            {"sessions": sessions, "ts": time.time()}, separators=(",", ":"), sort_keys=True
        )
        ordered = sorted(sessions, key=lambda x: x["session"])
        fingerprint = json.dumps(
            [(s["session"], s["state"], s.get("since")) for s in ordered],
            sort_keys=True,
        )
        now = time.time()
        if fingerprint != last_payload or now - last_sent > heartbeat:
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()
            last_payload = fingerprint
            last_sent = now
        time.sleep(poll_ms / 1000.0)
