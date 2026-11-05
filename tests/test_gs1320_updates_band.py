"""0.13.20 updates band (Noor SPEC rev6): shared rotation, Needs you in slot 1, avatar fallback."""
import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import cards  # noqa: E402
import grok_station as gs  # noqa: E402
import pygame  # noqa: E402


def setUpModule():
    pygame.display.init()
    pygame.font.init()


class _Wx:
    def __init__(self, code=0):
        self.data = {"code": code, "temp": 72, "day": True, "hi": 80, "lo": 60}

    def offline(self, now=None):
        return False


class RotationClock(unittest.TestCase):
    def test_default_is_20_and_the_constant_matches(self):
        self.assertEqual(cards.UPD_ROTATE_S, 20)
        self.assertEqual(cards.upd_rotate_s(""), 20)
        self.assertEqual(cards.upd_rotate_s(env={}), 20)
        self.assertEqual(cards.rotate_s(None), 20)

    def test_interval_is_read_from_config(self):
        self.assertEqual(cards.upd_rotate_s(env={"GROK_STATION_CARD_ROTATE": "45"}), 45)
        self.assertEqual(cards.upd_rotate_s("60"), 60)
        old = os.environ.get("GROK_STATION_CARD_ROTATE")
        os.environ["GROK_STATION_CARD_ROTATE"] = "30"
        gs.UPD_ROT.update(step=0, at=0)
        try:
            self.assertEqual(gs.update_rotation_step(29), 0)
            self.assertEqual(gs.update_rotation_step(30), 1)
            self.assertEqual(cards.update_next_step(1, 29, paused=False), 1)
            self.assertEqual(cards.update_next_step(1, 30, paused=False), 2)
        finally:
            gs.UPD_ROT.update(step=0, at=None)
            if old is None:
                os.environ.pop("GROK_STATION_CARD_ROTATE", None)
            else:
                os.environ["GROK_STATION_CARD_ROTATE"] = old

    def test_saved_values_snap_to_the_nearest_choice_ties_up_logged_once(self):
        cards._ROTATE_SNAP_LOGGED.discard(25)
        cards._ROTATE_SNAP_LOGGED.discard(12)
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertEqual(cards.upd_rotate_s("25"), 30)  # 5 from 20 and 5 from 30
            self.assertEqual(cards.upd_rotate_s("25"), 30)
            self.assertEqual(cards.upd_rotate_s("12"), 10)
            self.assertEqual(cards.upd_rotate_s("13"), 15)
            self.assertEqual(cards.upd_rotate_s("nope"), 20)
        log = buf.getvalue()
        self.assertEqual(log.count("25s snapped to 30s"), 1)
        self.assertIn("12s snapped to 10s", log)
        self.assertNotIn("nope", log)


class NeedsYouSlot(unittest.TestCase):
    def test_several_needs_you_rotate_in_slot_1_only(self):
        needs = [{"id": "n1"}, {"id": "n2"}, {"id": "n3"}]
        others = [{"id": "h1", "kind": "home"}, {"id": "u1"}, {"id": "u2"}]
        self.assertEqual([t["id"] for t in cards.update_slots(needs, others, 0)], ["n1", "h1"])
        self.assertEqual([t["id"] for t in cards.update_slots(needs, others, 1)], ["n2", "u1"])
        self.assertEqual([t["id"] for t in cards.update_slots(needs, others, 2)], ["n3", "u2"])
        self.assertEqual(cards.update_marker(needs, others, 1), "Needs you 2 of 3 \u00b7 others 2 of 3")


class AvatarAndBounds(unittest.TestCase):
    def setUp(self):
        self.personas = gs.PERSONAS_FILE
        self.dirs = list(gs.PORTRAIT_DIRS)
        self.cache = dict(gs._personas)
        self.deck, self.updates = gs.DECK, gs.UPDATES
        self.timers = list(gs.TIMER_CHIPS)
        gs.DECK = cards.Deck(rotate_s=20, ttl_max=86400)
        gs.UPDATES = cards.UpdateFeed()
        gs.UPD_ROT.update(step=0, at=None)
        gs.TIMER_CHIPS[:] = []

    def tearDown(self):
        gs.PERSONAS_FILE = self.personas
        gs.PORTRAIT_DIRS[:] = self.dirs
        gs._personas.clear()
        gs._personas.update(self.cache)
        gs._photo_cache.clear()
        gs.DECK, gs.UPDATES = self.deck, self.updates
        gs.TIMER_CHIPS[:] = self.timers

    def _tile(self, bot, **row):
        body = {"bot": bot, "type": "needs_you", "line": "Ready to approve the next step?", "time": 100.0}
        body.update(row)
        surf = pygame.Surface((1136, 560))
        surf.fill(gs.BG)
        gs.LAST_UPDATE_AVATARS = []
        gs._paint_update_tile(surf, gs.Layout(2560, 1706), surf.get_rect(), body, 100.0)
        return surf, gs.LAST_UPDATE_AVATARS[0]

    def test_missing_portrait_draws_the_initial(self):
        gs._personas.update(mtime=None, names={})
        surf, avatar = self._tile("aster")
        self.assertTrue(avatar["fallback"])
        self.assertEqual(avatar["initial"], "A")
        rect = avatar["rect"]
        self.assertEqual((rect.x, rect.y, rect.w, rect.h), (40, 40, 160, 160))
        center = surf.get_at((rect.centerx, rect.centery))
        self.assertEqual((center.r, center.g, center.b), (gs.SURFACE2.r, gs.SURFACE2.g, gs.SURFACE2.b))
        ink = gs.TEXT
        found = False
        for y in range(rect.top + 40, rect.bottom - 40, 2):
            for x in range(rect.left + 40, rect.right - 40, 2):
                px = surf.get_at((x, y))
                if abs(px.r - ink.r) < 8 and abs(px.g - ink.g) < 8 and abs(px.b - ink.b) < 8:
                    found = True
                    break
            if found:
                break
        self.assertTrue(found, "initial ink missing from the avatar circle")

    def test_portrait_file_is_cropped_into_the_circle(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        img = pygame.Surface((48, 48))
        img.fill((210, 30, 30))
        path = os.path.join(tmp, "aster.png")
        pygame.image.save(img, path)
        with open(os.path.join(tmp, "personas.json"), "w") as fh:
            fh.write('{"personas":[{"slug":"aster","name":"Aster"}]}')
        gs.PORTRAIT_DIRS[:] = [tmp]
        gs.PERSONAS_FILE = os.path.join(tmp, "personas.json")
        gs._personas.update(mtime=None, names={})
        surf, avatar = self._tile("Aster")
        self.assertFalse(avatar["fallback"])
        rect = avatar["rect"]
        center = surf.get_at((rect.centerx, rect.centery))
        self.assertGreater(center.r, 180)
        self.assertLess(center.g, 80)

    def test_weather_and_timer_leave_y820_to_y1380_clear(self):
        L = gs.Layout(2560, 1706)
        now = time.strptime("2026-10-28 19:42", "%Y-%m-%d %H:%M")
        epoch = 1_800_000_000.0
        gs.TIMER_CHIPS[:] = [
            {"id": "pasta01", "label": "Pasta", "ends_at_epoch": epoch + 600, "ringing": False, "duration": 600}
        ]
        surf = pygame.Surface((2560, 1706))
        gs.draw_idle(surf, L, now, _Wx(), colon_on=True, home_band=False)
        self.assertLessEqual(gs.LAST_WEATHER_RECT.bottom, 820)
        self.assertLess(gs.LAST_CLOCK_RECT.bottom, 820)
        self.assertLess(gs.LAST_DATE_RECT.bottom, 820)
        self.assertLess(gs.LAST_STATUS_RECT.bottom, 100)
        self.assertGreaterEqual(gs.LAST_TIMER_RECT.top, 1380)
        self.assertGreaterEqual(gs.content_limit(L), 1380)
        self.assertEqual(gs.LAST_DATE_RECT.bottom, 702)
        layout = gs.updates_band_layout(L)
        self.assertEqual(layout["rects"][0].y, 806)
        self.assertEqual(layout["rects"][0].h, 560)
        self.assertEqual(layout["rects"][0].bottom, 1366)


class BandClearance(unittest.TestCase):
    def setUp(self):
        self.timers = list(gs.TIMER_CHIPS)
        gs.TIMER_CHIPS[:] = []

    def tearDown(self):
        gs.TIMER_CHIPS[:] = self.timers

    def _layout(self, date_bottom):
        return gs.updates_band_layout(gs.Layout(2560, 1706), date_bottom=date_bottom, weather_bottom=0)

    def test_date_667_uses_the_104_px_gap(self):
        layout = self._layout(667)
        self.assertEqual(gs.BAND_AFTER_DATE + gs.BAND_MARKER_H + gs.BAND_BEFORE_BAND, 104)
        self.assertEqual(layout["rects"][0].y, 771)
        self.assertEqual(layout["rects"][0].h, 560)
        self.assertEqual(layout["rects"][0].bottom, 1331)
        self.assertEqual(layout["marker"].top - 667, 40)
        self.assertEqual(layout["scale"], 1.0)

    def test_date_702_is_full_height_under_the_limit(self):
        layout = self._layout(702)
        tile = layout["rects"][0]
        self.assertEqual((tile.y, tile.bottom, tile.h), (806, 1366, 560))
        self.assertEqual(layout["marker"].top - 702, 40)
        self.assertEqual(layout["scale"], 1.0)

    def test_shrinks_between_520_and_540_and_scales_type(self):
        # 742 + 104 = 846; 1376 - 846 = 530, which is below 540 so type scales.
        layout = self._layout(742)
        tile = layout["rects"][0]
        self.assertEqual((tile.y, tile.h, tile.bottom), (846, 530, 1376))
        self.assertGreaterEqual(tile.h, 520)
        self.assertLess(tile.h, 540)
        self.assertAlmostEqual(layout["scale"], 530 / 560.0)

    def test_scaled_range_starts_below_540(self):
        # 722 + 104 = 826; 1376 - 826 = 550, still at or above 540, so type stays full size.
        full = self._layout(722)
        self.assertEqual(full["rects"][0].h, 550)
        self.assertEqual(full["rects"][0].bottom, 1376)
        self.assertEqual(full["scale"], 1.0)
        scaled = self._layout(743)  # top 847, height 529
        self.assertLess(scaled["rects"][0].h, 540)
        self.assertGreaterEqual(scaled["rects"][0].h, 520)
        self.assertAlmostEqual(scaled["scale"], scaled["rects"][0].h / 560.0)

    def test_hides_when_520_does_not_fit(self):
        # 760 + 104 = 864; 1376 - 864 = 512, under the 520 minimum.
        self.assertIsNone(self._layout(760))


class NightReset(unittest.TestCase):
    def test_night_end_restarts_at_position_1(self):
        gs.UPD_ROT.update(step=4, at=1, night=False)
        self.assertFalse(gs.updates_night_edge(True))
        self.assertEqual(gs.UPD_ROT["step"], 4)
        self.assertTrue(gs.updates_night_edge(False))
        self.assertEqual(gs.UPD_ROT["step"], 0)
        self.assertIsNone(gs.UPD_ROT["at"])
        needs = [{"id": "n1"}, {"id": "n2"}]
        others = [{"id": "o1"}, {"id": "o2"}, {"id": "o3"}]
        shown = [t["id"] for t in cards.update_slots(needs, others, gs.UPD_ROT["step"])]
        self.assertEqual(shown, ["n1", "o1"])


class BandFade(unittest.TestCase):
    def setUp(self):
        self.motion = os.environ.get("GROK_STATION_REDUCE_MOTION")
        os.environ.pop("GROK_STATION_REDUCE_MOTION", None)
        self.dates = gs.LAST_DATE_RECT, gs.LAST_WEATHER_RECT, gs.LAST_OFFLINE_RECT
        gs.LAST_DATE_RECT = gs.LAST_WEATHER_RECT = gs.LAST_OFFLINE_RECT = None
        self.timers = list(gs.TIMER_CHIPS)
        gs.TIMER_CHIPS[:] = []
        gs._BAND_PREV = None

    def tearDown(self):
        gs.LAST_DATE_RECT, gs.LAST_WEATHER_RECT, gs.LAST_OFFLINE_RECT = self.dates
        gs.TIMER_CHIPS[:] = self.timers
        gs._BAND_PREV = None
        if self.motion is None:
            os.environ.pop("GROK_STATION_REDUCE_MOTION", None)
        else:
            os.environ["GROK_STATION_REDUCE_MOTION"] = self.motion

    def _rows(self, uid):
        return [{"kind": "update", "id": uid, "bot": "aster", "type": "done", "line": uid, "time": 1.0}]

    def test_eight_steps_in_place_and_instant_when_reduced(self):
        self.assertEqual(len(gs.band_fade_alphas(reduced=False)), 8)
        self.assertEqual(gs.band_fade_alphas(reduced=False)[-1], 255)
        self.assertLess(gs.band_fade_alphas(reduced=False)[0], 128)
        self.assertEqual(gs.band_fade_alphas(reduced=True), (255,))
        old, new = pygame.Surface((4, 4)), pygame.Surface((4, 4))
        old.fill((0, 0, 0))
        new.fill((255, 255, 255))
        mid = gs.blend_band_frame(old, new, 128)
        pix = mid.get_at((0, 0))
        self.assertGreater(pix.r, 40)
        self.assertLess(pix.r, 220)
        L = gs.Layout(2560, 1706)
        surf = pygame.Surface((2560, 1706))
        surf.fill(gs.BG)
        gs.LAST_UPDATE_BAND = {"marker": "1 of 2"}
        gs._draw_updates_band(surf, L, self._rows("a"), 10.0)
        self.assertEqual(gs.LAST_BAND_FADE["steps"], 1)
        gs.LAST_UPDATE_BAND = {"marker": "2 of 2"}
        gs._draw_updates_band(surf, L, self._rows("b"), 10.0)
        self.assertEqual(gs.LAST_BAND_FADE["steps"], 8)
        self.assertEqual(gs.LAST_BAND_FADE["alphas"][-1], 255)
        os.environ["GROK_STATION_REDUCE_MOTION"] = "1"
        gs.LAST_UPDATE_BAND = {"marker": "1 of 2"}
        gs._draw_updates_band(surf, L, self._rows("c"), 10.0)
        self.assertEqual(gs.LAST_BAND_FADE["alphas"], [255])


class NeedsYouPill(unittest.TestCase):
    def test_bang_is_its_own_icon(self):
        surf = pygame.Surface((1136, 560))
        surf.fill(gs.BG)
        gs.LAST_UPDATE_PILLS = []
        gs._paint_update_tile(surf, gs.Layout(2560, 1706), surf.get_rect(),
                              {"bot": "aster", "type": "needs_you", "line": "Ready?", "time": 100.0}, 100.0)
        self.assertEqual(gs.LAST_UPDATE_PILLS[0]["word"], "NEEDS YOU")
        self.assertTrue(gs.LAST_UPDATE_PILLS[0]["icon"])
        self.assertNotIn("!", gs.LAST_UPDATE_PILLS[0]["word"])


if __name__ == "__main__":
    unittest.main()
