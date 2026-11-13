"""0.13.41: the shared bottom panel does not change with the clock."""
import os
import sys
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import grok_station as gs
import pygame
from painted_gaps import painted_gaps

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
        """The lower tile is centered: painted gap above it equals the gap below, within 1 px.

        Height is the 0.13.42 panel (380 px at 1280x800, 867 px at 2560x1706).
        The gap is at least L(48). Measured from painted pixels, not font boxes.
        """
        import cards
        wx = _Wx()
        heights = {(1280, 800): 380, (2560, 1706): 867}
        old = gs.UPDATES
        try:
            for size in SIZES:
                L = gs.Layout(*size)
                now = time.strptime("2026-06-15 10:08", "%Y-%m-%d %H:%M")
                gs.UPDATES = cards.UpdateFeed()
                gs.UPDATES.put("otto", "a", {"type": "update", "kind": "Release",
                                              "line": "Shipped the nightly build",
                                              "detail": "Ready to try", "time": 1_000.0})
                surf = pygame.Surface(size)
                gs.draw_idle(surf, L, now, wx, home_band=True)
                above, below, height = painted_gaps(surf)
                self.assertLessEqual(abs(above - below), 1, (size, above, below))
                self.assertEqual(height, heights[size], size)
                self.assertGreaterEqual(above, L(48), size)
                first = gs.bottom_panel_rect(L)
                for hm in ("1:11", "8:00", "12:59"):
                    self._at(hm)
                    self.assertEqual(gs.bottom_panel_rect(L), first, hm)
        finally:
            gs.UPDATES = old


if __name__ == "__main__":
    unittest.main()
