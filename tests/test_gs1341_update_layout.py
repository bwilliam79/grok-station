"""0.13.41: the update card uses the wake panel's column inside the same rect."""
import os
import shutil
import sys
import tempfile
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import cards
import grok_station as gs
import pygame

PHONE = (1280, 800)


def setUpModule():
    pygame.display.init()
    pygame.font.init()


class _Wx:
    data = {"temp": 72, "code": 1, "day": True, "hi": 80, "lo": 60}

    def offline(self):
        return False


def _words(n):
    return " ".join("harbor" for _ in range(n))


class UpdateColumn(unittest.TestCase):
    def setUp(self):
        self.old = gs.UPDATES
        gs.UPDATES = cards.UpdateFeed()
        gs.UPD_ROT.update(step=0, at=None)
        gs.TIMER_CHIPS[:] = []
        gs._BAND_PREV = None
        art = os.environ.get("GS_ARTIFACTS")
        if art:
            self.art = art
            self._art_tmp = None
            os.makedirs(self.art, exist_ok=True)
        else:
            self._art_tmp = tempfile.mkdtemp(prefix="gs-update-card-")
            self.art = self._art_tmp

    def tearDown(self):
        gs.UPDATES = self.old
        gs.TIMER_CHIPS[:] = []
        gs._BAND_PREV = None
        if self._art_tmp:
            shutil.rmtree(self._art_tmp, ignore_errors=True)

    def _paint(self, size, line, detail, age_s=300, bot="wren"):
        gs.LAST_UPDATE_AVATARS = []
        gs.LAST_UPDATE_PILLS = []
        L = gs.Layout(*size)
        surf = pygame.Surface(size)
        rect = gs.bottom_panel_rect(L)
        row = {"bot": bot, "id": "1", "type": "heads_up", "line": line, "detail": detail,
               "time": time.time() - age_s, "kind": "update"}
        gs._paint_update_card(surf, L, rect, row, time.time())
        return surf, L, rect

    def _save(self, surf, name):
        pygame.image.save(surf, os.path.join(self.art, name))

    def test_avatar_and_text_match_the_wake_column(self):
        for size in (PHONE, (2560, 1706)):
            surf, L, rect = self._paint(size, "The north lot is closed today.", "East aisle, before noon.")
            outer = gs.panel_avatar_outer(L, rect)
            center = gs.panel_avatar_center(L, rect)
            avatar = gs.LAST_UPDATE_AVATARS[0]
            self.assertEqual(avatar["size"], outer, size)
            self.assertEqual(avatar["center"], center, size)
            self.assertEqual(gs.LAST_UPDATE_LINES["text_x"], gs.panel_text_x(L, rect), size)
            self.assertEqual(gs.LAST_UPDATE_LINES["text_w"], gs.panel_text_width(L, rect), size)
        L = gs.Layout(*PHONE)
        rect = gs.bottom_panel_rect(L)
        self.assertEqual(gs.panel_avatar_outer(L, rect), 272)
        self.assertEqual(gs.panel_avatar_center(L, rect)[0], 230)
        self.assertEqual(gs.panel_text_x(L, rect), 424)

    def test_line_clamps_at_three_and_detail_at_two(self):
        for size in (PHONE, (2560, 1706)):
            L = gs.Layout(*size)
            rect = gs.bottom_panel_rect(L)
            width = gs.panel_text_width(L, rect)
            line = _words(4)
            while len(gs.wrap(gs.font("body", L(gs.WAKE_REPLY_PX)), line, width)) <= gs.UPDATE_LINE_MAX:
                line = line + " " + _words(8)
            detail = _words(4)
            while len(gs.wrap(gs.font("body", L(gs.WAKE_QUOTE_PX)), detail, width)) <= gs.UPDATE_DETAIL_MAX:
                detail = detail + " " + _words(8)
            self._paint(size, line, detail)
            shown = gs.LAST_UPDATE_LINES
            self.assertEqual(len(shown["line"]), 3, size)
            self.assertTrue(shown["line"][-1].endswith("\u2026"), size)
            self.assertNotEqual(shown["text"], line, size)
            self.assertEqual(len(shown["detail"]), 2, size)
            self.assertTrue(shown["detail"][-1].endswith("\u2026"), size)
            self.assertNotEqual(shown["detail_text"], detail, size)

    def test_pill_text_is_20px_and_scales_with_the_card(self):
        phone = gs.Layout(*PHONE)
        wide = gs.Layout(2560, 1706)
        self.assertEqual(phone(30), 20)
        for size, layout in ((PHONE, phone), ((2560, 1706), wide)):
            self._paint(size, "The north lot is closed today.", "East aisle, before noon.")
            self.assertEqual(gs.LAST_UPDATE_PILLS[-1].get("font_px"), layout(30), size)
        self.assertEqual(wide(30) / phone(30), wide(gs.WAKE_NAME_PX) / phone(gs.WAKE_NAME_PX))

    def test_pill_stays_centered_on_the_name(self):
        for size in (PHONE, (2560, 1706)):
            self._paint(size, "The north lot is closed today.", "East aisle, before noon.")
            L = gs.Layout(*size)
            name_font = gs.font("display", L(gs.WAKE_NAME_PX))
            sample = name_font.render("x", True, (255, 255, 255))
            ink = pygame.mask.from_surface(sample, 8).get_bounding_rects()[0]
            mid = ink.y + ink.h // 2
            pill = gs.LAST_UPDATE_PILLS[-1]["rect"]
            self.assertAlmostEqual(pill.centery, gs.LAST_UPDATE_LINES["name_y"] + mid, delta=1, msg=str(size))

    def test_a_long_bot_name_moves_the_pill_onto_the_line_row(self):
        self._paint(PHONE, "The north lot is closed today.", "East aisle.", bot="northwind-community-hub")
        pill = gs.LAST_UPDATE_PILLS[-1]["rect"]
        shown = gs.LAST_UPDATE_LINES
        self.assertEqual(pill.left, shown["text_x"])
        self.assertLessEqual(pill.right, shown["text_x"] + shown["text_w"])
        self.assertGreaterEqual(shown["line_y"], pill.bottom)
        self._paint(PHONE, "The north lot is closed today.", "East aisle.", bot="wren")
        short = gs.LAST_UPDATE_PILLS[-1]["rect"]
        self.assertEqual(pill.size, short.size)

    def test_text_block_is_centered_with_and_without_detail(self):
        heights = {}
        for detail in ("East aisle, before noon.", ""):
            surf, L, rect = self._paint(PHONE, "The north lot is closed today.", detail)
            shown = gs.LAST_UPDATE_LINES
            cy = gs.LAST_UPDATE_AVATARS[0]["center"][1]
            self.assertEqual(shown["block_center"], cy, repr(detail))
            self.assertEqual(cy, rect.centery)
            heights[bool(detail)] = shown["block_h"]
            if detail:
                self.assertEqual(len(shown["detail"]), 1)
            else:
                self.assertEqual(shown["detail"], [])
        self.assertLess(heights[False], heights[True])

    def test_frames(self):
        long_line = _words(80)
        samples = (
            ("update-card-wren.png", "The north lot is closed for paving today.",
             "The stall moved to the east aisle beside the bakery.", 300),
            ("update-card-line-overflow.png", long_line,
             "Bring the signed form before closing.", 300),
            ("update-card-no-detail.png", "Filed the market list.", "", 300),
            ("update-card-long-name.png", "The north lot is closed for paving today.",
             "The stall moved to the east aisle beside the bakery.", 300),
        )
        now_s = time.mktime(time.strptime("2026-06-15 10:08", "%Y-%m-%d %H:%M"))
        wx = _Wx()
        for name, line, detail, age in samples:
            bot = "northwind-community-hub" if name == "update-card-long-name.png" else "wren"
            gs.UPDATES = cards.UpdateFeed()
            gs._BAND_PREV = None
            gs.UPD_ROT.update(step=0, at=None)
            gs.UPDATES.put(bot, "note", {"type": "heads_up", "line": line, "detail": detail,
                                         "time": time.time() - age})
            L = gs.Layout(*PHONE)
            surf = pygame.Surface(PHONE)
            gs.draw_idle(surf, L, time.localtime(now_s), wx, home_band=True)
            self.assertEqual(gs.LAST_UPDATE_AVATARS[0]["bot"], bot)
            self.assertEqual(gs.LAST_UPDATE_AVATARS[0]["size"], gs.panel_avatar_outer(L, gs.bottom_panel_rect(L)))
            self.assertEqual(gs.LAST_UPDATE_LINES["text_x"], gs.panel_text_x(L, gs.bottom_panel_rect(L)))
            self._save(surf, name)
            self.assertTrue(os.path.isfile(os.path.join(self.art, name)))
            if not os.environ.get("GS_ARTIFACTS"):
                self.assertNotIn("/opt/cursor/artifacts", os.path.abspath(self.art))


if __name__ == "__main__":
    unittest.main()
