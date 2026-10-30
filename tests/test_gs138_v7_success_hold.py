#!/usr/bin/env python3
"""A finished enroll shows "Saved. Checking the recordings." for 4 s, then the tile closes.

It does not stay on "5 of 5 saved", and that screen draws no button. Earlier saved
clips keep the enroll hold. Passed, uploaded, and error stay at 10 s.
"""
import os
import sys
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import grok_station as gs
import pygame

SUCCESS = "Saved. Checking the recordings."
HEARD = "5/5|saved|"


def setUpModule():
    pygame.display.init()
    pygame.font.init()


class _Font:
    def __init__(self, inner, seen):
        self.inner = inner
        self.seen = seen

    def render(self, text, antialias, color, *args, **kwargs):
        self.seen.append(text)
        return self.inner.render(text, antialias, color, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class SuccessHold(unittest.TestCase):
    def setUp(self):
        self._timers = list(gs.TIMER_CHIPS)
        gs.TIMER_CHIPS[:] = []

    def tearDown(self):
        gs.TIMER_CHIPS[:] = self._timers

    def test_success_holds_four_seconds_then_the_tile_is_gone(self):
        self.assertEqual(gs.VOICE_HOLDS["enroll"], 30)
        now = 1000.0
        hold = gs.state_hold("enroll", False, HEARD)
        until = now + hold
        self.assertEqual(hold, 4)
        self.assertLess(now + 4 - 0.001, until)       # still up just before 4 s
        self.assertGreaterEqual(now + 4, until)       # the hold is over at 4 s
        # A clip that is not the last one is not this screen.
        self.assertEqual(gs.state_hold("enroll", False, "4/5|saved|"), 30)
        self.assertEqual(gs.state_hold("enroll", False, "4/5|saved|next"), 30)
        self.assertEqual(gs.state_hold("enroll", False, "0/5|passed|"), 10)
        head, sub, _color, status, _bar = gs.enroll_copy("Juniper", 5, 5, "saved", "")
        self.assertEqual((head, sub, status), (SUCCESS, "", "5 of 5"))
        self.assertEqual(gs.enroll_copy("Juniper", 4, 5, "saved", "next")[0], "4 of 5 saved")
        seen = []
        real = gs.font
        gs.font = lambda kind, px: _Font(real(kind, px), seen)
        try:
            surf = pygame.Surface((1920, 1080))
            L = gs.Layout(1920, 1080)
            msg = {"from": "Juniper", "voice": True, "state": "enroll", "text": "“Juniper”",
                   "heard": HEARD}
            gs.draw_enroll(surf, L, time.localtime(), msg, 1.0)
            self.assertIsNotNone(gs.LAST_ENROLL_RECT)
            self.assertIn(SUCCESS, seen)
            self.assertNotIn("5 of 5 saved", seen)
            self.assertEqual(gs.LAST_ENROLL_BUTTONS, [])
            for label in ("Cancel", "Done", "Close", "Not now", "Record 5 more"):
                self.assertNotIn(label, seen)
            # No card on this message. When the hold elapses the loop paints idle and the tile is gone.
            self.assertFalse(msg.get("card"))
            self.assertGreaterEqual(now + hold, until)
            gs.draw_idle(surf, L, time.localtime(), gs._ENROLL_WX, home_band=False)
            self.assertIsNone(gs.LAST_ENROLL_RECT)
            self.assertEqual(gs.LAST_ENROLL_BUTTONS, [])
        finally:
            gs.font = real


if __name__ == "__main__":
    unittest.main()
