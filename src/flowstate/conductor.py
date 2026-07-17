"""The daemon. Merges session state from every host, decides, moves the slider.

Runs on the Mac, because that is where Spotify is. Owns the event log so that
every metric shares one clock.
"""

import json
import os
import re
import subprocess
import sys
import time

from . import config, events, metrics, spotify, state
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


_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')


def hid_idle_seconds():
    """Seconds since the last keyboard/mouse input on this Mac, or None.

    This is the presence signal. flow-state infers "you're waiting" from session
    state, but that is blind to whether *you* are actually here -- a session
    grinding overnight while you sleep looks identical to one you're watching.
    HID idle is the ground truth for "is a human at the keyboard".

    None means we can't tell (not macOS, or ioreg failed); callers treat that as
    "present", because the only host that runs the conductor is the Mac and a
    missing signal must never be the reason music stops.
    """
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=4,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = _IDLE_RE.search(out)
    if not m:
        return None
    return int(m.group(1)) // 1_000_000_000


def is_away(idle_s, away_after):
    """True when the human has been idle at the machine long enough to count as
    gone. idle_s of 0 (including the can't-tell case) is always present."""
    return idle_s >= away_after


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
    away = None            # our belief about whether the human has stepped away
    idle_s = 0             # last HID idle reading
    idle_checked_at = 0.0  # throttle: reading ioreg every poll is wasteful

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
                    # bg>0 means this state is held by background work (a subagent
                    # or background shell), not a live prompt. This is the field
                    # that lets you tell a real handoff from a background run
                    # after the fact -- the exact distinction behind the
                    # "deep research went silent" false positive.
                    bg=s.get("bg", 0),
                    **{"from": frm, "to": to}
                )
            prev = cur

            play, reason, counts = state.decide(
                sessions, cfg.get("park_after_s", 90)
            )

            # Presence gate. No matter what the sessions say, don't play to an
            # empty room: if the human hasn't touched the machine in a while,
            # they've stepped away (or fallen asleep) and the music should wait.
            away_after = cfg.get("away_after_s", 600)
            now_t = time.time()
            if now_t - idle_checked_at >= 2:
                idle = hid_idle_seconds()
                idle_s = idle if idle is not None else 0
                idle_checked_at = now_t
            gone = is_away(idle_s, away_after)  # can't-tell => idle_s 0 => present
            if gone != away and away is not None:
                events.emit("presence", state="away" if gone else "back",
                            idle_s=idle_s)
                _log("presence: %s (idle %s)" % (
                    "away" if gone else "back", metrics.human(idle_s)))
            away = gone

            if gone:
                play = False
                reason = "you're away (idle %s)" % metrics.human(idle_s)

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
            # include bg: a remote session flipping to/from background work while
            # staying "busy" must push immediately, not wait for the heartbeat.
            [(s["session"], s["state"], s.get("since"), s.get("bg", 0)) for s in ordered],
            sort_keys=True,
        )
        now = time.time()
        if fingerprint != last_payload or now - last_sent > heartbeat:
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()
            last_payload = fingerprint
            last_sent = now
        time.sleep(poll_ms / 1000.0)
