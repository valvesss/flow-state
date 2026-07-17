"""Spotify control over AppleScript, with fades that sound like fades.

Two things this file exists to get right:

1. One osascript spawn per fade, not one per step. A process spawn is ~40ms; a
   24-step fade done as 24 spawns is a second of overhead and audibly steppy.
   The whole ramp is emitted as a single AppleScript `repeat` loop.

2. Perceived loudness is not linear in amplitude -- it follows roughly a power
   law (Stevens', exponent ~0.6 for sones vs. sound pressure). A linear 0->79
   ramp *sounds* like it lurches up then plateaus. To make loudness rise evenly
   the amplitude must follow t**(1/0.6) ~= t**1.67. That exponent is the
   difference between "it fades" and "it fades nicely".

Every entry point is guarded by `application "Spotify" is running`, because a
bare `tell application "Spotify"` will *launch* Spotify -- flow-state must never
open an app you didn't open.
"""

import subprocess
import threading

STEPS = 24
LOUDNESS_EXP = 1.67  # 1 / 0.6, Stevens' power law


def _osa(script, timeout=10):
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def is_running():
    return _osa('application "Spotify" is running') == "true"


def player_state():
    """'playing' | 'paused' | 'stopped' | None (Spotify not running)."""
    if not is_running():
        return None
    return _osa('tell application "Spotify" to return player state as string')


def volume():
    if not is_running():
        return None
    try:
        return int(_osa('tell application "Spotify" to return sound volume'))
    except (TypeError, ValueError):
        return None


def now_playing():
    if not is_running():
        return None
    out = _osa(
        'tell application "Spotify" to return '
        '(name of current track) & "\\t" & (artist of current track)'
    )
    if not out or "\t" not in out:
        return None
    name, artist = out.split("\t", 1)
    return {"track": name, "artist": artist}


# --- script construction ---------------------------------------------------

def _guard(body):
    return "\n".join(
        ['if application "Spotify" is running then', '  tell application "Spotify"']
        + body
        + ["  end tell", "end if"]
    )


def _ramp(frm, to, dur_ms):
    """Lines that ramp the volume frm->to on a perceptual curve."""
    delay = max(dur_ms / STEPS / 1000.0, 0.01)
    base, span = min(frm, to), abs(to - frm)
    t = "(i / %d)" % STEPS
    if to < frm:
        t = "(1 - %s)" % t
    return [
        "    repeat with i from 1 to %d" % STEPS,
        "      set sound volume to (round (%d + (%d * (%s ^ %s))))"
        % (base, span, t, LOUDNESS_EXP),
        "      delay %.3f" % delay,
        "    end repeat",
    ]


def fade_in_script(target, dur_ms):
    return _guard(
        ["    set sound volume to 0", "    play"]
        + _ramp(0, target, dur_ms)
        + ["    set sound volume to %d" % target]
    )


def fade_out_script(frm, rest, dur_ms):
    # After pausing, restore the resting volume: if you later hit play in
    # Spotify yourself, it must not come back mysteriously silent.
    return _guard(
        _ramp(frm, 0, dur_ms)
        + ["    set sound volume to 0", "    pause", "    set sound volume to %d" % rest]
    )


# --- fader -----------------------------------------------------------------

class Fader:
    """Serialises fades and lets a new one interrupt one in flight.

    State can flip mid-fade (a session goes busy while we're fading out). The
    in-flight osascript is killed and the next fade starts from wherever the
    volume actually got to, so the two never fight over the slider.
    """

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()

    def in_flight(self):
        """True while a fade's osascript is still ramping the volume.

        A volume read taken now is a point on the ramp, not a resting value --
        e.g. one third through a fade-in the perceptual curve is only at ~9.
        relearn_volume uses this to refuse to 'learn' a mid-ramp reading, which
        is what once walked the resting volume down to single digits.
        """
        with self._lock:
            p = self._proc
        return p is not None and p.poll() is None

    def _cancel(self):
        with self._lock:
            p, self._proc = self._proc, None
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def _run(self, script):
        self._cancel()
        try:
            p = subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        with self._lock:
            self._proc = p
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self._cancel()
            return False
        return p.returncode == 0

    def fade_in(self, target, dur_ms):
        if not is_running():
            return False
        return self._run(fade_in_script(target, dur_ms))

    def fade_out(self, rest, dur_ms):
        if not is_running() or player_state() != "playing":
            return False
        cur = volume()
        if cur is None:
            return False
        return self._run(fade_out_script(cur, rest, dur_ms))

    def stop(self):
        self._cancel()
