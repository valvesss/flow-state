# Changelog

## Unreleased

### Changed
- **Event log is instrumented for after-the-fact analysis.** Three changes so
  the log can answer "was that a false positive?" and "what were the outliers?":
  - Every event carries a schema version (`v`), so decisions stay attributable
    when the rule changes.
  - `transition` events record `bg` — whether the state was held by background
    work (a subagent, a background shell) rather than a live prompt. This is the
    field that distinguishes a real handoff from a background run after the fact.
  - Metrics stop dropping outliers silently. Long idle gaps and gaps you were
    away for are split into their own buckets and surfaced (`outliers`,
    `away_gaps`, `p99`, `max`, `away_time`) instead of vanishing from the
    typical stats. `flow-state stats` shows what it set aside.
  - Stuck sessions no longer inflate work time. A `busy` span with no background
    work (`bg==0`) running longer than `STALE_BUSY` (45 min) is treated as a
    stuck session — one that went busy and never fired Stop, which the Mac can't
    pid-prune for a *remote* host — and excluded from `busy_time`, surfaced as
    `stale_busy_time`/`stale_busy_count`. A genuine long run has `bg>0` and is
    never touched.

### Fixed
- **Window edges are no longer wrong.** Metrics filtered the event log to
  `[since, now]` at read time, so a flow block or busy span that began before
  the window but was still going inside it counted as **zero** — the 1h/6h
  presets (the dashboard defaults) undercounted anything ongoing. Metrics now
  read full history and clip each quantity to the window; activity straddling
  the start counts its in-window portion.
- **Closing a session no longer fakes a response time.** An `idle -> gone`
  (closing a finished Claude window) was counted as "you responded in Ns",
  skewing the median and inflating `n`. Only `idle -> busy` (you came back and
  dispatched) counts now.
- **`uninstall-hooks` writes settings safely** — backup + temp-file + atomic
  replace, mirroring `install-hooks`, so an interrupted removal can't corrupt
  `~/.claude/settings.json`.
- Corrupt event-log lines (valid JSON but missing `ts`) are skipped instead of
  crashing `stats`/the dashboard. Remote `bg` changes push immediately instead
  of waiting for the heartbeat. `park_after_s` default unified to 90 everywhere
  (was 300 at some fallbacks). Dead metric keys removed.
- **Swimlane no longer buried under empty lanes.** The SSH-bridge spawns many
  ephemeral sessions that only ever go `idle->gone`; each got its own lane,
  drowning the real ones under dozens of blank rows. The timeline now shows only
  sessions that did a turn (capped, with a "+N hidden" note — never silent), and
  `sessions_seen` counts working sessions, not every registration.

### Added
- **The day reconciles.** Metrics were three overlapping measures on one
  timeline that didn't add up — flow + attention + away left ~60% of the window
  unaccounted for. Borrowing the count-once discipline behind ad-impression
  accounting, the window is now partitioned into four mutually-exclusive slices
  — **in flow / waiting on you / away / idle** — that sum to 100%. `flow-state
  stats` and the dashboard show it as a single bar. Per-project time is now
  **wall-clock (union of a project's busy spans), expressed as a share of the
  window**, instead of person-seconds that double-counted concurrent sessions
  and answered a different question than the headline.
- **Presence gate.** flow-state inferred "you're waiting" from session state but
  never checked whether a human was actually there — so a run grinding overnight
  played music (and counted as flow) to an empty room while the user slept, for
  hours. The conductor now reads macOS HID idle time; if you haven't touched the
  machine in `away_after_s` (default 600s / 10 min), you're treated as away and
  the music waits, whatever the sessions are doing. Kept generous on purpose: HID
  idle measures input, not attention, so the threshold is long enough not to
  interrupt watching a long run, far shorter than a night's sleep. Surfaced in
  `flow-state status` and logged as `presence` events. macOS only.

### Fixed
- **Background work no longer silences the music.** A turn that hands off to a
  long-running subagent (deep research), or a background shell command, fires
  `Stop` immediately while the work keeps running — flow-state was reading that
  as "your move" and pausing, during exactly the stretch you have nothing to do.
  `Stop` now inspects its `background_tasks` payload and keeps the session
  *working* while anything there is still `running`. The status line and the
  decision reason show what's running in the background.
- **Resting volume could walk down to single digits.** During a burst of rapid
  pause/play flips, a pause landing mid fade-in read a low point on the
  perceptual ramp (a third of the way up a fade-in is ~9) and "learned" it as
  your resting level. `auto` now refuses to learn a volume while a fade is in
  flight. Added `1h` range to the dashboard.

## 0.1.0

Initial release. Music while Claude works; pause is the notification. Hook-based
(no extension), local + remote sessions over SSH, perceptual fades, metrics and
a dashboard. macOS + Spotify.
