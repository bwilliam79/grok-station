#!/usr/bin/env python3
"""0.13.9 fix round items 1a and 1b (Noor gs-fixround-0139 rev1, gs-voice-train rev1.6).

1a: every recording stage keeps "Say “<phrase>”" as the heading; the status moves to the secondary line.
1b: one silent clip starts a 3 s on-screen countdown. Speech during it cancels it and the clip retries.
Silence through it stops: "Stopped. Nothing was saved." (top-up: "Nothing new"), restart line under it, 4 s.
Fails on 0.13.8 (status headings; two silent clips; a notice tile).
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "wake"))
import grok_station as gs  # noqa: E402
import grok_wake as gw  # noqa: E402

Q = "Say “Hey Finn”"


class PinnedHeading(unittest.TestCase):
    def test_heading_and_secondary_lines(self):
        want = {
            ("ready", 1, ""): "Get ready… Clip 1 of 5",
            ("count", 1, "2"): "Recording in 2…",
            ("recording", 3, ""): "Listening… Clip 3 of 5",
            ("saved", 3, "next"): "Clip 3 saved. Next…",
            ("quiet", 3, ""): "I didn't hear it. Again after the chime.",
            ("loud", 3, ""): "Too loud. Step back a little. Clip 3 will retry.",
            ("stopping", 2, "1"): "I didn't hear it. Stopping in 1…",
        }
        for (stage, i, extra), sub in want.items():
            head, got, color, _s, _b = gs.enroll_copy("Hey Finn", i, 5, stage, extra)
            self.assertEqual((head, got), (Q, sub), stage)
        self.assertEqual(gs.enroll_copy("Hey Finn", 3, 5, "recording", "")[2], gs.LISTENING)
        self.assertEqual(gs.enroll_copy("Hey Finn", 3, 5, "custom", "raw text")[:2], (Q, "raw text"))

    def test_ended_sessions_keep_their_heading(self):
        self.assertEqual(gs.enroll_copy("Hey Finn", 5, 5, "saved", "")[0], "Saved. Checking the recordings.")
        self.assertEqual(gs.enroll_copy("Hey Finn", 1, 5, "stopped", "")[0], "Stopped. Nothing was saved.")
        self.assertEqual(gs.enroll_copy("Hey Finn", 1, 5, "stopped", "topup")[0], "Stopped. Nothing new was saved.")

    def test_hint_and_holds(self):
        self.assertEqual(gs.QUIET_HINT, "To stop, stay quiet for 3 seconds.")
        self.assertIn("stopping", gs._QUIET_HINT_STAGES)
        self.assertNotIn("stopped", gs._QUIET_HINT_STAGES)
        self.assertEqual(gs.state_hold("enroll", False, "1/5|stopped|"), 4)
        self.assertEqual(gs.enroll_parse({"heard": "2/5|stopping|3"})[2:], ("stopping", "3"))

    def test_stopped_tile_draws_restart_line(self):
        import pygame
        pygame.init()
        seen = []
        real = gs.font

        class F:
            def __init__(self, f):
                self.f = f

            def __getattr__(self, k):
                return getattr(self.f, k)

            def render(self, text, *a, **k):
                seen.append(text)
                return self.f.render(text, *a, **k)
        gs.font = lambda kind, px: F(real(kind, px))
        try:
            surf = pygame.Surface((1920, 1080))
            gs.draw_enroll(surf, gs.Layout(1920, 1080), __import__("time").localtime(),
                           {"from": "Finn", "voice": True, "state": "enroll", "text": "“Hey Finn”", "heard": "1/5|stopped|"}, 1.0)
        finally:
            gs.font = real
        self.assertIn("Stopped. Nothing was saved.", seen)
        self.assertIn("Say 'Hey Grok, train my wake word for Finn' to try again.", " ".join(seen))  # wraps to 2 lines
        self.assertNotIn(gs.QUIET_HINT, seen)


class Countdown(unittest.TestCase):
    def _run(self, speech_once, topup=False):
        posted, notices = [], []
        root = tempfile.mkdtemp()
        old = (gw.post_state, gw.subprocess.Popen, gw.ENROLL_DIR, gw.time.sleep, gw.last_session)
        state = {"spoke": False}

        def post_state(state_=None, **kw):
            if state_ == "notice":
                notices.append(kw.get("text"))
            heard = str(kw.get("heard") or "")
            if "|" in heard:
                posted.append(heard.split("|")[1])

        def read16(raw=False):
            if not raw and speech_once and not state["spoke"] and posted and posted[-1] == "stopping":
                state["spoke"] = True
                t = gw.np.arange(1280)
                return (gw.np.sin(t * 0.2) * 3000).astype(gw.np.int16)
            return gw.np.zeros(1280, gw.np.int16)

        gw.post_state = post_state
        gw.subprocess.Popen = lambda *a, **k: None
        gw.ENROLL_DIR = root
        gw.time.sleep = lambda s: None
        gw.last_session = lambda phrase: "20261029-000000" if topup else None
        gw.ENROLL_STATE.update(busy=False, last=None, discard=False, pause=False)
        gw.ENROLL_STATE.pop("silent_stop", None)
        try:
            with redirect_stdout(io.StringIO()):
                gw.enroll({"phrase": "hey_finn", "persona": "finn", "count": 5, "voice": True, "topup": topup},
                          read16, "default")
            files = [f for _d, _s, fs in os.walk(root) for f in fs]
        finally:
            (gw.post_state, gw.subprocess.Popen, gw.ENROLL_DIR, gw.time.sleep, gw.last_session) = old
            gw.ENROLL_STATE.pop("silent_stop", None)
        return posted, notices, files

    def test_silence_through_the_countdown_stops_and_saves_nothing(self):
        posted, notices, files = self._run(False)
        self.assertEqual(posted.count("recording"), 1)
        self.assertEqual(posted.count("stopping"), 3)
        self.assertEqual(posted[-1], "stopped")
        self.assertEqual(notices, [])
        self.assertEqual(files, [])
        self.assertEqual(gw.ENROLL_STATE["last"]["message"], "Stopped. Nothing was saved.")

    def test_speech_during_the_countdown_retries_the_clip(self):
        posted, _n, files = self._run(True)
        self.assertEqual(posted.count("recording"), 2)  # the same clip again
        first = posted.index("stopping")
        self.assertEqual(posted[first + 1], "ready")  # cancelled after 1 s: back to get ready
        self.assertEqual(posted[-1], "stopped")
        self.assertEqual(files, [])

    def test_topup_line(self):
        posted, _n, _f = self._run(False, topup=True)
        self.assertEqual(posted[-1], "stopped")
        self.assertEqual(gw.ENROLL_STATE["last"]["message"], "Stopped. Nothing new was saved.")


if __name__ == "__main__":
    unittest.main()


class HeardSpeech(unittest.TestCase):
    def test_a_short_sound_at_the_edge_counts(self):
        t = gw.np.arange(1280)
        tone = (gw.np.sin(t * 0.2) * 3000).astype(gw.np.int16)
        pcm = gw.np.concatenate([tone, gw.np.zeros(14080, gw.np.int16)])
        self.assertIsNone(gw.trim_clip(pcm))  # too short for a clip
        self.assertTrue(gw.heard_speech(pcm))  # but it is speech for the countdown
        self.assertFalse(gw.heard_speech(gw.np.zeros(15360, gw.np.int16)))
