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
        layout = gs.updates_band_layout(L)
        self.assertEqual(
            [(r.x, r.y, r.w, r.h) for r in layout["rects"]],
            [(128, 820, 1136, 560), (1296, 820, 1136, 560)],
        )
        self.assertEqual((layout["marker"].x, layout["marker"].y, layout["marker"].w), (128, 748, 2304))


if __name__ == "__main__":
    unittest.main()
