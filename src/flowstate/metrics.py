"""Turn the event log into numbers.

The two headline measures are mirror images:

  flow time      -- you were waiting on Claude (every session busy; music on)
  attention time -- Claude was waiting on you (something idle and unattended)

Response latency is the per-occurrence version of the second: how long a session
sat finished before you came back to it.
"""

import time

from . import events

HOUR = 3600
# An idle->busy gap longer than this isn't "response time", it's "you went to
# lunch". Counting it would let one long lunch swamp the median.
MAX_RESPONSE = 2 * HOUR


def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def compute(since=None, park_after_s=300, now=None):
    now = time.time() if now is None else now
    log = events.read(since=since)
    trans = [e for e in log if e.get("ev") == "transition"]
    music = [e for e in log if e.get("ev") == "music"]

    window_start = since if since else (log[0]["ts"] if log else now)

    # --- music: what actually played -------------------------------------
    flow_blocks = []
    open_play = None
    for m in music:
        if m.get("action") == "play" and open_play is None:
            open_play = m
        elif m.get("action") == "pause" and open_play is not None:
            flow_blocks.append((open_play["ts"], m["ts"]))
            open_play = None
    if open_play is not None:
        flow_blocks.append((open_play["ts"], now))

    flow_time = sum(b - a for a, b in flow_blocks)
    longest_flow = max((b - a for a, b in flow_blocks), default=0.0)

    # --- sessions: per-session state intervals ---------------------------
    lanes = {}
    for e in trans:
        key = "%s/%s" % (e.get("host", "local"), e.get("session"))
        lanes.setdefault(
            key,
            {
                "session": e.get("session"),
                "host": e.get("host", "local"),
                "project": e.get("project", ""),
                "points": [],
            },
        )
        if e.get("project"):
            lanes[key]["project"] = e["project"]
        lanes[key]["points"].append((e["ts"], e.get("to")))

    for lane in lanes.values():
        spans, pts = [], lane["points"]
        for i, (ts, st) in enumerate(pts):
            if st == "gone":
                continue
            end = pts[i + 1][0] if i + 1 < len(pts) else now
            if end > ts:
                spans.append({"start": ts, "end": end, "state": st})
        lane["spans"] = spans
        lane["busy_time"] = sum(s["end"] - s["start"] for s in spans if s["state"] == "busy")
        lane["turns"] = sum(1 for _, st in pts if st == "busy")
        del lane["points"]

    # --- response latency: idle -> busy on the same session ---------------
    responses = []
    for lane in lanes.values():
        for s in lane["spans"]:
            if s["state"] == "idle" and s["end"] < now:
                d = s["end"] - s["start"]
                if 0 < d <= MAX_RESPONSE:
                    responses.append(d)
    responses.sort()

    # --- attention time: sweep with synthetic park boundaries -------------
    # A session parks mid-interval (park_after_s after it went idle), so park
    # moments must be sweep points too or the arithmetic silently drifts.
    marks = {window_start, now}
    for lane in lanes.values():
        for s in lane["spans"]:
            marks.add(s["start"])
            marks.add(s["end"])
            if s["state"] == "idle":
                p = s["start"] + park_after_s
                if s["start"] < p < s["end"]:
                    marks.add(p)
    marks = sorted(m for m in marks if window_start <= m <= now)

    attention_time = 0.0
    for i in range(len(marks) - 1):
        a, b = marks[i], marks[i + 1]
        mid = (a + b) / 2
        waiting = False
        for lane in lanes.values():
            for s in lane["spans"]:
                if s["start"] <= mid < s["end"] and s["state"] == "idle":
                    if mid - s["start"] < park_after_s:
                        waiting = True
                        break
            if waiting:
                break
        if waiting:
            attention_time += b - a

    # --- soundtrack -------------------------------------------------------
    tracks = {}
    for m in music:
        if m.get("action") == "play" and m.get("track"):
            k = (m["track"], m.get("artist", ""))
            tracks[k] = tracks.get(k, 0) + 1
    soundtrack = [
        {"track": t, "artist": a, "plays": n}
        for (t, a), n in sorted(tracks.items(), key=lambda kv: -kv[1])
    ]

    # --- projects ---------------------------------------------------------
    projects = {}
    for lane in lanes.values():
        p = lane["project"] or "(unknown)"
        d = projects.setdefault(p, {"project": p, "turns": 0, "busy_time": 0.0})
        d["turns"] += lane["turns"]
        d["busy_time"] += lane["busy_time"]
    projects = sorted(projects.values(), key=lambda d: -d["busy_time"])

    return {
        "window": {"start": window_start, "end": now, "span": now - window_start},
        "flow_time": flow_time,
        "attention_time": attention_time,
        "longest_flow": longest_flow,
        "flow_blocks": [{"start": a, "end": b} for a, b in flow_blocks],
        "turns": sum(l["turns"] for l in lanes.values()),
        "sessions_seen": len(lanes),
        "response": {
            "count": len(responses),
            "median": _pct(responses, 0.5),
            "p90": _pct(responses, 0.9),
            "total": sum(responses),
        },
        "lanes": sorted(lanes.values(), key=lambda l: -l["busy_time"]),
        "projects": projects,
        "soundtrack": soundtrack,
    }


def tune(park_values=(30, 60, 90, 120, 300, 600), since=None):
    """Replay your real log at each park_after and report how much music you'd get.

    park_after is the only knob that meaningfully changes the feel, and the
    right value depends on how *you* work -- how many sessions you run and how
    fast you turn around. Rather than defend a default, replay the decisions you
    actually made and let the numbers pick.
    """
    from . import state  # local import: metrics is imported by state's callers

    log = [e for e in events.read(since=since) if e.get("ev") == "transition"]
    if len(log) < 2:
        return []

    window = log[-1]["ts"] - log[0]["ts"]
    if window <= 0:
        return []

    out = []
    for park in park_values:
        live, since_map = {}, {}
        on, flow, last = False, 0.0, None
        pauses = 0
        for e in log:
            if last is not None and on:
                flow += e["ts"] - last
            key = "%s/%s" % (e.get("host", "local"), e.get("session"))
            if e.get("to") == "gone":
                live.pop(key, None)
                since_map.pop(key, None)
            else:
                live[key] = e["to"]
                since_map[key] = e["ts"]
            sessions = [
                {"session": k, "state": v, "since": since_map[k]} for k, v in live.items()
            ]
            was = on
            on, _, _ = state.decide(sessions, park, now=e["ts"])
            if was and not on:
                pauses += 1
            last = e["ts"]
        out.append({
            "park_after_s": park,
            "flow_time": flow,
            "share": flow / window,
            "pauses": pauses,
            "pauses_per_hour": pauses / (window / HOUR) if window else 0,
        })
    return out


def human(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < HOUR:
        return "%dm %02ds" % (seconds // 60, seconds % 60)
    return "%dh %02dm" % (seconds // HOUR, (seconds % HOUR) // 60)
