"""Append-only event log. The conductor is the sole writer.

Hooks deliberately do not write here. The conductor already observes every
transition (local and remote), so making it the only writer removes a
multi-writer merge, and sidesteps clock skew between hosts -- every
timestamp on the line comes from one clock.
"""

import json
import os
import time

from . import config

# Bumped when the meaning of a field changes, so analysis can tell events
# written under one decision rule from another. Absent `v` means pre-versioning.
SCHEMA = 1


def emit(ev, **fields):
    rec = {"v": SCHEMA, "ts": round(time.time(), 3), "ev": ev}
    rec.update(fields)
    try:
        os.makedirs(config.ROOT, exist_ok=True)
        with open(config.EVENTS, "a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except OSError:
        pass  # never let logging take the music down
    return rec


def read(since=None):
    out = []
    try:
        with open(config.EVENTS) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since and rec.get("ts", 0) < since:
                    continue
                out.append(rec)
    except OSError:
        pass
    return out
