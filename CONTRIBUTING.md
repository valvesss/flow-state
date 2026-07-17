# Contributing

Thanks for looking. flow-state is small on purpose — stdlib-only Python, no build
step, no dependencies — and the goal is to keep it that way.

## Ground rules

- **No runtime dependencies.** If a feature needs a pip package, it probably
  belongs in a fork. The whole thing has to run on whatever `python3` a fresh
  machine already has (3.8+).
- **The hook must never disturb a session.** `flow-state hook` runs inside your
  Claude Code turns. It is declared `async: true` and swallows every exception,
  because a `Stop` hook that exits non-zero *blocks the turn from ending*. Keep
  it that way.
- **Never launch Spotify.** Every AppleScript path is guarded by
  `application "Spotify" is running`. flow-state reacts to the app you opened; it
  doesn't open one.

## Running it

```sh
python3 -m unittest discover -s tests -v     # the whole suite, no deps

# see the dashboard against synthetic data, without touching your real setup
FLOW_STATE_HOME=/tmp/fs-demo python3 scripts/demo-data.py
FLOW_STATE_HOME=/tmp/fs-demo python3 bin/flow-state dash
```

`FLOW_STATE_HOME` relocates *everything* — state, config, event log — so you can
develop without disturbing a live install.

## Before you open a PR

CI runs these on Linux and macOS; run them first:

```sh
python3 -m unittest discover -s tests    # tests must pass
ruff check .                             # lint (config in pyproject.toml)
shellcheck install.sh                    # the installer
```

New behaviour needs a test. The bugs worth having tests are the ones that only
show up on real hardware — Spotify's quantised volume scale, fades caught
mid-ramp, `Stop` firing while background work runs. Several tests exist purely
because those bit us in production; add to them.

## Regenerating the screenshots

```sh
python3 scripts/capture-docs.py          # needs Chrome or Chromium
```

This seeds throwaway data, serves the real dashboard, and captures both themes.
It never reads your real log or your real Spotify.

## Scope

flow-state is macOS + Spotify today because that's what AppleScript gives us for
free. A different player or OS means a different control backend behind the same
`spotify.py` seam — that's a welcome contribution, but keep it behind the same
interface so the conductor doesn't have to care.
