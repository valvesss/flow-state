# Changelog

## Unreleased

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
