#!/usr/bin/env python3
"""Nix 0.13.5 L2: the training listening window is bounded (prompt TTL, pause cap), on a fake monotonic clock."""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "wake"))
import tempfile  # noqa: E402

import numpy as np  # noqa: E402

import grok_wake as gw  # noqa: E402


def _tone():
    t = np.arange(1280)
    return (np.sin(t * 0.2) * 3000).astype(np.int16)


def _zeros(raw=False):
    return np.zeros(1280, np.int16)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TrainingLimits(unittest.TestCase):
    """0.13.7: no spoken stop. Training ends by sample count, Stop on the tile, or these time limits."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old_dir = gw.ENROLL_DIR
        self.state = dict(gw.ENROLL_STATE)
        gw.ENROLL_DIR = self.d
        gw.ENROLL_STATE.update(busy=False, pause=False, discard=False, prompt=None)
        self.post, self.sleep, self.popen = gw.post_state, gw.time.sleep, gw.subprocess.Popen
        gw.post_state = lambda state=None, **kw: None
        gw.time.sleep = lambda s: None
        gw.subprocess.Popen = lambda *a, **k: None
        self.clock = _Clock()
        self.old_mono = gw._mono
        gw._mono = self.clock

    def tearDown(self):
        gw._mono = self.old_mono
        gw.ENROLL_DIR = self.old_dir
        gw.ENROLL_STATE.clear()
        gw.ENROLL_STATE.update(self.state)
        gw.post_state, gw.time.sleep, gw.subprocess.Popen = self.post, self.sleep, self.popen
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_constants(self):
        self.assertEqual(gw.PROMPT_TTL_S, 120)
        self.assertEqual(gw.PAUSE_MAX_S, 600)

    def test_prompt_expires_on_the_listener_tick(self):
        posted = []
        gw.post_state = lambda state=None, **kw: posted.append(state)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(gw.open_enroll_tile({"phrase": "hey_juniper", "persona": "juniper", "name": "Juniper"})[0], 200)
            gw._prompt_tick()
            self.clock.t += gw.PROMPT_TTL_S - 1
            gw._prompt_tick()
            self.assertIsNotNone(gw.ENROLL_STATE["prompt"])
            self.clock.t += 2
            gw._prompt_tick()
            gw._prompt_tick()
        self.assertIsNone(gw.ENROLL_STATE["prompt"])
        self.assertFalse(gw._training_holding())
        self.assertEqual(posted, ["enroll", "idle"])
        out = buf.getvalue()
        self.assertEqual(out.count("\n"), 1)
        self.assertNotIn("juniper", out.lower())

    def test_prompt_hold_returns_after_ttl(self):
        reads = {"n": 0}

        def read16(raw=False):
            reads["n"] += 1
            if reads["n"] == 5:
                self.clock.t += gw.PROMPT_TTL_S + 1
            if reads["n"] > 50:
                raise AssertionError("hold never ended")
            return _zeros()

        with redirect_stdout(io.StringIO()):
            gw.open_enroll_tile({"phrase": "hey_juniper", "persona": "juniper", "name": "Juniper"})
            gw._enroll_hold(read16)
        self.assertIsNone(gw.ENROLL_STATE["prompt"])
        self.assertLess(reads["n"], 10)

    def test_over_cap_pause_discards_clips_and_goes_idle(self):
        posted = []
        gw.post_state = lambda state=None, **kw: posted.append(state)
        raw_n = {"n": 0, "after": 0}

        def read16(raw=False):
            if raw:
                raw_n["n"] += 1
                if raw_n["n"] == 3 and not gw.ENROLL_STATE.get("pause"):
                    self.assertEqual(gw.enroll_control({"op": "pause"})[0], 200)
            if gw.ENROLL_STATE.get("pause"):
                raw_n["after"] += 1
                if raw_n["after"] == 5:
                    self.clock.t += gw.PAUSE_MAX_S + 1
                if raw_n["after"] > 200:
                    raise AssertionError("pause never ended")
            return _zeros()

        buf = io.StringIO()
        with redirect_stdout(buf):
            gw.enroll({"phrase": "hey_juniper", "persona": "juniper", "count": 3}, read16, "default")
        self.assertEqual(os.listdir(os.path.join(self.d, "hey_juniper")), [])
        self.assertEqual(gw.ENROLL_STATE["last"]["stage"], "cancelled")
        self.assertFalse(gw.ENROLL_STATE["busy"])
        self.assertFalse(gw.ENROLL_STATE["pause"])
        self.assertIn("idle", posted)
        self.assertIn("enroll pause timed out", buf.getvalue())
        self.assertNotIn("enroll discarded", buf.getvalue())
        self.assertNotIn("timed out; discarding hey_juniper", buf.getvalue())

    def test_short_pause_is_not_discarded(self):
        gw.ENROLL_STATE.update(busy=True, pause=False, discard=False)
        gw.enroll_control({"op": "pause"})
        self.clock.t += gw.PAUSE_MAX_S - 1
        with redirect_stdout(io.StringIO()):
            self.assertTrue(gw._training_holding())
        self.assertFalse(gw.ENROLL_STATE["discard"])
        gw.ENROLL_STATE.update(busy=False, pause=False)


class TrainingCap(TrainingLimits):
    """Nix 0.13.5 I1: pause/resume can't extend training past TRAIN_MAX_S."""

    def test_cap_constant(self):
        self.assertEqual(gw.TRAIN_MAX_S, 1800)

    def test_resume_clears_pause_deadline(self):
        gw.ENROLL_STATE.update(busy=True, pause=False, discard=False)
        gw.enroll_control({"op": "pause"})
        self.assertIsNotNone(gw.ENROLL_STATE.get("pause_until"))
        gw.enroll_control({"op": "resume"})
        self.assertIsNone(gw.ENROLL_STATE.get("pause_until"))
        gw.ENROLL_STATE.update(busy=False)

    def test_pause_resume_loop_discards_at_the_30_minute_cap(self):
        posted = []
        gw.post_state = lambda state=None, **kw: posted.append(state)
        tick = {"n": 0, "cycles": 0}

        def read16(raw=False):
            tick["n"] += 1
            if tick["n"] > 5000:
                raise AssertionError("training never ended")
            if raw and tick["n"] % 7 == 0 and not gw.ENROLL_STATE.get("discard"):
                # Pause for 9 minutes, then resume: every pause stays under PAUSE_MAX_S.
                gw.enroll_control({"op": "pause"})
                self.clock.t += gw.PAUSE_MAX_S - 60
                tick["cycles"] += 1
            elif gw.ENROLL_STATE.get("pause"):
                gw.enroll_control({"op": "resume"})
            # 0.13.9 (rev1.6): one silent clip now ends a session after a 3 s countdown, so the clips here
            # carry a steady tone; the loop runs until the 30-minute cap, as before.
            return _tone()

        buf = io.StringIO()
        with redirect_stdout(buf):
            gw.enroll({"phrase": "hey_juniper", "persona": "juniper", "count": 25}, read16, "default")
        out = buf.getvalue()
        self.assertEqual(os.listdir(os.path.join(self.d, "hey_juniper")), [])
        self.assertEqual(gw.ENROLL_STATE["last"]["stage"], "cancelled")
        self.assertFalse(gw.ENROLL_STATE["busy"])
        self.assertIn("idle", posted)
        self.assertIn("enroll training time limit reached; discarding", out)
        self.assertNotIn("pause timed out", out)
        self.assertNotIn("enroll discarded", out)
        self.assertGreaterEqual(tick["cycles"], 3)
        self.assertLessEqual(tick["cycles"], 5)

    def test_prompt_cap_set_at_open(self):
        with redirect_stdout(io.StringIO()):
            gw.open_enroll_tile({"phrase": "hey_juniper", "persona": "juniper", "name": "Juniper"})
        self.assertEqual(gw.ENROLL_STATE.get("train_until"), self.clock.t + gw.TRAIN_MAX_S)
        with redirect_stdout(io.StringIO()):
            gw.enroll_control({"op": "discard"})


if __name__ == "__main__":
    unittest.main()
