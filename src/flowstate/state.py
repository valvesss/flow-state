"""Session state: one small JSON file per session, written by hooks.

Why a file per session rather than one shared file: every writer touches only
its own path, so concurrent sessions never contend and no lock is needed. The
aggregate is just a directory listing.
"""

import json
import os
import subprocess
import time

from . import config

BUSY = "busy"
IDLE = "idle"

# A busy session with no verifiable pid is presumed dead after this long. Only
# a fallback: pid liveness is the real check, because a legitimate turn can run
# far longer than any TTL you would want to pick.
STALE_AFTER_S = 1800


def _safe_name(session_id):
    return "".join(c for c in str(session_id) if c.isalnum() or c in "-_")[:64]


def _proc_name(pid):
    try:
        with open("/proc/%d/comm" % int(pid)) as f:  # Linux
            return f.read().strip()
    except OSError:
        pass
    try:  # macOS
        r = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(int(pid))],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ""


def _claude_pid():
    """Our parent, but only if it really is Claude.

    Hooks are exec'd by a shell that usually exec-replaces itself, so getppid()
    is normally the claude process. 'Normally' is not good enough to hang a
    liveness check on: if we record the wrong pid and it exits, we would prune a
    session that is still working and pause the music mid-turn with no event
    coming to undo it. So we verify, and record nothing when unsure -- a missing
    pid degrades to the TTL, which is the safe direction.
    """
    pid = os.getppid()
    name = os.path.basename(_proc_name(pid) or "").lower()
    return pid if "claude" in name or "node" in name else None


def write(session_id, state, cwd=None, extra=None):
    """Called from the hook. Atomic, and never raises into the session."""
    os.makedirs(config.SESSIONS, exist_ok=True)
    name = _safe_name(session_id)
    if not name:
        return
    path = os.path.join(config.SESSIONS, name + ".json")

    now = time.time()
    since = now
    try:
        # Preserve `since` across repeated writes of the same state, so
        # park_after measures time-in-state and not time-since-last-write.
        with open(path) as f:
            prev = json.load(f)
        if prev.get("state") == state:
            since = prev.get("since", now)
    except (OSError, json.JSONDecodeError):
        pass

    rec = {
        "session": name,
        "state": state,
        "since": since,
        "updated": now,
        "pid": _claude_pid(),
        "cwd": cwd or "",
        "project": os.path.basename(cwd.rstrip("/")) if cwd else "",
    }
    if extra:
        rec.update(extra)

    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, path)


def remove(session_id):
    name = _safe_name(session_id)
    if not name:
        return
    try:
        os.remove(os.path.join(config.SESSIONS, name + ".json"))
    except OSError:
        pass


def _alive(rec):
    pid = rec.get("pid")
    if pid:
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except (TypeError, ValueError):
            pass
    # No trustworthy pid: fall back to a generous TTL.
    return (time.time() - rec.get("updated", 0)) < STALE_AFTER_S


def read_all(prune=True, host="local"):
    """All live sessions on this host, pruning records whose process is gone.

    A crashed session that left a `busy` file behind would otherwise keep the
    music playing forever.
    """
    out = []
    try:
        names = os.listdir(config.SESSIONS)
    except OSError:
        return out

    for n in names:
        if not n.endswith(".json"):
            continue
        path = os.path.join(config.SESSIONS, n)
        try:
            with open(path) as f:
                rec = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if prune and not _alive(rec):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        rec["host"] = host
        out.append(rec)
    return out


def decide(sessions, park_after_s, now=None):
    """The whole product, in one function.

    Play iff at least one session is working AND nothing is waiting on you.

    An idle session older than park_after_s is 'parked': you already got the
    pause that announced it, and you have visibly moved on. Without this, one
    session left open but mentally abandoned vetoes the music forever -- which
    with five parallel sessions is the common case, not the corner case.
    """
    now = time.time() if now is None else now
    busy, waiting, parked = [], [], []
    for s in sessions:
        if s.get("state") == BUSY:
            busy.append(s)
        elif now - s.get("since", now) < park_after_s:
            waiting.append(s)
        else:
            parked.append(s)

    play = bool(busy) and not waiting
    if play:
        bg = sum(1 for s in busy if s.get("bg"))
        reason = "%d working%s, nothing waiting on you" % (
            len(busy), " (%d in background)" % bg if bg else "")
    elif waiting:
        reason = "waiting on you: " + ", ".join(
            (w.get("project") or str(w.get("session", ""))[:8]) for w in waiting[:3]
        )
    elif not busy:
        reason = "no sessions working"
    else:
        reason = "idle"
    return play, reason, {"busy": len(busy), "waiting": len(waiting), "parked": len(parked)}
