"""0.13.42: a clear gap between the time block and the tile panel, at both sizes."""
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame  # noqa: E402
import grok_station as gs  # noqa: E402

pygame.display.init()
pygame.font.init()


class _Wx:
    data = {"temp": 72, "code": 1, "day": True, "hi": 80, "lo": 60}

    def offline(self):
        return False


def _gap(size):
    layout = gs.Layout(*size)
    clock_top = gs.idle_clock_top(layout)
    _top, bot = gs.idle_block_offsets(layout)
    block_bottom = clock_top + bot
    panel = gs.bottom_panel_rect(layout)
    return panel.top - block_bottom, layout, panel, clock_top


class PanelGap(unittest.TestCase):
    def test_minimum_gap_at_both_resolutions(self):
        for size in ((1280, 800), (2560, 1706)):
            gap, layout, panel, clock_top = _gap(size)
            self.assertGreaterEqual(gap, layout(48), size)
            if size == (1280, 800):
                self.assertGreaterEqual(gap, 32)
            self.assertGreaterEqual(panel.top, gs.clock_date_bottom(layout, time.strptime("2026-06-15 10:08", "%Y-%m-%d %H:%M")))
            # The 0.13.41 rule: the margin under the panel equals the ink above the block.
            self.assertEqual(size[1] - panel.bottom, clock_top + gs.idle_block_offsets(layout)[0])

    def test_frames_show_the_gap(self):
        """1280x800 idle with an update card, and the same size with a content tile.

        GS_ARTIFACTS keeps the PNGs. Otherwise they go to a temp dir this test deletes.
        """
        import cards
        now = time.strptime("2026-06-15 10:08", "%Y-%m-%d %H:%M")
        layout = gs.Layout(1280, 800)
        gs.UPDATES = cards.UpdateFeed()
        gs.UPDATES.put("otto", "a", {"type": "update", "kind": "Release", "line": "Shipped the nightly build",
                                      "detail": "Ready to try on the station", "time": time.time() - 90})
        sports = {"v": 1, "type": "sports", "league": "City league",
                  "home": {"name": "Harbor City", "abbr": "HC"},
                  "away": {"name": "Ridgeview", "abbr": "RV"},
                  "home_score": 3, "away_score": 2, "status": "live", "period": "2nd",
                  "schedule": [{"label": "Harbor City at Ridgeview", "when": "Sat 7pm"}]}
        keep = os.environ.get("GS_ARTIFACTS", "")
        tmp = None
        try:
            dest = keep if keep else tempfile.mkdtemp(prefix="gs1342-frames-")
            if not keep:
                tmp = dest
            os.makedirs(dest, exist_ok=True)
            idle = pygame.Surface((1280, 800))
            gs.draw_idle(idle, layout, now, _Wx(), home_band=True)
            tile = pygame.Surface((1280, 800))
            gs.draw_zone(tile, layout, now, _Wx(), zone={"tile": sports})
            idle_path = os.path.join(dest, "idle-update-1280x800.png")
            tile_path = os.path.join(dest, "content-tile-1280x800.png")
            pygame.image.save(idle, idle_path)
            pygame.image.save(tile, tile_path)
            self.assertGreater(os.path.getsize(idle_path), 1000)
            self.assertGreater(os.path.getsize(tile_path), 1000)
            panel = gs.bottom_panel_rect(layout)
            clock_top = gs.idle_clock_top(layout)
            block_bottom = clock_top + gs.idle_block_offsets(layout)[1]
            self.assertGreaterEqual(panel.top - block_bottom, layout(48))
        finally:
            gs.UPDATES = None
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)


def _ink_rect(surf, rect):
    """Painted pixels inside rect. The font box includes empty rows; this does not."""
    sub = surf.subsurface(rect).copy()
    sub.set_colorkey(gs.BG)
    box = sub.get_bounding_rect()
    return pygame.Rect(rect.x + box.x, rect.y + box.y, box.w, box.h)


class ClockWeatherHeight(unittest.TestCase):
    def test_clock_date_ink_matches_weather_ink(self):
        """Painted ink of the clock digits and the date matches the weather ink.

        Top, bottom, and height, each within 2 px. 1280x800 and 2560x1706.
        The comparison is get_bounding_rect of the painted pixels, not the font box.
        """
        now = time.strptime("2026-06-15 10:08", "%Y-%m-%d %H:%M")
        for size in ((1280, 800), (2560, 1706)):
            surf = pygame.Surface(size)
            gs.draw_idle(surf, gs.Layout(*size), now, _Wx(), home_band=False)
            clock = _ink_rect(surf, gs.LAST_CLOCK_RECT)
            date = _ink_rect(surf, gs.LAST_DATE_RECT)
            block = clock.union(date)
            weather = _ink_rect(surf, gs.LAST_WEATHER_RECT)
            self.assertLessEqual(abs(block.top - weather.top), 2, (size, "top", block, weather))
            self.assertLessEqual(abs(block.bottom - weather.bottom), 2, (size, "bottom", block, weather))
            self.assertLessEqual(abs(block.h - weather.h), 2, (size, "height", block, weather))
            self.assertLessEqual(gs.IDLE_CLOCK_PX, 238)
            self.assertLessEqual(gs.IDLE_DATE_PX, 60)


if __name__ == "__main__":
    unittest.main()
