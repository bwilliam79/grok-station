#!/usr/bin/env python3
"""P5 (Noor, b8b1fe6 re-check, Sev 1): the station's 'Enrollment stopped' screen is plain text, no Close button.

The station has no touch input, so like the other station enroll screens it draws its title and line only.
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


class ErrorScreenNoButton(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.display.init()
        pygame.font.init()

    def test_error_stage_draws_no_button(self):
        self.assertEqual(gs._enroll_actions("error"), ([], False))
        for size in ((1920, 1080), (1280, 720), (800, 480)):
            seen, real = [], gs.font
            gs.font = lambda kind, px: _Font(real(kind, px), seen)
            try:
                gs.draw_enroll(pygame.Surface(size), gs.Layout(*size), time.localtime(),
                               {"from": "Juniper", "voice": True, "state": "enroll", "text": "“Hey Juniper”",
                                "heard": "1/5|error|"}, 1.0)
            finally:
                gs.font = real
            self.assertEqual(gs.LAST_ENROLL_BUTTONS, [], size)
            self.assertNotIn("Close", seen, size)
            self.assertIn("Enrollment stopped", seen, size)
            self.assertIn("Nothing was lost. Try again later.", seen, size)


if __name__ == "__main__":
    unittest.main()
