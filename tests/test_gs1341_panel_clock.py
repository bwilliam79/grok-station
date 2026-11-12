"""0.13.41: the shared bottom panel does not change with the clock."""
import os
import sys
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import grok_station as gs
import pygame

SIZES = ((1280, 800), (2560, 1706))


def setUpModule():
    pygame.display.init()
    pygame.font.init()


class _Wx:
    data = {"temp": 72, "code": 1, "day": True, "hi": 80, "lo": 60}

    def offline(self):
        return False


def _ink_top(surf):
    """First non-background row left of the status icons. Row compares stay in C."""
    bg = surf.get_at((0, 0))[:3]
    limit = gs.LAST_STATUS_RECT.left if gs.LAST_STATUS_RECT else surf.get_width()
    band = min(surf.get_height(), 420)
    clip = pygame.Surface((limit, band))
    clip.blit(surf, (0, 0), pygame.Rect(0, 0, limit, band))
    raw = pygame.image.tobytes(clip, "RGB")
    stride = limit * 3
    blank = bytes(bg) * limit
    view = memoryview(raw)
    for y in range(band):
        if view[y * stride:(y + 1) * stride] != blank:
            return y
    raise AssertionError("the idle screen has no clock or weather ink")


class PanelClock(unittest.TestCase):
    def setUp(self):
        self.real = time.localtime
        gs.TIMER_CHIPS[:] = []

    def tearDown(self):
        time.localtime = self.real
        gs.TIMER_CHIPS[:] = []

    def _at(self, hm):
        def local(secs=None, hm=hm):
            return time.strptime("2026-06-15 " + hm, "%Y-%m-%d %H:%M")
        time.localtime = local

    def test_rect_is_the_same_for_every_minute(self):
        # The idle clock is 12-hour. 00:00-23:59 are included because the rect
        # must not move even if a 24-hour face is what the station clock shows.
        hours = range(0, 24)
        for size in SIZES:
            L = gs.Layout(*size)
            seen = set()
            for hour in hours:
                for minute in range(60):
                    self._at("%d:%02d" % (hour, minute))
                    rect = gs.bottom_panel_rect(L)
                    seen.add((rect.x, rect.y, rect.w, rect.h))
            self.assertEqual(len(seen), 1, size)
            sample = gs.clock_parts(time.strptime("2026-06-15 00:00", "%Y-%m-%d %H:%M"))
            self.assertEqual(sample[0], "12:00")

    def test_a_recycled_font_id_does_not_reuse_another_size(self):
        import gc
        text = gs.REF_CLOCK_DIGITS
        gs._fonts.clear()
        gs._GLYPH_INK.clear()
        gs._BLOCK_OFFSETS.clear()
        big = gs.glyph_ink_dy("display", 400, text)
        gs._fonts.clear()
        gc.collect()
        for px in range(20, 80):
            gs.font("body", px)
        for px in range(20, 80):
            f = gs.font("body", px)
            got = gs.glyph_ink_dy("body", px, text)
            real = f.render(text, True, gs.TEXT).get_bounding_rect().y
            self.assertEqual(got, real if real else 0, px)
        L = gs.Layout(2560, 1706)
        first = tuple(gs.bottom_panel_rect(L))
        gs._fonts.clear()
        gc.collect()
        for px in range(20, 90):
            gs.font("body", px)
        self.assertEqual(tuple(gs.bottom_panel_rect(L)), first)
        self.assertEqual(gs.glyph_ink_dy("display", 400, text), big)

    def test_visible_top_gap_matches_the_bottom_gap(self):
        wx = _Wx()
        for size in SIZES:
            L = gs.Layout(*size)
            # Every minute of the 12-hour face, plus a short digit and a tall digit
            # from the afternoon, which is what the station actually draws.
            for hour in range(1, 13):
                for minute in range(60):
                    hm = "%d:%02d" % (hour, minute)
                    now = time.strptime("2026-06-15 " + hm, "%Y-%m-%d %H:%M")
                    self._at(hm)
                    surf = pygame.Surface(size)
                    gs.draw_idle(surf, L, now, wx, home_band=False)
                    ink = _ink_top(surf)
                    panel = gs.bottom_panel_rect(L)
                    self.assertEqual(ink, size[1] - panel.bottom, (size, hm))
                    self.assertGreaterEqual(panel.top, gs.clock_date_bottom(L), (size, hm))


if __name__ == "__main__":
    unittest.main()
