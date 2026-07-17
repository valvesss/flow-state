"""Command line surface."""

import argparse
import json
import os
import subprocess
import sys
import time

from . import config, conductor, events, metrics, spotify, state

# What each hook event means about the session that fired it.
#   Notification -- it wants permission or input. Your move.
#   Stop         -- the turn ended, but see _stop_state below: that does NOT
#                   reliably mean your move.
# SubagentStop is deliberately absent: it carries the *parent's* session_id, so
# hooking it would mark a session idle every time an Explore agent finished.
EVENT_STATE = {
    "SessionStart": state.IDLE,
    "UserPromptSubmit": state.BUSY,
    "Notification": state.IDLE,
}

HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "Stop", "Notification", "SessionEnd"]


def _stop_state(payload):
    """A turn ending is not the same as Claude being done.

    When a turn hands off to background work -- a subagent, a deep research
    sweep, a long-running background command -- `Stop` fires immediately while
    the work grinds on for minutes. Treating that as "your move" is exactly
    backwards: it is the purest case of you having nothing to do, and the naive
    reading silences the music for the entire run.

    `Stop`'s payload carries `background_tasks` (undocumented at time of
    writing, verified on the wire), each entry with a `status`. Anything still
    `running` means the session is still working:

        [{"id": "...", "type": "subagent", "status": "running",
          "description": "Probe hook payloads", "agent_type": "Explore"}]

    When that work finishes the harness re-invokes the session, so another
    `Stop` follows with the list drained -- and that one is the real cue.
    """
    running = [
        t for t in (payload.get("background_tasks") or [])
        if isinstance(t, dict) and t.get("status") == "running"
    ]
    if not running:
        return state.IDLE, {"bg": 0}
    return state.BUSY, {
        "bg": len(running),
        "bg_desc": str(running[0].get("description") or running[0].get("type") or "")[:60],
    }


def cmd_hook(args):
    """Fire-and-forget state write. Must never disturb the session.

    Declared with "async": true, so Claude Code ignores our exit code and never
    waits on us. We still swallow everything: a Stop hook that exits non-zero
    *blocks the turn from ending*, and no music feature is worth wedging a
    session over.
    """
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        ev = payload.get("hook_event_name", "")
        sid = payload.get("session_id")
        if not sid:
            return 0
        if ev == "SessionEnd":
            state.remove(sid)
        elif ev == "Stop":
            st, extra = _stop_state(payload)
            state.write(sid, st, cwd=payload.get("cwd"), extra=extra)
        elif ev in EVENT_STATE:
            state.write(sid, EVENT_STATE[ev], cwd=payload.get("cwd"), extra={"bg": 0})
    except Exception:
        pass
    return 0


def cmd_conductor(args):
    conductor.run(once=args.once)
    return 0


def cmd_watch(args):
    conductor.watch(poll_ms=args.poll_ms)
    return 0


def cmd_status(args):
    cfg = config.load()
    sessions = state.read_all(host="local")

    remote_status = []
    for r in cfg.get("remotes", []):
        name = r.get("name") or r["ssh"]
        try:
            out = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", r["ssh"],
                 r.get("cmd", "~/.flow-state/bin/flow-state watch") + " --once"],
                capture_output=True, text=True, timeout=15,
            )
            data = json.loads(out.stdout.strip().splitlines()[-1])
            for s in data.get("sessions", []):
                s["host"] = name
                sessions.append(s)
            remote_status.append((name, "ok"))
        except Exception as e:
            remote_status.append((name, "unreachable (%s)" % type(e).__name__))

    play, reason, counts = state.decide(sessions, cfg.get("park_after_s", 300))
    st = spotify.player_state()
    np = spotify.now_playing() or {}

    print("flow-state: %s" % ("on" if cfg.get("enabled", True) else "OFF (flow-state off)"))
    print("decision  : %s — %s" % ("PLAY" if play else "PAUSE", reason))
    print("sessions  : %d working, %d waiting on you, %d parked"
          % (counts["busy"], counts["waiting"], counts["parked"]))
    print("spotify   : %s%s" % (
        st or "not running",
        "  ·  %s — %s" % (np["track"], np["artist"]) if np else "",
    ))
    if remote_status:
        print("remotes   : " + ", ".join("%s: %s" % rs for rs in remote_status))
    if sessions:
        print()
        now = time.time()
        for s in sorted(sessions, key=lambda x: x.get("since", 0)):
            age = now - s.get("since", now)
            mark = "▶" if s["state"] == "busy" else (
                "⏸" if age < cfg.get("park_after_s", 90) else "·")
            note = ""
            if s.get("bg"):
                note = "  ⋯ background: %s" % (s.get("bg_desc") or "%d task(s)" % s["bg"])
            print("  %s %-6s %-10s %-22s %s ago%s" % (
                mark, s["state"], s.get("host", "local"),
                (s.get("project") or "")[:22], metrics.human(age), note))
    return 0


def _parse_since(s):
    if not s or s == "all":
        return None
    units = {"m": 60, "h": 3600, "d": 86400}
    try:
        if s[-1] in units:
            return time.time() - float(s[:-1]) * units[s[-1]]
        return time.time() - float(s)
    except ValueError:
        return None


def cmd_stats(args):
    cfg = config.load()
    m = metrics.compute(
        since=_parse_since(args.since), park_after_s=cfg.get("park_after_s", 300)
    )
    if args.json:
        print(json.dumps(m, indent=2))
        return 0

    h = metrics.human
    print("\n  flow-state · last %s\n" % (args.since if args.since != "all" else "everything"))
    print("  %-22s %s" % ("you waited on Claude", h(m["flow_time"])))
    print("  %-22s %s" % ("Claude waited on you", h(m["attention_time"])))
    print("  %-22s %s" % ("longest unbroken flow", h(m["longest_flow"])))
    print("  %-22s %d across %d sessions" % ("turns", m["turns"], m["sessions_seen"]))
    r = m["response"]
    if r["count"]:
        print("  %-22s %s median · %s p90 (n=%d)"
              % ("your response time", h(r["median"]), h(r["p90"]), r["count"]))
    if m["projects"]:
        print("\n  by project")
        for p in m["projects"][:6]:
            print("    %-24s %-9s %d turns" % (p["project"][:24], h(p["busy_time"]), p["turns"]))
    if m["soundtrack"]:
        print("\n  soundtrack")
        for t in m["soundtrack"][:6]:
            print("    %-32s %-22s ×%d" % (t["track"][:32], t["artist"][:22], t["plays"]))
    print()
    return 0


def cmd_tune(args):
    rows = metrics.tune(since=_parse_since(args.since))
    if not rows:
        print("Not enough history yet — use flow-state for a while, then try again.")
        return 0
    cfg = config.load()
    cur = cfg.get("park_after_s", 90)

    print("\n  Replaying your last %s at each park_after.\n" % args.since)
    print("  park_after   music plays        silence breaks")
    print("  " + "-" * 56)
    for r in rows:
        bar = "█" * int(r["share"] * 40)
        mark = "  ← current" if r["park_after_s"] == cur else ""
        print("  %5ds      %-8s %5.1f%%  %-14s %.1f/h%s" % (
            r["park_after_s"], metrics.human(r["flow_time"]), r["share"] * 100,
            bar, r["pauses_per_hour"], mark))
    print("""
  Longer park_after  = a finished session holds the silence longer.
  Shorter park_after = more music, and the cue passes sooner.

  The pause always fires the moment a session finishes; this only sets how
  long the quiet lasts before flow-state assumes you aren't coming.

  Set it with:  flow-state set park_after_s <seconds>
""")
    return 0


def cmd_set(args):
    cfg = config.load()
    key = args.key
    if key not in config.DEFAULTS:
        print("unknown key: %s\nknown: %s" % (key, ", ".join(sorted(config.DEFAULTS))),
              file=sys.stderr)
        return 2
    raw = args.value
    default = config.DEFAULTS[key]
    try:
        if isinstance(default, bool):
            val = raw.lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            val = int(raw)
        elif isinstance(default, list):
            val = json.loads(raw)
        else:
            val = int(raw) if raw.isdigit() else raw
    except (ValueError, json.JSONDecodeError) as e:
        print("bad value for %s: %s" % (key, e), file=sys.stderr)
        return 2
    cfg[key] = val
    config.save(cfg)
    print("%s = %s" % (key, json.dumps(val)))
    return 0


def cmd_dash(args):
    from . import server
    server.serve(port=args.port, open_browser=not args.no_open)
    return 0


def cmd_toggle(args):
    cfg = config.load()
    cfg["enabled"] = args.cmd == "on"
    config.save(cfg)
    print("flow-state %s" % ("on" if cfg["enabled"] else "off"))
    return 0


def _settings_path():
    return os.path.expanduser("~/.claude/settings.json")


def cmd_install_hooks(args):
    """Merge our hooks into ~/.claude/settings.json without clobbering others."""
    path = _settings_path()
    try:
        with open(path) as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {}
    except json.JSONDecodeError:
        print("error: %s is not valid JSON; refusing to touch it" % path, file=sys.stderr)
        return 1

    cmd = os.path.join(config.ROOT, "bin", "flow-state") + " hook"
    hooks = settings.setdefault("hooks", {})
    added = []
    for ev in HOOK_EVENTS:
        entries = hooks.setdefault(ev, [])
        if any(
            h.get("command", "").endswith("flow-state hook")
            for entry in entries for h in entry.get("hooks", [])
        ):
            continue
        entries.append({
            "hooks": [{
                "type": "command",
                "command": cmd,
                # async => Claude never waits on us, and our exit code is
                # ignored. Critical for Stop, where a non-zero exit would
                # block the turn from ending.
                "async": True,
            }]
        })
        added.append(ev)

    if not added:
        print("hooks already installed in %s" % path)
        return 0

    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        backup = path + ".flow-state-backup"
        with open(path) as a, open(backup, "w") as b:
            b.write(a.read())
        print("backed up %s -> %s" % (path, backup))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, path)
    print("installed hooks: %s" % ", ".join(added))
    return 0


def cmd_uninstall_hooks(args):
    path = _settings_path()
    try:
        with open(path) as f:
            settings = json.load(f)
    except (OSError, json.JSONDecodeError):
        print("nothing to remove")
        return 0
    hooks = settings.get("hooks", {})
    removed = []
    for ev in list(hooks):
        kept = []
        for entry in hooks[ev]:
            inner = [h for h in entry.get("hooks", [])
                     if not h.get("command", "").endswith("flow-state hook")]
            if inner:
                entry["hooks"] = inner
                kept.append(entry)
            elif entry.get("hooks"):
                removed.append(ev)
        if kept:
            hooks[ev] = kept
        else:
            hooks.pop(ev)
    if not hooks:
        settings.pop("hooks", None)
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
    print("removed flow-state hooks from: %s" % (", ".join(sorted(set(removed))) or "nothing"))
    return 0


def cmd_doctor(args):
    ok = True

    def chk(label, good, detail=""):
        nonlocal ok
        ok = ok and good
        print("  %s %-28s %s" % ("✓" if good else "✗", label, detail))

    print("\n  flow-state doctor\n")
    chk("python", sys.version_info >= (3, 8), sys.version.split()[0])
    chk("install root", os.path.isdir(config.ROOT), config.ROOT)

    if sys.platform == "darwin":
        chk("spotify installed", os.path.isdir("/Applications/Spotify.app"))
        chk("spotify running", spotify.is_running(),
            spotify.player_state() or "start Spotify to use flow-state")
        v = spotify.volume()
        chk("applescript control", v is not None, "volume=%s" % v)
    else:
        print("  · not macOS — this host can only report sessions (`watch`)")

    try:
        with open(_settings_path()) as f:
            s = json.load(f)
        installed = [
            ev for ev in HOOK_EVENTS
            if any(h.get("command", "").endswith("flow-state hook")
                   for e in s.get("hooks", {}).get(ev, []) for h in e.get("hooks", []))
        ]
        chk("hooks installed", len(installed) == len(HOOK_EVENTS),
            "%d/%d: %s" % (len(installed), len(HOOK_EVENTS), ",".join(installed) or "none"))
    except (OSError, json.JSONDecodeError):
        chk("hooks installed", False, "no readable ~/.claude/settings.json")

    cfg = config.load()
    chk("enabled", cfg.get("enabled", True), "flow-state on/off")
    for r in cfg.get("remotes", []):
        name = r.get("name") or r["ssh"]
        try:
            rc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", r["ssh"],
                 "test -x ~/.flow-state/bin/flow-state && echo yes"],
                capture_output=True, text=True, timeout=15,
            )
            chk("remote %s" % name, rc.stdout.strip() == "yes",
                r["ssh"] + (" ok" if rc.stdout.strip() == "yes" else " — flow-state not installed there"))
        except Exception as e:
            chk("remote %s" % name, False, "%s: %s" % (r["ssh"], type(e).__name__))

    n = len(events.read())
    chk("event log", True, "%d events · %s" % (n, config.EVENTS))
    print()
    return 0 if ok else 1


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="flow-state",
        description="Music while Claude works. Silence means it's your move.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("hook", help="internal: consume a Claude Code hook event")

    c = sub.add_parser("conductor", help="run the daemon (owns Spotify)")
    c.add_argument("--once", action="store_true", help="evaluate once and exit")

    w = sub.add_parser("watch", help="internal: stream this host's sessions as JSON")
    w.add_argument("--poll-ms", type=int, default=250)
    w.add_argument("--once", action="store_true")

    sub.add_parser("status", help="what flow-state thinks right now")

    s = sub.add_parser("stats", help="metrics from the event log")
    s.add_argument("--since", default="24h", help="e.g. 90m, 24h, 7d, all")
    s.add_argument("--json", action="store_true")

    t = sub.add_parser("tune", help="replay your log to pick park_after_s")
    t.add_argument("--since", default="7d", help="e.g. 24h, 7d, all")

    se = sub.add_parser("set", help="set a config key")
    se.add_argument("key")
    se.add_argument("value")

    d = sub.add_parser("dash", help="open the dashboard")
    d.add_argument("--port", type=int, default=config.load().get("dashboard_port", 7777))
    d.add_argument("--no-open", action="store_true")

    sub.add_parser("on", help="enable")
    sub.add_parser("off", help="disable (music left alone)")
    sub.add_parser("install-hooks", help="add hooks to ~/.claude/settings.json")
    sub.add_parser("uninstall-hooks", help="remove them again")
    sub.add_parser("doctor", help="check the setup")

    args = p.parse_args(argv)

    if args.cmd == "watch" and args.once:
        sessions = state.read_all()
        print(json.dumps({"sessions": sessions, "ts": time.time()}, separators=(",", ":")))
        return 0

    return {
        "hook": cmd_hook,
        "conductor": cmd_conductor,
        "watch": cmd_watch,
        "status": cmd_status,
        "stats": cmd_stats,
        "tune": cmd_tune,
        "set": cmd_set,
        "dash": cmd_dash,
        "on": cmd_toggle,
        "off": cmd_toggle,
        "install-hooks": cmd_install_hooks,
        "uninstall-hooks": cmd_uninstall_hooks,
        "doctor": cmd_doctor,
    }[args.cmd](args)
