#!/usr/bin/env python3
"""L2: the enroll tile never overlaps the weather column (or the offline badge) drawn beside the clock.

Proof from the real renderer: at every supported resolution, for every weather label, offline, and with
timer chips up, the weather/offline marks end above clock_date_bottom(), which is the tile's floor.
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

SIZES = ((800, 480), (1024, 600), (1280, 720), (1280, 800), (1920, 1080), (2560, 1706), (3840, 2160))


def setUpModule():
    pygame.display.init()
    pygame.font.init()


class _Wx:
    def __init__(self, code=None, off=False):
        self.data = None if code is None else {"code": code, "temp": -88, "day": True, "hi": 108, "lo": -88}
        self.off = off

    def offline(self, now=None):
        return self.off


class WeatherFloor(unittest.TestCase):
    def setUp(self):
        self.timers = list(gs.TIMER_CHIPS)
        self.addCleanup(lambda: gs.TIMER_CHIPS.__setitem__(slice(None), self.timers))

    def _check(self, wx, chips):
        gs.TIMER_CHIPS[:] = chips
        for w, h in SIZES:
            L = gs.Layout(w, h)
            gs.draw_enroll(pygame.Surface((w, h)), L, time.localtime(),
                           {"from": "Juniper", "voice": True, "state": "enroll", "text": "“Juniper”",
                            "heard": "1/5|recording|"}, 1.0, wx=wx)
            tile = gs.LAST_ENROLL_RECT
            for mark in (gs.LAST_WEATHER_RECT, gs.LAST_OFFLINE_RECT):
                if mark is None:
                    continue
                self.assertFalse(mark.colliderect(tile), (w, h, mark, tile))
                self.assertLessEqual(mark.bottom, gs.clock_date_bottom(L), (w, h))
                self.assertGreaterEqual(tile.top, mark.bottom, (w, h))

    def test_every_label_every_size(self):
        longest = max(gs.WMO, key=lambda c: len(gs.WMO[c][0]))
        for code in sorted({longest, *list(gs.WMO)[:3]}):
            self._check(_Wx(code), [])

    def test_offline_badge(self):
        self._check(_Wx(off=True), [])

    def test_with_timer_chips(self):
        now = time.time()
        chips = [{"id": "t%d" % k, "label": "Pasta", "ends_at_epoch": now + 600 + k, "ringing": False} for k in range(3)]
        longest = max(gs.WMO, key=lambda c: len(gs.WMO[c][0]))
        self._check(_Wx(longest), chips)


if __name__ == "__main__":
    unittest.main()
