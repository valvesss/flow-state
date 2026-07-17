"""Stdlib unittest — no pytest, no install. `python3 -m unittest discover tests`"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

_tmp = tempfile.mkdtemp(prefix="flow-state-test-")
os.environ["FLOW_STATE_HOME"] = _tmp

from flowstate import config  # noqa: E402

config.ROOT = _tmp
config.RUN = os.path.join(_tmp, "run")
config.SESSIONS = os.path.join(config.RUN, "sessions")
config.EVENTS = os.path.join(_tmp, "events.jsonl")
config.CONFIG = os.path.join(_tmp, "config.json")

from flowstate import metrics, spotify, state  # noqa: E402

NOW = 1_000_000.0
PARK = 300


def sess(name, st, since_ago, project="p"):
    return {"session": name, "state": st, "since": NOW - since_ago,
            "updated": NOW, "project": project, "host": "local"}


class TestDecide(unittest.TestCase):
    def test_no_sessions_no_music(self):
        play, _, c = state.decide([], PARK, now=NOW)
        self.assertFalse(play)
        self.assertEqual(c, {"busy": 0, "waiting": 0, "parked": 0})

    def test_all_busy_plays(self):
        play, _, c = state.decide(
            [sess("a", "busy", 10), sess("b", "busy", 5)], PARK, now=NOW)
        self.assertTrue(play)
        self.assertEqual(c["busy"], 2)

    def test_one_fresh_idle_vetoes(self):
        """The whole point: any session that just finished is your cue."""
        play, reason, c = state.decide(
            [sess("a", "busy", 10), sess("b", "idle", 5)], PARK, now=NOW)
        self.assertFalse(play)
        self.assertEqual(c["waiting"], 1)
        self.assertIn("waiting on you", reason)

    def test_parked_idle_does_not_veto(self):
        """A session left idle past park_after has been seen and set aside."""
        play, _, c = state.decide(
            [sess("a", "busy", 10), sess("b", "idle", PARK + 1)], PARK, now=NOW)
        self.assertTrue(play)
        self.assertEqual(c["parked"], 1)
        self.assertEqual(c["waiting"], 0)

    def test_park_boundary_is_exclusive(self):
        play, _, _ = state.decide(
            [sess("a", "busy", 10), sess("b", "idle", PARK)], PARK, now=NOW)
        self.assertTrue(play, "exactly at park_after should already be parked")

    def test_idle_only_no_music(self):
        """Nothing is working, so there is nothing to wait for."""
        play, _, _ = state.decide([sess("a", "idle", PARK + 99)], PARK, now=NOW)
        self.assertFalse(play)


class TestStateFiles(unittest.TestCase):
    def setUp(self):
        os.makedirs(config.SESSIONS, exist_ok=True)
        for f in os.listdir(config.SESSIONS):
            os.remove(os.path.join(config.SESSIONS, f))

    def test_write_read_remove(self):
        state.write("sess-1", state.BUSY, cwd="/home/u/proj")
        got = state.read_all(prune=False)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["state"], "busy")
        self.assertEqual(got[0]["project"], "proj")
        state.remove("sess-1")
        self.assertEqual(state.read_all(prune=False), [])

    def test_since_preserved_across_same_state_writes(self):
        """park_after must measure time-in-state, not time-since-last-write."""
        state.write("s", state.IDLE)
        first = state.read_all(prune=False)[0]["since"]
        time.sleep(0.02)
        state.write("s", state.IDLE)
        self.assertEqual(state.read_all(prune=False)[0]["since"], first)

    def test_since_resets_on_state_change(self):
        state.write("s", state.IDLE)
        first = state.read_all(prune=False)[0]["since"]
        time.sleep(0.02)
        state.write("s", state.BUSY)
        self.assertGreater(state.read_all(prune=False)[0]["since"], first)

    def test_dead_pid_is_pruned(self):
        state.write("ghost", state.BUSY)
        p = os.path.join(config.SESSIONS, "ghost.json")
        with open(p) as f:
            rec = json.load(f)
        rec["pid"] = 999999  # not a real process
        rec["updated"] = time.time()
        with open(p, "w") as f:
            json.dump(rec, f)
        self.assertEqual(state.read_all(prune=True), [],
                         "a crashed busy session must not hold the music on forever")

    def test_no_pid_falls_back_to_ttl(self):
        state.write("old", state.BUSY)
        p = os.path.join(config.SESSIONS, "old.json")
        with open(p) as f:
            rec = json.load(f)
        rec["pid"] = None
        rec["updated"] = time.time() - state.STALE_AFTER_S - 1
        with open(p, "w") as f:
            json.dump(rec, f)
        self.assertEqual(state.read_all(prune=True), [])

    def test_traversal_is_not_possible(self):
        state.write("../../etc/passwd", state.BUSY)
        for f in os.listdir(config.SESSIONS):
            self.assertNotIn("/", f)
            self.assertNotIn("..", f)


class TestMetrics(unittest.TestCase):
    def setUp(self):
        open(config.EVENTS, "w").close()

    def _log(self, recs):
        with open(config.EVENTS, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def test_flow_and_response(self):
        t = 10_000.0
        self._log([
            {"ts": t, "ev": "transition", "host": "local", "session": "a",
             "project": "proj", "from": None, "to": "busy"},
            {"ts": t + 100, "ev": "music", "action": "play", "track": "T", "artist": "A"},
            {"ts": t + 160, "ev": "transition", "host": "local", "session": "a",
             "project": "proj", "from": "busy", "to": "idle"},
            {"ts": t + 160, "ev": "music", "action": "pause"},
            {"ts": t + 200, "ev": "transition", "host": "local", "session": "a",
             "project": "proj", "from": "idle", "to": "busy"},
            {"ts": t + 300, "ev": "transition", "host": "local", "session": "a",
             "project": "proj", "from": "busy", "to": "gone"},
        ])
        m = metrics.compute(since=t - 1, park_after_s=PARK, now=t + 300)
        self.assertAlmostEqual(m["flow_time"], 60, places=3)
        self.assertAlmostEqual(m["longest_flow"], 60, places=3)
        # the idle stretch was 40s and it was answered
        self.assertEqual(m["response"]["count"], 1)
        self.assertAlmostEqual(m["response"]["median"], 40, places=3)
        # 40s of idle, all of it inside park_after => all attention
        self.assertAlmostEqual(m["attention_time"], 40, places=3)
        self.assertEqual(m["turns"], 2)
        self.assertEqual(m["soundtrack"][0]["track"], "T")
        self.assertEqual(m["projects"][0]["project"], "proj")

    def test_attention_stops_accruing_once_parked(self):
        t = 10_000.0
        self._log([
            {"ts": t, "ev": "transition", "host": "local", "session": "a",
             "project": "p", "from": None, "to": "idle"},
            {"ts": t + 1000, "ev": "transition", "host": "local", "session": "a",
             "project": "p", "from": "idle", "to": "busy"},
        ])
        m = metrics.compute(since=t - 1, park_after_s=PARK, now=t + 1000)
        self.assertAlmostEqual(m["attention_time"], PARK, delta=1,
                               msg="attention should stop at the park boundary")

    def test_long_gap_excluded_from_response(self):
        t = 10_000.0
        gap = metrics.MAX_RESPONSE + 60
        self._log([
            {"ts": t, "ev": "transition", "host": "local", "session": "a",
             "project": "p", "from": None, "to": "idle"},
            {"ts": t + gap, "ev": "transition", "host": "local", "session": "a",
             "project": "p", "from": "idle", "to": "busy"},
        ])
        m = metrics.compute(since=t - 1, park_after_s=PARK, now=t + gap)
        self.assertEqual(m["response"]["count"], 0, "lunch is not response time")

    def test_open_music_block_runs_to_now(self):
        t = 10_000.0
        self._log([{"ts": t, "ev": "music", "action": "play"}])
        m = metrics.compute(since=t - 1, park_after_s=PARK, now=t + 50)
        self.assertAlmostEqual(m["flow_time"], 50, places=3)

    def test_empty_log_is_all_zeros(self):
        m = metrics.compute(since=None, park_after_s=PARK, now=NOW)
        self.assertEqual(m["flow_time"], 0)
        self.assertEqual(m["response"]["count"], 0)
        self.assertEqual(m["lanes"], [])


class TestFadeScripts(unittest.TestCase):
    def test_guarded_against_launching_spotify(self):
        for s in (spotify.fade_in_script(70, 1200), spotify.fade_out_script(70, 70, 800)):
            self.assertIn('if application "Spotify" is running then', s,
                          "a bare tell would LAUNCH Spotify")

    def test_fade_in_starts_silent_and_ends_at_target(self):
        s = spotify.fade_in_script(70, 1200)
        self.assertIn("set sound volume to 0", s)
        self.assertIn("play", s)
        self.assertTrue(s.strip().split("\n")[-3].strip().endswith("70"))

    def test_fade_out_restores_resting_volume_after_pause(self):
        s = spotify.fade_out_script(70, 65, 800)
        tail = [line.strip() for line in s.split("\n")][-4:]
        self.assertIn("pause", tail)
        self.assertIn("set sound volume to 65", tail,
                      "must hand back a usable volume, not leave Spotify at 0")

    def test_single_osascript_invocation(self):
        s = spotify.fade_in_script(70, 1200)
        self.assertEqual(s.count("repeat with i from 1 to"), 1)
        self.assertEqual(s.count("end repeat"), 1)


class TestVolumeDrift(unittest.TestCase):
    """Spotify's volume scale is quantised: write 79, read back 78; write 78,
    read back 77. Real hardware, verified. If `auto` believed that readback it
    would ratchet the volume to silence over a day.
    """

    def setUp(self):
        from flowstate import conductor
        self.c = conductor
        os.makedirs(config.RUN, exist_ok=True)
        try:
            os.remove(conductor.LEARNED)
        except OSError:
            pass
        self._real_volume = spotify.volume

    def tearDown(self):
        spotify.volume = self._real_volume

    def test_auto_does_not_ratchet_down(self):
        cfg = {"target_volume": "auto"}
        self.c._write_learned(79)
        # every read comes back one below what we last set, forever
        box = {"v": 79}
        spotify.volume = lambda: box["v"]
        for _ in range(50):
            got = self.c.relearn_volume(cfg)
            box["v"] = got - 1  # Spotify's lossy readback
        self.assertEqual(self.c._read_learned(), 79,
                         "learned volume must not walk downward from quantisation noise")

    def test_real_user_change_is_learned(self):
        cfg = {"target_volume": "auto"}
        self.c._write_learned(79)
        spotify.volume = lambda: 40  # you dragged the slider
        self.assertEqual(self.c.relearn_volume(cfg), 40)
        self.assertEqual(self.c._read_learned(), 40)

    def test_explicit_target_is_never_relearned(self):
        cfg = {"target_volume": 55}
        spotify.volume = lambda: 20
        self.assertIsNone(self.c.relearn_volume(cfg))
        self.assertEqual(self.c.resting_volume(cfg), 55)

    def test_muted_spotify_is_not_learned_as_resting(self):
        cfg = {"target_volume": "auto"}
        self.c._write_learned(79)
        spotify.volume = lambda: 0
        self.assertEqual(self.c.relearn_volume(cfg), 79)
        self.assertEqual(self.c._read_learned(), 79)

    def test_mid_fade_read_is_not_learned(self):
        """The vol=9 bug: a session flips idle mid fade-in, the volume read is a
        low point on the perceptual ramp, and it must NOT become the resting
        level. Reproduced from real use where resting walked down to 9.
        """
        cfg = {"target_volume": "auto"}
        self.c._write_learned(75)

        class FakeFader:
            def __init__(self): self.flying = True
            def in_flight(self): return self.flying

        fader = FakeFader()
        spotify.volume = lambda: 9  # one third up a fade-in

        # while the fade is ramping, the low read is refused
        self.assertEqual(self.c.relearn_volume(cfg, fader), 75)
        self.assertEqual(self.c._read_learned(), 75, "must not learn a mid-ramp value")

        # once the fade settles at the true resting level, a real change sticks
        fader.flying = False
        spotify.volume = lambda: 55
        self.assertEqual(self.c.relearn_volume(cfg, fader), 55)

    def test_rapid_flip_does_not_ratchet_via_fades(self):
        """Many pause/play flips that each catch a fade mid-ramp must leave the
        resting volume where the user put it.
        """
        cfg = {"target_volume": "auto"}
        self.c._write_learned(75)

        class FakeFader:
            def in_flight(self): return True  # always mid-fade during the burst

        fader = FakeFader()
        for v in (9, 3, 25, 1, 40, 8):  # assorted points along ramps
            spotify.volume = lambda v=v: v
            self.c.relearn_volume(cfg, fader)
        self.assertEqual(self.c._read_learned(), 75,
                         "resting volume must survive a burst of interrupted fades")


class TestEventSchema(unittest.TestCase):
    def test_every_event_carries_version_and_bg_capable(self):
        from flowstate import events
        rec = events.emit("transition", host="local", session="x", bg=2,
                          **{"from": "idle", "to": "busy"})
        self.assertEqual(rec["v"], events.SCHEMA)
        self.assertEqual(rec["bg"], 2, "transitions must be able to carry the bg flag")


class TestMetricsOutliersAndAway(unittest.TestCase):
    """#2: stop dropping outliers silently, and don't count sleep as response."""

    def setUp(self):
        open(config.EVENTS, "w").close()

    def _log(self, recs):
        with open(config.EVENTS, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def test_long_gap_is_kept_as_outlier_not_dropped(self):
        t = 10_000.0
        big = metrics.MAX_RESPONSE + 3600
        self._log([
            {"ts": t, "ev": "transition", "host": "local", "session": "a",
             "project": "p", "from": None, "to": "idle"},
            {"ts": t + big, "ev": "transition", "host": "local", "session": "a",
             "project": "p", "from": "idle", "to": "busy"},
        ])
        m = metrics.compute(since=t - 1, park_after_s=PARK, now=t + big + 1)
        r = m["response"]
        self.assertEqual(r["count"], 0, "a huge gap must not be a typical response")
        self.assertEqual(r["outliers"], 1, "but it must be KEPT and surfaced, not dropped")
        self.assertAlmostEqual(r["outlier_max"], big, delta=1)

    def test_gap_while_away_is_not_a_response(self):
        t = 10_000.0
        self._log([
            {"ts": t, "ev": "transition", "host": "local", "session": "a",
             "project": "p", "from": None, "to": "idle"},
            {"ts": t + 20, "ev": "presence", "state": "away", "idle_s": 600},
            {"ts": t + 5000, "ev": "presence", "state": "back", "idle_s": 4900},
            {"ts": t + 5000, "ev": "transition", "host": "local", "session": "a",
             "project": "p", "from": "idle", "to": "busy"},
        ])
        m = metrics.compute(since=t - 1, park_after_s=PARK, now=t + 5001)
        r = m["response"]
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["away_gaps"], 1, "the sleep gap is 'away', not a 1h23m response")
        self.assertEqual(r["outliers"], 0)
        self.assertGreater(m["away_time"], 4000, "away time is reported honestly")

    def test_normal_response_still_measured(self):
        t = 10_000.0
        self._log([
            {"ts": t, "ev": "transition", "host": "local", "session": "a",
             "project": "p", "from": None, "to": "idle"},
            {"ts": t + 40, "ev": "transition", "host": "local", "session": "a",
             "project": "p", "from": "idle", "to": "busy"},
        ])
        m = metrics.compute(since=t - 1, park_after_s=PARK, now=t + 41)
        r = m["response"]
        self.assertEqual(r["count"], 1)
        self.assertAlmostEqual(r["median"], 40, places=3)
        self.assertEqual(r["outliers"], 0)
        self.assertEqual(r["away_gaps"], 0)


class TestPresence(unittest.TestCase):
    """Reported from real use: sessions ran overnight, the decision stayed 'play'
    the whole time, and music played (and counted as flow) for ~5.5h while the
    user slept. flow-state inferred 'you're waiting' from sessions but never
    checked whether a human was actually there.
    """

    def test_present_when_recently_active(self):
        from flowstate import conductor
        self.assertFalse(conductor.is_away(30, 600))
        self.assertFalse(conductor.is_away(599, 600))

    def test_away_past_threshold(self):
        from flowstate import conductor
        self.assertTrue(conductor.is_away(600, 600))
        self.assertTrue(conductor.is_away(5 * 3600, 600), "asleep for hours is away")

    def test_cant_tell_reads_as_present(self):
        """A missing HID signal (idle_s 0) must never be the reason music stops."""
        from flowstate import conductor
        self.assertFalse(conductor.is_away(0, 600))

    def test_hid_parse(self):
        from flowstate import conductor
        sample = (
            '  | {\n    "HIDIdleTime" = 44000000000\n    "HIDkeyboardBacklight" = 0\n  }'
        )
        real = conductor.subprocess.run
        try:
            class R:
                stdout = sample
            conductor.subprocess.run = lambda *a, **k: R()
            self.assertEqual(conductor.hid_idle_seconds(), 44)
        finally:
            conductor.subprocess.run = real

    def test_hid_failure_is_none_not_zero(self):
        from flowstate import conductor
        real = conductor.subprocess.run

        def boom(*a, **k):
            raise OSError("no ioreg")
        try:
            conductor.subprocess.run = boom
            self.assertIsNone(conductor.hid_idle_seconds(),
                              "a failed read must be None (present), not a bogus number")
        finally:
            conductor.subprocess.run = real


class TestHook(unittest.TestCase):
    def test_hook_never_raises_on_garbage(self):
        from flowstate import cli

        class A:
            pass

        for junk in ("", "not json", "{}", '{"session_id": null}', "[]"):
            import io
            old = sys.stdin
            sys.stdin = io.StringIO(junk)
            try:
                self.assertEqual(cli.cmd_hook(A()), 0,
                                 "a nonzero exit from a Stop hook blocks the turn")
            finally:
                sys.stdin = old

    def test_event_mapping(self):
        from flowstate import cli
        self.assertEqual(cli.EVENT_STATE["UserPromptSubmit"], "busy")
        self.assertEqual(cli.EVENT_STATE["Notification"], "idle")
        self.assertNotIn("SubagentStop", cli.EVENT_STATE,
                         "SubagentStop carries the PARENT session_id — hooking it "
                         "would mark a live session idle whenever a subagent ended")
        self.assertNotIn("Stop", cli.EVENT_STATE,
                         "Stop is not a fixed mapping — it depends on background_tasks")


class TestStopWithBackgroundWork(unittest.TestCase):
    """A turn ending is not Claude being done.

    Reported from real use: a deep-research sweep ran for many minutes, Stop
    fired the instant it handed off to the background, the session was marked
    idle, `busy` dropped to zero, and the music never started -- during exactly
    the stretch where there was nothing for a human to do.
    """

    def _stop(self, tasks):
        from flowstate import cli
        return cli._stop_state({"hook_event_name": "Stop", "background_tasks": tasks})

    def test_plain_stop_is_your_move(self):
        st, extra = self._stop([])
        self.assertEqual(st, "idle")
        self.assertEqual(extra["bg"], 0)

    def test_missing_field_is_your_move(self):
        from flowstate import cli
        st, _ = cli._stop_state({"hook_event_name": "Stop"})
        self.assertEqual(st, "idle", "absent background_tasks must not read as busy")

    def test_running_subagent_keeps_session_working(self):
        st, extra = self._stop([
            {"id": "a1", "type": "subagent", "status": "running",
             "description": "Deep research sweep", "agent_type": "Explore"},
        ])
        self.assertEqual(st, "busy", "a running background agent means you are NOT needed")
        self.assertEqual(extra["bg"], 1)
        self.assertEqual(extra["bg_desc"], "Deep research sweep")

    def test_completed_tasks_do_not_keep_it_working(self):
        st, extra = self._stop([
            {"id": "a1", "type": "subagent", "status": "completed"},
            {"id": "a2", "type": "subagent", "status": "failed"},
        ])
        self.assertEqual(st, "idle")
        self.assertEqual(extra["bg"], 0)

    def test_mixed_counts_only_running(self):
        st, extra = self._stop([
            {"id": "a1", "status": "completed"},
            {"id": "a2", "status": "running", "description": "still going"},
            {"id": "a3", "status": "running", "description": "also going"},
        ])
        self.assertEqual(st, "busy")
        self.assertEqual(extra["bg"], 2)

    def test_background_bash_counts_too(self):
        st, extra = self._stop([
            {"id": "b1", "type": "bash", "status": "running", "description": "pytest -q"},
        ])
        self.assertEqual(st, "busy", "background shell work is work too")
        self.assertEqual(extra["bg_desc"], "pytest -q")

    def test_garbage_entries_are_survived(self):
        for junk in ([None], ["nope"], [{}], [{"status": None}], "not-a-list"):
            st, _ = self._stop(junk)
            self.assertEqual(st, "idle")

    def test_background_session_plays_music_alone(self):
        """The end-to-end shape of the reported bug."""
        s = sess("deep", "busy", 5)
        s["bg"] = 1
        play, reason, counts = state.decide([s], PARK, now=NOW)
        self.assertTrue(play, "a lone session doing background work must still play music")
        self.assertIn("background", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
