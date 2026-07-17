"""Pull session state from remote hosts over SSH.

Direction matters: your laptop can reach the remote box, but the remote box
usually cannot reach back through NAT. So the laptop pulls. `flow-state watch`
on the remote prints a JSON line whenever its session set changes, plus a
heartbeat; we hold that connection open and read stdout. No inbound port on the
laptop, no tunnel, no new attack surface -- it rides SSH trust that already
exists.
"""

import json
import subprocess
import threading
import time

HEARTBEAT_TIMEOUT = 45  # no line for this long => treat the remote as gone


class RemoteWatcher:
    def __init__(self, name, ssh_target, remote_cmd, on_log=None):
        self.name = name
        self.ssh_target = ssh_target
        self.remote_cmd = remote_cmd
        self.on_log = on_log or (lambda m: None)
        self._sessions = []
        self._last_line = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        """Remote sessions, or [] if the link is stale.

        Failing to empty is the safe default: if we cannot see the remote host
        we must not keep believing it is busy, or the music would play straight
        through a turn that actually finished.
        """
        with self._lock:
            if time.time() - self._last_line > HEARTBEAT_TIMEOUT:
                return []
            return list(self._sessions)

    @property
    def connected(self):
        with self._lock:
            return time.time() - self._last_line <= HEARTBEAT_TIMEOUT

    def _loop(self):
        backoff = 1
        while not self._stop.is_set():
            try:
                p = subprocess.Popen(
                    [
                        "ssh",
                        "-o", "BatchMode=yes",
                        "-o", "ConnectTimeout=10",
                        # keep the pipe alive across flaky links
                        "-o", "ServerAliveInterval=15",
                        "-o", "ServerAliveCountMax=3",
                        self.ssh_target,
                        self.remote_cmd,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except OSError as e:
                self.on_log("remote %s: spawn failed: %s" % (self.name, e))
                self._sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            self.on_log("remote %s: connected" % self.name)
            backoff = 1
            for line in p.stdout:
                if self._stop.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sessions = payload.get("sessions", [])
                for s in sessions:
                    s["host"] = self.name
                with self._lock:
                    self._sessions = sessions
                    self._last_line = time.time()

            try:
                p.terminate()
            except Exception:
                pass
            with self._lock:
                self._sessions = []
            if not self._stop.is_set():
                self.on_log("remote %s: link dropped, retrying in %ds" % (self.name, backoff))
                self._sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _sleep(self, secs):
        self._stop.wait(secs)
