#!/usr/bin/env python3
"""Thinking chime: start delay, repeat, stop on speech or leaving thinking, toggle file."""
import json, os, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "wake"))
import wait_tone as wt


class WaitToneState(unittest.TestCase):
    def setUp(self):
        self.t = wt.WaitTone()

    def test_delay_then_repeat(self):
        self.t.note_phase("thinking", now=100.0)
        self.assertFalse(self.t.due(100.0 + wt.WAIT_TONE_DELAY - 0.01))
        self.assertTrue(self.t.claim(100.0 + wt.WAIT_TONE_DELAY))
        self.t.playing = False
        self.assertFalse(self.t.due(100.5))
        self.assertTrue(self.t.due(100.0 + wt.WAIT_TONE_DELAY + wt.WAIT_TONE_EVERY))

    def test_fast_reply_never_chimes(self):
        self.t.note_phase("thinking", now=10.0)
        self.t.note_phase("speaking", now=10.2)
        self.assertFalse(self.t.due(20.0))

    def test_speech_blocks_and_rearms(self):
        self.t.note_phase("thinking", now=0.0)
        self.t.begin_speech()
        self.assertFalse(self.t.due(5.0))
        self.t.end_speech(now=5.0)
        self.assertFalse(self.t.due(5.0 + wt.WAIT_TONE_DELAY - 0.01))
        self.assertTrue(self.t.due(5.0 + wt.WAIT_TONE_DELAY))

    def test_disabled_never_due(self):
        self.t.note_phase("thinking", now=0.0)
        self.assertFalse(self.t.due(10.0, enabled=False))

    def test_toggle_file_default_on(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "wait-tone.json")
            self.assertTrue(wt.wait_tone_enabled(p))
            json.dump({"on": False}, open(p, "w"))
            self.assertFalse(wt.wait_tone_enabled(p))
            json.dump({"on": "no"}, open(p, "w"))
            self.assertTrue(wt.wait_tone_enabled(p))

    def test_night_clock_window(self):
        import time
        night = time.struct_time((2026, 10, 27, 23, 0, 0, 1, 300, -1))
        day = time.struct_time((2026, 10, 27, 12, 0, 0, 1, 300, -1))
        self.assertTrue(wt.night_clock_quiet("22-6", night))
        self.assertFalse(wt.night_clock_quiet("22-6", day))
        self.assertFalse(wt.night_clock_quiet("", night))


if __name__ == "__main__":
    unittest.main()
