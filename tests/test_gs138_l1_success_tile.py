#!/usr/bin/env python3
"""L1 (gs-voice-train rev1.3/1.4 copy deck): the success tile shows only "Saved. Checking the recordings."
for 4 s, then closes. No "To stop, stay quiet." hint and no stale "5 of 5" progress line."""
import os
import sys
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import grok_station as gs  # noqa: E402
import pygame  # noqa: E402

SUCCESS = "Saved. Checking the recordings."


def setUpModule():
    pygame.display.init()
    pygame.font.init()


class _Font:
    def __init__(self, inner, seen):
        self.inner, self.seen = inner, seen

    def render(self, text, *a, **k):
        self.seen.append(text)
        return self.inner.render(text, *a, **k)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class SuccessTile(unittest.TestCase):
    def _drawn(self, heard, w=1920, h=1080):
        seen, real = [], gs.font
        gs.font = lambda kind, px: _Font(real(kind, px), seen)
        timers = list(gs.TIMER_CHIPS)
        gs.TIMER_CHIPS[:] = []
        try:
            gs.draw_enroll(pygame.Surface((w, h)), gs.Layout(w, h), time.localtime(),
                           {"from": "Juniper", "voice": True, "state": "enroll", "text": "“Juniper”", "heard": heard}, 1.0)
        finally:
            gs.font = real
            gs.TIMER_CHIPS[:] = timers
        return [s for s in seen if s.strip()]

    def test_copy_has_no_status(self):
        head, sub, _c, status, _b = gs.enroll_copy("Juniper", 5, 5, "saved", "")
        self.assertEqual((head, sub, status), (SUCCESS, "", None))

    def test_success_tile_draws_only_the_line(self):
        for w, h in ((800, 480), (1920, 1080)):
            seen = self._drawn("5/5|saved|", w, h)
            self.assertIn(SUCCESS, seen)
            self.assertNotIn(gs.QUIET_HINT, seen)
            self.assertFalse([s for s in seen if "of 5" in s], seen)

    def test_midway_saved_still_shows_hint(self):
        seen = self._drawn("4/5|saved|next")
        self.assertIn(gs.QUIET_HINT, seen)

    def test_holds_4_s(self):
        self.assertEqual(gs.state_hold("enroll", False, "5/5|saved|"), 4)


if __name__ == "__main__":
    unittest.main()
