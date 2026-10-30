#!/usr/bin/env python3
"""Station enroll tile: no Cancel button. A muted hint sits under the progress.

The station has no touch input. Staying quiet is the way out, so the tile says
"To stop, stay quiet." in MUTED on the tile background (at least 4.5:1), and the
spoken intro and Train more line say the same. The admin dialog is not this tile.
"""
import os
import sys
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay"))
import grok_station as gs
import pygame
import train_flow

HINT = "To stop, stay quiet for 3 seconds."  # 0.13.9 gs-voice-train rev1.6
INTRO = "Okay. I'll record you 5 times. Say Juniper after each chime. To stop, stay quiet."
MORE = "Okay. I'll add 5 more. Say Juniper after each chime. To stop, stay quiet."
# Stages that used to draw ("stop", "Cancel"). The hint replaces that button.
CANCEL_STAGES = ("ready", "count", "recording", "quiet", "loud", "saved")


def setUpModule():
    pygame.display.init()
    pygame.font.init()


def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def contrast(a, b):
    """WCAG contrast of two RGB triples."""
    def lum(col):
        return 0.2126 * _lin(col[0]) + 0.7152 * _lin(col[1]) + 0.0722 * _lin(col[2])
    x, y = sorted((lum(a), lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


def _msg(stage, i=1, n=5, extra=""):
    return {"from": "Juniper", "voice": True, "state": "enroll", "text": "“Hey Juniper”",
            "heard": f"{i}/{n}|{stage}|{extra}"}


class _Font:
    def __init__(self, inner, seen):
        self.inner = inner
        self.seen = seen

    def render(self, text, antialias, color, *args, **kwargs):
        self.seen.append((text, tuple(color)[:3]))
        return self.inner.render(text, antialias, color, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class StationCancel(unittest.TestCase):
    def setUp(self):
        self._timers = list(gs.TIMER_CHIPS)
        gs.TIMER_CHIPS[:] = []

    def tearDown(self):
        gs.TIMER_CHIPS[:] = self._timers

    def _draw(self, stage, size=(1920, 1080), **kw):
        seen = []
        real = gs.font
        gs.font = lambda kind, px: _Font(real(kind, px), seen)
        try:
            surf = pygame.Surface(size)
            L = gs.Layout(*size)
            gs.draw_enroll(surf, L, time.localtime(), _msg(stage, **kw), 1.0)
        finally:
            gs.font = real
        return surf, seen

    def test_no_cancel_and_muted_hint_under_progress(self):
        muted = tuple(gs.MUTED)[:3]
        tile_bg = tuple(gs.SURFACE)[:3]
        ratio = contrast(muted, tile_bg)
        self.assertGreaterEqual(ratio, 4.5, "MUTED on the tile background")
        for stage in CANCEL_STAGES:
            extra = "3" if stage == "count" else ("next" if stage == "saved" else "")
            i = 2 if stage == "saved" else 1
            surf, seen = self._draw(stage, i=i, extra=extra)
            ids = [b[0] for b in gs.LAST_ENROLL_BUTTONS]
            self.assertNotIn("stop", ids, stage)
            self.assertNotIn("Cancel", [text for text, _c in seen], stage)
            hints = [c for text, c in seen if text == HINT]
            self.assertEqual(hints, [muted], stage)
            hint = gs.LAST_ENROLL_HINT
            prog = gs.LAST_ENROLL_PROGRESS
            self.assertIsNotNone(hint, stage)
            self.assertIsNotNone(prog, stage)
            tile = gs.LAST_ENROLL_RECT
            self.assertGreaterEqual(hint.top, prog.bottom - 1, stage)
            self.assertTrue(tile.contains(hint), stage)
            # The hint sits on the tile fill, not on a button.
            bg = tuple(surf.get_at((tile.right - 36, hint.centery))[:3])
            self.assertEqual(bg, tile_bg, stage)
            self.assertGreaterEqual(contrast(hints[0], bg), 4.5, stage)

    def test_prompt_draws_no_buttons_and_admin_copy_is_untouched(self):
        """0.13.9 (rev1.5): prompt has no Start / Not now either. Stop and discard stays admin-only."""
        _surf, seen = self._draw("prompt")
        self.assertEqual(gs.LAST_ENROLL_BUTTONS, [])
        self.assertNotIn(HINT, [text for text, _c in seen])
        admin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "web", "app.js")
        with open(admin, encoding="utf-8") as fh:
            self.assertIn("Stop and discard", fh.read())

    def test_spoken_intro_and_train_more(self):
        hit = {"slug": "juniper", "name": "Juniper", "phrase": "hey_juniper", "spoken": "Juniper"}
        plan = train_flow.voice_train_plan(hit, has_model=False)
        more = train_flow.voice_train_plan(hit, has_model=True)
        self.assertEqual(plan["say"], INTRO)
        self.assertEqual(more["say"], MORE)
        # rev1.6: silence through the countdown stops and says nothing was saved.
        second = train_flow.silent_clip(2, "Juniper", voice=True)
        self.assertEqual(second["action"], "stop")
        self.assertEqual(second["say"], "Stopped. Nothing was saved.")


if __name__ == "__main__":
    unittest.main()
