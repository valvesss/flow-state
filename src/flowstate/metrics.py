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

# A `busy` span with no background work (bg==0) that runs longer than this is
# almost certainly a stuck session -- one that went busy and never fired Stop
# (a remote session the Mac can't pid-check, a crash without SessionEnd) -- not
# real work. Counting it inflates busy_time wildly: one overnight stuck span can
# be 90% of a project's total. A genuine long run has bg>0 (a subagent or
# background shell) and is never treated as stale. Kept generous so a real long
# single turn isn't mistaken for stuck.
STALE_BUSY = 45 * 60


def _merge(intervals):
    """Union of [start, end) intervals, so overlapping/concurrent spans count as
    wall-clock once."""
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def compute(since=None, park_after_s=90, now=None):
    now = time.time() if now is None else now
    # Read the WHOLE log, not just [since, now]. Activity straddling the window
    # start -- a flow block or busy span that began earlier and is still going at
    # the edge -- must count for its in-window portion; filtering by `since` at
    # read time dropped it entirely, which broke the 1h/6h presets. So we build
    # spans over full history and clip when summing; `since` only sets
    # window_start. Skipping lines without `ts` keeps a hand-edited/torn record
    # from crashing the tool.
    log = [e for e in events.read() if "ts" in e]
    trans = [e for e in log if e.get("ev") == "transition"]
    music = [e for e in log if e.get("ev") == "music"]

    window_start = since if since is not None else (log[0]["ts"] if log else now)
    span = now - window_start

    def _clip(a, b):
        """The part of [a, b) inside the window, as (lo, hi), or None."""
        lo, hi = max(a, window_start), min(b, now)
        return (lo, hi) if hi > lo else None

    def _win(a, b):
        c = _clip(a, b)
        return c[1] - c[0] if c else 0.0

    # --- music: what actually played (full history; clipped for display) --
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

    flow_win = [c for c in (_clip(a, b) for a, b in flow_blocks) if c]
    longest_flow = max((b - a for a, b in flow_win), default=0.0)

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
        lanes[key]["points"].append((e["ts"], e.get("to"), e.get("bg", 0)))

    for lane in lanes.values():
        spans, pts = [], lane["points"]
        for i, (ts, st, bg) in enumerate(pts):
            if st == "gone":
                continue
            end = pts[i + 1][0] if i + 1 < len(pts) else now
            closed_by = pts[i + 1][1] if i + 1 < len(pts) else None
            if end > ts:
                dur = end - ts  # staleness judges the whole span, not the window
                stale = st == "busy" and bg == 0 and dur > STALE_BUSY
                spans.append({"start": ts, "end": end, "state": st, "bg": bg,
                              "stale": stale, "closed_by": closed_by})
        lane["spans"] = spans
        # busy_time excludes stale spans (a stuck session wasn't working) and is
        # clipped to the window.
        lane["busy_time"] = sum(
            _win(s["start"], s["end"]) for s in spans if s["state"] == "busy" and not s["stale"])
        lane["stale_busy_time"] = sum(_win(s["start"], s["end"]) for s in spans if s["stale"])
        lane["stale_busy_count"] = sum(
            1 for s in spans if s["stale"] and _clip(s["start"], s["end"]))
        lane["turns"] = sum(
            1 for ts, st, _ in pts if st == "busy" and window_start <= ts <= now)
        del lane["points"]

    # --- presence: intervals you were away (from presence events) ---------
    away_intervals = []
    open_away = None
    for e in log:
        if e.get("ev") != "presence":
            continue
        if e.get("state") == "away" and open_away is None:
            open_away = e["ts"]
        elif e.get("state") == "back" and open_away is not None:
            away_intervals.append((open_away, e["ts"]))
            open_away = None
    if open_away is not None:
        away_intervals.append((open_away, now))

    def _during_away(a, b):
        return any(a < y and x < b for x, y in away_intervals)

    # --- response latency: idle -> busy on the same session ---------------
    # Three buckets instead of one. A gap the user was away for is not a
    # response; a gap far too long to be one (but with no away signal, e.g.
    # older data) is an outlier we KEEP and expose rather than drop silently --
    # dropping it made the log say "covered everything" when it hadn't.
    responses, outliers = [], []
    away_gaps = 0
    for lane in lanes.values():
        for s in lane["spans"]:
            # A response is idle -> busy: you came back and dispatched work.
            # idle -> gone is closing a finished window, not responding -- don't
            # count it (it faked a "you responded in Ns" every time you closed a
            # session). Only gaps whose end lands in the window count.
            if s["state"] != "idle" or s["closed_by"] != "busy":
                continue
            if not (window_start <= s["end"] <= now):
                continue
            d = s["end"] - s["start"]
            if d <= 0:
                continue
            if _during_away(s["start"], s["end"]):
                away_gaps += 1
            elif d > MAX_RESPONSE:
                outliers.append(d)
            else:
                responses.append(d)
    responses.sort()
    outliers.sort()

    # --- the day, partitioned -------------------------------------------
    # One timeline, each instant classified into exactly one state, so the
    # pieces sum to the window instead of being three overlapping measures that
    # don't add up. Precedence away > flow > waiting > idle makes it a true
    # partition (idempotent, count-once -- the discipline borrowed from how ad
    # impressions are accounted). Sweep at every boundary, including the park
    # moment inside each idle span, or the arithmetic drifts.
    marks = {window_start, now}
    for a, b in flow_blocks:
        marks.add(a)
        marks.add(b)
    for a, b in away_intervals:
        marks.add(a)
        marks.add(b)
    for lane in lanes.values():
        for s in lane["spans"]:
            marks.add(s["start"])
            marks.add(s["end"])
            if s["state"] == "idle":
                p = s["start"] + park_after_s
                if s["start"] < p < s["end"]:
                    marks.add(p)
    marks = sorted(m for m in marks if window_start <= m <= now)

    def _in(intervals, t):
        return any(x <= t < y for x, y in intervals)

    def _waiting_at(t):
        for lane in lanes.values():
            for s in lane["spans"]:
                if (s["state"] == "idle" and s["start"] <= t < s["end"]
                        and t - s["start"] < park_after_s):
                    return True
        return False

    day = {"flow": 0.0, "waiting": 0.0, "away": 0.0, "idle": 0.0}
    for a, b in zip(marks, marks[1:]):
        mid = (a + b) / 2
        if _in(away_intervals, mid):
            day["away"] += b - a
        elif _in(flow_blocks, mid):
            day["flow"] += b - a
        elif _waiting_at(mid):
            day["waiting"] += b - a
        else:
            day["idle"] += b - a

    # --- soundtrack -------------------------------------------------------
    tracks = {}
    for m in music:
        if (m.get("action") == "play" and m.get("track")
                and window_start <= m["ts"] <= now):
            k = (m["track"], m.get("artist", ""))
            tracks[k] = tracks.get(k, 0) + 1
    soundtrack = [
        {"track": t, "artist": a, "plays": n}
        for (t, a), n in sorted(tracks.items(), key=lambda kv: -kv[1])
    ]

    # --- projects: wall-clock, not person-seconds -------------------------
    # A project's busy_time is the UNION of its sessions' busy spans, not their
    # sum. Summing double-counts wall-clock when sessions run concurrently (five
    # sessions busy for a minute is one minute of work on that project, not
    # five), which made the per-project total answer a different question than
    # the headline. `share` expresses it as a fraction of the window, so it's
    # directly comparable. Shares across projects can exceed 100% -- two
    # projects genuinely can be worked at once -- but each number is honest.
    agg = {}
    intervals = {}
    for lane in lanes.values():
        p = lane["project"] or "(unknown)"
        agg.setdefault(p, {"project": p, "turns": 0})["turns"] += lane["turns"]
        intervals.setdefault(p, []).extend(
            c for c in (_clip(s["start"], s["end"]) for s in lane["spans"]
                        if s["state"] == "busy" and not s["stale"]) if c)
    projects = []
    for p, d in agg.items():
        wall = sum(b - a for a, b in _merge(intervals[p]))
        projects.append({**d, "busy_time": wall, "share": wall / span if span else 0.0})
    projects.sort(key=lambda d: -d["busy_time"])

    return {
        "window": {"start": window_start, "end": now, "span": span},
        # The four in `day` add up to the window (idempotent partition), so the
        # day reconciles. flow_time is music-actually-playing minus any away.
        "flow_time": day["flow"],
        "attention_time": day["waiting"],
        "day": day,
        "longest_flow": longest_flow,
        "flow_blocks": [{"start": a, "end": b} for a, b in flow_win],
        "turns": sum(lane["turns"] for lane in lanes.values()),
        # sessions that actually did a turn; ephemeral idle->gone ones (the
        # SSH-bridge spawns many) would otherwise inflate the count.
        "sessions_seen": sum(1 for lane in lanes.values() if lane["turns"] > 0),
        "away_time": day["away"],
        "stale_busy_time": sum(lane["stale_busy_time"] for lane in lanes.values()),
        "stale_busy_count": sum(lane["stale_busy_count"] for lane in lanes.values()),
        "response": {
            "count": len(responses),
            "median": _pct(responses, 0.5),
            "p90": _pct(responses, 0.9),
            "p99": _pct(responses, 0.99),
            "max": responses[-1] if responses else 0.0,
            "total": sum(responses),
            # kept, not dropped: long gaps that skew the typical stats, and gaps
            # you were away for. Exposed so truncation is never silent.
            "outliers": len(outliers),
            "outlier_max": outliers[-1] if outliers else 0.0,
            "away_gaps": away_gaps,
        },
        "lanes": sorted(lanes.values(), key=lambda lane: -lane["busy_time"]),
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
