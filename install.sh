#!/usr/bin/env sh
# flow-state installer. POSIX sh, no dependencies beyond python3 + ssh.
#
#   ./install.sh                  install here (hooks + conductor if macOS)
#   ./install.sh --no-agent       skip the launchd agent
#   ./install.sh --remote build-box   install on a remote host and wire it up
#
set -eu

ROOT="${FLOW_STATE_HOME:-$HOME/.flow-state}"
SRC="$(cd "$(dirname "$0")" && pwd)"
AGENT=1
REMOTE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --no-agent) AGENT=0 ;;
    --remote) REMOTE="${2:?--remote needs an ssh target}"; shift ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# ---------------------------------------------------------------- remote
if [ -n "$REMOTE" ]; then
  echo "→ installing flow-state on $REMOTE"
  ssh "$REMOTE" 'mkdir -p ~/.flow-state'
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "$SRC/bin" "$SRC/src" "$REMOTE:.flow-state/"
  else
    tar -C "$SRC" -cf - bin src | ssh "$REMOTE" 'tar -C ~/.flow-state -xf -'
  fi
  ssh "$REMOTE" 'chmod +x ~/.flow-state/bin/flow-state && ~/.flow-state/bin/flow-state install-hooks'

  # Register the remote with the local conductor, without clobbering the config.
  python3 - "$REMOTE" <<'PY'
import json, os, sys
target = sys.argv[1]
root = os.environ.get("FLOW_STATE_HOME", os.path.expanduser("~/.flow-state"))
path = os.path.join(root, "config.json")
try:
    with open(path) as f: cfg = json.load(f)
except Exception:
    cfg = {}
remotes = cfg.setdefault("remotes", [])
if not any(r.get("ssh") == target for r in remotes):
    remotes.append({"name": target, "ssh": target})
    os.makedirs(root, exist_ok=True)
    with open(path, "w") as f: json.dump(cfg, f, indent=2)
    print("→ registered remote '%s' in %s" % (target, path))
else:
    print("→ remote '%s' already registered" % target)
PY
  echo "→ done. Restart the conductor to pick it up:"
  echo "     launchctl kickstart -k gui/$(id -u)/com.flowstate.conductor"
  exit 0
fi

# ---------------------------------------------------------------- local
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }

echo "→ installing to $ROOT"
mkdir -p "$ROOT"
# ${ROOT:?} so an empty ROOT aborts rather than expanding to `rm -rf /bin`.
rm -rf "${ROOT:?}/bin" "${ROOT:?}/src"
cp -R "$SRC/bin" "$SRC/src" "$ROOT/"
chmod +x "$ROOT/bin/flow-state"

"$ROOT/bin/flow-state" install-hooks

if [ "$(uname -s)" = "Darwin" ] && [ "$AGENT" -eq 1 ]; then
  PLIST="$HOME/Library/LaunchAgents/com.flowstate.conductor.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  sed "s|__ROOT__|$ROOT|g" "$SRC/packaging/com.flowstate.conductor.plist" > "$PLIST"
  launchctl bootout "gui/$(id -u)/com.flowstate.conductor" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "→ conductor running under launchd (starts at login)"
elif [ "$(uname -s)" != "Darwin" ]; then
  echo "→ not macOS: this host reports sessions only; run the conductor on your Mac"
fi

echo
"$ROOT/bin/flow-state" doctor || true
cat <<EOF

  flow-state installed.

    $ROOT/bin/flow-state status     what it thinks right now
    $ROOT/bin/flow-state dash       the dashboard
    $ROOT/bin/flow-state off        stop touching my music

  Put $ROOT/bin on your PATH to drop the prefix.
EOF
