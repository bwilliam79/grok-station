#!/usr/bin/env python3
"""The enroll tile sits fully under the clock and date at every tested station size.

At 1920x1080 the tile was slid up to keep its height and covered the date. It shrinks
instead. clock_date_bottom is the renderer's own clock/date block.
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


class TileBelowClock(unittest.TestCase):
    def setUp(self):
        self._timers = list(gs.TIMER_CHIPS)
        gs.TIMER_CHIPS[:] = []

    def tearDown(self):
        gs.TIMER_CHIPS[:] = self._timers

    def test_tile_sits_below_the_clock_and_date(self):
        now = time.localtime()
        for size in STATION_SIZES:
            with self.subTest(size=size):
                L = gs.Layout(*size)
                surf = pygame.Surface(size)
                gs.draw_idle(surf, L, now, gs._ENROLL_WX, home_band=False)
                drawn = max(gs.LAST_DATE_RECT.bottom, gs.LAST_CLOCK_RECT.bottom)
                tile = gs.enroll_tile_rect(L)
                self.assertGreaterEqual(tile.top, drawn, "tile covers the clock or date")
                self.assertEqual(gs.clock_date_bottom(L), drawn)
                self.assertGreaterEqual(tile.top, gs.clock_date_bottom(L))
                self.assertGreaterEqual(tile.left, 0)
                self.assertGreaterEqual(tile.top, 0)
                self.assertLessEqual(tile.right, size[0])
                self.assertLessEqual(tile.bottom, size[1])
                self.assertGreater(tile.w, 1)
                self.assertGreater(tile.h, 1)


if __name__ == "__main__":
    unittest.main()
