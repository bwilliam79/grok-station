#!/usr/bin/env python3
"""Enroll headline box is the font's ascent plus descent, at every tested station size.

Descenders (g, j, p, q, y) were clipped because the box used a fixed cap-height slot.
The box height comes from the font, so it does not change with the string.
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

# README "Screens" / docs/display.md: sizes the layout tests already draw.
STATION_SIZES = (
    (800, 480),
    (1024, 600),
    (1280, 720),
    (1280, 800),
    (1920, 1080),
    (2560, 1706),
    (3840, 2160),
)


def setUpModule():
    pygame.display.init()
    pygame.font.init()


def _headline_font(L, text, width):
    px = gs._fit_px_step("display", text, width, gs.zpx(L, 128), gs.zpx(L, 96), gs.zpx(L, 8))
    return gs.font("display", px)


def _fixed_slot(L):
    """The pre-fix clip, used only when the renderer has not published a box."""
    tile = gs.LAST_ENROLL_RECT
    col_x = tile.x + gs.zpx(L, 720)
    col_w = max(40, tile.right - gs.zpx(L, 80) - col_x)
    return pygame.Rect(col_x, tile.y + gs.zpx(L, 170), col_w, max(1, gs.zpx(L, 131)))


class HeadlineBox(unittest.TestCase):
    def setUp(self):
        self._timers = list(gs.TIMER_CHIPS)
        gs.TIMER_CHIPS[:] = []
        self._copy = gs.enroll_copy

    def tearDown(self):
        gs.TIMER_CHIPS[:] = self._timers
        gs.enroll_copy = self._copy

    def _draw(self, headline, size):
        gs.enroll_copy = lambda phrase, i, n, stage, extra: (
            headline, "Speak after the chime.", gs.LISTENING, "1 of 5", 0.0)
        surf = pygame.Surface(size)
        L = gs.Layout(*size)
        msg = {"from": "Juniper", "voice": True, "state": "enroll", "text": "“Hey Juniper”",
               "heard": "1/5|recording|"}
        gs.draw_enroll(surf, L, time.localtime(), msg, 1.0)
        box = getattr(gs, "LAST_ENROLL_HEADLINE", None)
        if box is None:
            box = _fixed_slot(L)
        return surf, L, pygame.Rect(box)

    def test_descenders_fit_and_height_is_the_font(self):
        for size in STATION_SIZES:
            surf, L, box = self._draw("gjpqy", size)
            hf = _headline_font(L, "gjpqy", box.w)
            line = hf.get_ascent() - hf.get_descent()
            self.assertGreaterEqual(box.h, line, size)
            img = hf.render("gjpqy", True, gs.TEXT)
            br = img.get_bounding_rect()
            self.assertLessEqual(br.y + br.h, box.h, size)
            self.assertLessEqual(br.x + br.w, box.w, size)
            # Lowest solid ink of "gjpqy" (the descenders), not the faint antialias fringe.
            solid = None
            for y in range(br.bottom - 1, -1, -1):
                xs = [x for x in range(max(0, br.left), br.right) if img.get_at((x, y)).a >= 200]
                if xs:
                    solid = (y, xs[len(xs) // 2])
                    break
            self.assertIsNotNone(solid, size)
            y, x = solid
            self.assertLess(y, box.h, size)
            pix = surf.get_at((box.x + x, box.y + y))
            self.assertGreater(int(pix[0]), 160, size)
            _surf2, _L2, plain = self._draw("HHH", size)
            self.assertEqual(plain.h, box.h, size)


if __name__ == "__main__":
    unittest.main()
