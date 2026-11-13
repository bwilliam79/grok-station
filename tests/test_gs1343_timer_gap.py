"""0.13.43: timer chips must not pull the lower tile into the weather line."""
import os
import sys
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import pygame  # noqa: E402
import cards  # noqa: E402
import grok_station as gs  # noqa: E402
from painted_gaps import painted_gaps  # noqa: E402

pygame.display.init()
pygame.font.init()

SIZES = ((1280, 800), (2560, 1706), (1080, 1920))


class _Wx:
    data = {"temp": 72, "code": 1, "day": True, "hi": 80, "lo": 60}

    def offline(self):
        return False


class TimerGap(unittest.TestCase):
    def setUp(self):
        self.old_updates = gs.UPDATES
        self.old_chips = list(gs.TIMER_CHIPS)
        gs.UPDATES = cards.UpdateFeed()
        gs.UPDATES.put("otto", "a", {"type": "update", "kind": "Release",
                                      "line": "Shipped the nightly build",
                                      "detail": "Ready to try", "time": time.time() - 90})

    def tearDown(self):
        gs.UPDATES = self.old_updates
        gs.TIMER_CHIPS[:] = self.old_chips

    def test_timer_chips_keep_the_minimum_gap(self):
        now = time.strptime("2026-06-15 10:08", "%Y-%m-%d %H:%M")
        gs.TIMER_CHIPS[:] = [{"id": "aaaa01", "label": "pasta",
                               "ends_at_epoch": time.time() + 400, "ringing": False}]
        for size in SIZES:
            layout = gs.Layout(*size)
            surf = pygame.Surface(size)
            gs.draw_idle(surf, layout, now, _Wx(), home_band=True)
            above, _below, _height = painted_gaps(surf)
            panel = gs.bottom_panel_rect(layout)
            self.assertGreaterEqual(above, layout(gs.PANEL_GAP), (size, above, panel))
            self.assertLessEqual(panel.bottom, gs.content_limit(layout), size)
            self.assertGreaterEqual(panel.top, 0, size)

    def test_date_slack_is_measured_once_per_layout(self):
        gs._DATE_SLACK.clear()
        layout = gs.Layout(1280, 800)
        gs.bottom_panel_rect(layout)
        gs.bottom_panel_rect(layout)
        self.assertEqual(len(gs._DATE_SLACK), 1)


if __name__ == "__main__":
    unittest.main()
