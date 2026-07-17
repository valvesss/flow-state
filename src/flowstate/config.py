"""Config loading. Stdlib only."""

import json
import os

HOME = os.path.expanduser("~")
ROOT = os.environ.get("FLOW_STATE_HOME", os.path.join(HOME, ".flow-state"))
RUN = os.path.join(ROOT, "run")
SESSIONS = os.path.join(RUN, "sessions")
EVENTS = os.path.join(ROOT, "events.jsonl")
CONFIG = os.path.join(ROOT, "config.json")
LOG = os.path.join(ROOT, "conductor.log")

DEFAULTS = {
    # Volume flow-state fades up to. "auto" learns it from whatever Spotify was
    # sitting at the first time we took control, so we hand back what you chose.
    "target_volume": "auto",
    "fade_in_ms": 1200,
    "fade_out_ms": 800,
    # How long the silence lasts before flow-state decides you aren't coming.
    #
    # This one number is the whole feel of the thing. The pause always fires the
    # instant a session finishes -- that is the notification, and it is not
    # negotiable. park_after only decides how long the room stays quiet waiting
    # for you before the idle session is written off as parked and the music
    # resumes. Too long and, with several parallel sessions, something is always
    # freshly idle and the music never plays. Too short and the cue is gone
    # before you look up.
    #
    # Measure it against your own log rather than trusting this default:
    #     flow-state tune
    "park_after_s": 90,
    "poll_ms": 250,
    # Hosts whose sessions also count. name is cosmetic; ssh is an ssh(1) target.
    "remotes": [],
    "dashboard_port": 7777,
    "enabled": True,
}


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        # A broken config must never take the music down with it.
        pass
    return cfg


def save(cfg):
    os.makedirs(ROOT, exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG)
