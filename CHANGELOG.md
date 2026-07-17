# Changelog

## Unreleased

### Added
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
