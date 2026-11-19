"""The idle date shares the clock's left edge and stays on the screen."""
import os
import sys
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import pygame  # noqa: E402
import grok_station as gs  # noqa: E402

pygame.display.init()
pygame.font.init()


def _ink(surf, rect):
    clipped = rect.clamp(surf.get_rect())
    sub = surf.subsurface(clipped).copy()
    sub.set_colorkey(gs.BG)
    box = sub.get_bounding_rect()
    return pygame.Rect(clipped.x + box.x, clipped.y + box.y, box.w, box.h)


class DateAlign(unittest.TestCase):
    def test_the_date_lines_up_with_the_clock_and_stays_on_screen(self):
        samples = ("2026-11-25 01:11", "2026-09-30 01:11")
        for size, margin in (((1280, 720), 64), ((2560, 1440), 128)):
            for stamp in samples:
                now = time.strptime(stamp, "%Y-%m-%d %H:%M")
                surf = pygame.Surface(size)
                gs.draw_idle(surf, gs.Layout(*size), now, gs._ENROLL_WX, home_band=False)
                clock = _ink(surf, gs.LAST_CLOCK_RECT)
                date = _ink(surf, gs.LAST_DATE_RECT)
                self.assertEqual(date.left, clock.left, (size, stamp, date, clock))
                self.assertEqual(clock.left, margin, (size, stamp, clock))
                self.assertGreaterEqual(date.left, 0, (size, stamp, date))
                self.assertLessEqual(date.right, size[0], (size, stamp, date))
                self.assertTrue(surf.get_rect().contains(date), (size, stamp, date))


if __name__ == "__main__":
    unittest.main()
