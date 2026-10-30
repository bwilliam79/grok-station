#!/usr/bin/env python3
"""P8 (Noor, 0a1fc0d re-check): the station's queued, uploaded and custom enroll screens are plain text, no Close.

The station has no touch input and voice close/dismiss doesn't reach the training tile, so nobody can press it.
Each stage keeps its text and its auto-close (queued 30 s, uploaded 10 s, custom 30 s). Same pattern as P5.
"""
import os
import sys
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import grok_station as gs  # noqa: E402
import pygame  # noqa: E402


class _Font:
    def __init__(self, inner, seen):
        self.inner, self.seen = inner, seen

    def render(self, text, *a, **k):
        self.seen.append(text)
        return self.inner.render(text, *a, **k)

    def __getattr__(self, name):
        return getattr(self.inner, name)


CASES = (("5/5|uploaded|", "uploaded", "All 5 clips saved", 10),
         ("0/5|queued|2", "queued", "Training queued", 30),
         ("3/25|Recording… speak now", "custom", "Recording… speak now", 30))


class InfoScreensNoButton(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.display.init()
        pygame.font.init()

    def test_stages_draw_no_button_and_keep_text_and_hold(self):
        for heard, stage, title, hold in CASES:
            self.assertEqual(gs.enroll_parse({"heard": heard})[2], stage, heard)
            self.assertEqual(gs._enroll_actions(stage), ([], False), stage)
            self.assertEqual(gs.state_hold("enroll", False, heard), hold, stage)
            for size in ((1920, 1080), (1280, 720), (800, 480)):
                seen, real = [], gs.font
                gs.font = lambda kind, px: _Font(real(kind, px), seen)
                try:
                    gs.draw_enroll(pygame.Surface(size), gs.Layout(*size), time.localtime(),
                                   {"from": "Juniper", "voice": True, "state": "enroll", "text": "“Hey Juniper”",
                                    "heard": heard}, 1.0)
                finally:
                    gs.font = real
                self.assertEqual(gs.LAST_ENROLL_BUTTONS, [], (stage, size))
                self.assertNotIn("Close", seen, (stage, size))
                self.assertTrue(any(title in s for s in seen), (stage, size, seen))

    def test_other_stages_unchanged(self):
        self.assertEqual([a[0] for a in gs._enroll_actions("prompt")[0]], ["start", "dismiss"])
        self.assertEqual([a[0] for a in gs._enroll_actions("failed")[0]], ["more", "dismiss"])
        self.assertEqual([a[0] for a in gs._enroll_actions("passed")[0]], ["done"])


if __name__ == "__main__":
    unittest.main()
