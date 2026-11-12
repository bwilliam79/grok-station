"""0.13.41 display: one update card, shared panel size, equal margins, wake replaces the card."""
import os
import sys
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import grok_station as gs
import cards
import pygame

NATIVE = (2560, 1706)
PHONE = (1280, 800)


def setUpModule():
    pygame.display.init()
    pygame.font.init()


class _Wx:
    def __init__(self, data=True):
        self.data = {"temp": 72, "code": 1, "day": True, "hi": 80, "lo": 60} if data else None

    def offline(self):
        return False


def _row(bot, line, detail, type_="heads_up"):
    return {"bot": bot, "id": bot[:8], "type": type_, "line": line, "detail": detail,
            "time": time.time() - 30, "kind": "update"}


class OneUpdateCard(unittest.TestCase):
    def setUp(self):
        self.old = gs.UPDATES
        gs.UPDATES = cards.UpdateFeed()
        gs.UPD_ROT.update(step=0, at=None)
        gs.TIMER_CHIPS[:] = []

    def tearDown(self):
        gs.UPDATES = self.old
        gs.TIMER_CHIPS[:] = []

    def test_one_card_no_count_keeps_the_bot_and_fits_the_text(self):
        line = "Harbor market opens early and the north lot is closed for paving today"
        detail = "Tell Wren the stall moved to the east aisle beside the bakery before noon"
        now = time.time()
        gs.UPDATES.put("wren", "note1", {"type": "heads_up", "line": line, "detail": detail, "time": now - 40})
        L = gs.Layout(*NATIVE)
        surf = pygame.Surface(NATIVE)
        gs.draw_idle(surf, L, time.localtime(), _Wx(), home_band=True)
        band = gs.LAST_UPDATE_BAND
        self.assertEqual(len(band["tiles"]), 1)
        self.assertEqual(band["marker"], "")
        self.assertNotIn("Paused", band["marker"])
        self.assertEqual(band["tiles"][0]["bot"], "wren")
        self.assertEqual(gs.LAST_BAND_GEOM["rect"], gs.bottom_panel_rect(L))
        self.assertEqual(gs.LAST_UPDATE_AVATARS[0]["bot"], "wren")
        self.assertEqual(gs.LAST_UPDATE_LINES["text"], line)
        self.assertEqual(gs.LAST_UPDATE_LINES["detail_text"], detail)
        self.assertNotIn("\u2026", gs.LAST_UPDATE_LINES["text"])

    def test_rotation_uses_the_display_setting_and_advances_one_bot(self):
        self.assertEqual(cards.upd_rotate_s(""), 20)
        self.assertEqual(cards.ROTATE_DEFAULT, 20)
        now = time.time()
        gs.UPDATES.put("wren", "a", {"type": "done", "line": "First", "time": now - 20})
        gs.UPDATES.put("juniper", "b", {"type": "done", "line": "Second", "time": now - 10})
        gs.UPD_ROT.update(step=0, at=now)
        first = gs.idle_band_rows(now)[0]["bot"]
        nxt = gs.idle_band_rows(now + 20)[0]["bot"]
        self.assertNotEqual(first, nxt)

    def test_full_text_fits_at_both_station_sizes(self):
        line = "A" * 20 + " market note that still has to fit on the update card in full"
        detail = "Bring the signed form and the spare key to the front desk before closing time"
        row = _row("wren", line[:80], detail[:90])
        for size in (PHONE, NATIVE):
            L = gs.Layout(*size)
            surf = pygame.Surface(size)
            rect = gs.bottom_panel_rect(L)
            gs._paint_update_card(surf, L, rect, row, time.time())
            self.assertEqual(gs.LAST_UPDATE_LINES["text"], row["line"], size)
            self.assertEqual(gs.LAST_UPDATE_LINES["detail_text"], row["detail"], size)


class WakeReplacesTheCard(unittest.TestCase):
    def setUp(self):
        self.old = gs.UPDATES
        gs.UPDATES = cards.UpdateFeed()
        gs.UPD_ROT.update(step=0, at=None)
        gs.TIMER_CHIPS[:] = []
        gs.TILE.clear()

    def tearDown(self):
        gs.UPDATES = self.old
        gs.TILE.clear()

    def test_conversation_replaces_the_card_then_the_card_returns(self):
        now = time.time()
        gs.UPDATES.put("wren", "a", {"type": "done", "line": "One", "time": now})
        gs.UPDATES.put("juniper", "b", {"type": "done", "line": "Two", "time": now - 5})
        L = gs.Layout(*NATIVE)
        surf = pygame.Surface(NATIVE)
        gs.draw_idle(surf, L, time.localtime(), _Wx(False), home_band=True)
        self.assertTrue(gs.LAST_UPDATE_BAND["visible"])
        held = gs.UPD_ROT["step"]
        voice = {"voice": True, "state": "speaking", "from": "Wren", "text": "I am here.", "heard": "hey wren"}
        zone = gs.zone_view(voice, None)
        gs.LAST_REPLY_LINE = None
        gs.draw_zone(surf, L, time.localtime(), _Wx(False), zone=zone)
        self.assertIsNotNone(gs.LAST_ZONE_RECT)
        self.assertEqual(gs.LAST_ZONE_RECT, gs.enroll_tile_rect(L))
        self.assertIsNone(gs.LAST_REPLY_LINE)
        self.assertEqual(gs.UPD_ROT["step"], held)
        self.assertEqual(gs.update_rotation_step(now + 90, paused=True), held)
        gs.draw_idle(surf, L, time.localtime(), _Wx(False), home_band=True)
        self.assertTrue(gs.LAST_UPDATE_BAND["visible"])
        self.assertEqual(gs.LAST_UPDATE_BAND["marker"], "")
        advanced = gs.idle_band_rows(now + 20)
        self.assertEqual(len(advanced), 1)


class SharedPanel(unittest.TestCase):
    def setUp(self):
        gs.TIMER_CHIPS[:] = []

    def test_update_wake_and_training_panels_are_one_rect(self):
        for size in (PHONE, NATIVE, (1920, 1080)):
            L = gs.Layout(*size)
            panel = gs.bottom_panel_rect(L)
            self.assertEqual(gs.zone_rect(L, 0), panel, size)
            self.assertEqual(gs.enroll_tile_rect(L), panel, size)
            surf = pygame.Surface(size)
            gs.LAST_UPDATE_BAND = None
            gs.draw_idle(surf, L, time.localtime(), _Wx(False), home_band=False)
            self.assertEqual(gs.bottom_panel_rect(L), panel, size)


class EqualMargins(unittest.TestCase):
    def setUp(self):
        gs.TIMER_CHIPS[:] = []

    def _ink_top(self, surf):
        """First painted row of the clock and weather, ignoring the status icons at the right edge."""
        bg = surf.get_at((0, 0))
        limit = gs.LAST_STATUS_RECT.left if gs.LAST_STATUS_RECT else surf.get_width()
        w, h = surf.get_size()
        for y in range(h):
            for x in range(0, limit):
                if surf.get_at((x, y)) != bg:
                    return y
        self.fail("the idle screen has no clock or weather ink")

    def test_top_margin_matches_the_bottom_margin(self):
        for size in (PHONE, NATIVE):
            L = gs.Layout(*size)
            surf = pygame.Surface(size)
            now = time.strptime("2026-06-15 10:08", "%Y-%m-%d %H:%M")
            gs.draw_idle(surf, L, now, _Wx(True), home_band=False)
            ink = self._ink_top(surf)
            panel = gs.bottom_panel_rect(L)
            self.assertEqual(ink, size[1] - panel.bottom, size)
            self.assertGreaterEqual(panel.top, gs.clock_date_bottom(L, now), size)
            self.assertLess(gs.LAST_STATUS_RECT.bottom, ink, size)
            self.assertEqual(panel, gs.enroll_tile_rect(L), size)


if __name__ == "__main__":
    unittest.main()
