"""0.12.6: a tile stays up in a session with a corner badge and the bot's name; stale tiles clear on the response."""
import os, sys, time, unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import grok_station as gs
import pygame

CARD = {"v": 1, "type": "sports", "league": "NCAAM", "home": {"name": "Arkansas", "abbr": "ARK"},
        "away": {"name": "Gonzaga", "abbr": "GONZ"}, "home_score": 70, "away_score": 68, "status": "final", "period": "F", "schedule": []}


def setUpModule():
    pygame.display.init()
    pygame.font.init()


class Screen(unittest.TestCase):
    def test_session_keeps_the_tile(self):
        msg = {"from": "Rhea", "text": "…", "state": "thinking", "voice": True}
        for busy in (True, False):
            self.assertEqual(gs.screen_for(None, busy, msg, False, CARD, False, CARD), "feed")

    def _draw(self, msg):
        calls = []
        real_badge, real_line = gs.draw_tile_badge, gs._line
        def line(surf, kind, px, text, color, width):
            calls.append(text)
            return real_line(surf, kind, px, text, color, width)
        gs._line = line
        try:
            surf = pygame.Surface((1920, 1080))
            gs.draw_feed(surf, gs.Layout(1920, 1080), time.localtime(), CARD, True, msg, msg and msg.get("state"), 0.3)
        finally:
            gs._line = real_line
        return calls

    def test_badge_and_name_replace_the_caption(self):
        calls = self._draw({"from": "Rhea", "text": "…", "state": "speaking", "voice": True})
        self.assertIn("Rhea", calls)
        self.assertNotIn("Sports", calls)
        r = gs.LAST_PORTRAIT_RECT
        self.assertIsNotNone(r)
        L = gs.Layout(1920, 1080)
        self.assertLess(r.bottom, L(120))  # clear of the card top
        self.assertEqual(gs.LAST_PORTRAIT_ARGS[1], L(52))
        self.assertLess(gs.LAST_PORTRAIT_ARGS[1], 60)

    def test_idle_brings_back_the_caption(self):
        calls = self._draw(None)
        self.assertIn("Sports", calls)
        self.assertIsNone(gs.LAST_PORTRAIT_RECT)

    def test_missing_avatar_falls_back(self):
        calls = self._draw({"from": "Nobody-Here", "text": "…", "state": "listening", "voice": True})
        self.assertIn("Nobody-Here", calls)
        self.assertIsNotNone(gs.LAST_PORTRAIT_RECT)


class Stale(unittest.TestCase):
    def test_new_turn_clears_on_response(self):
        tile = {"shown": 100.0}
        self.assertEqual(gs.tile_step(tile, "speaking", "listening", 101, 10), "keep")  # not pulled mid-listen
        self.assertEqual(gs.tile_step(tile, "listening", "thinking", 102, 10), "keep")
        self.assertEqual(gs.tile_step(tile, "thinking", "speaking", 103, 10), "clear")

    def test_expired_during_session_clears_when_speaking_starts(self):
        tile = {"shown": 100.0}
        self.assertEqual(gs.tile_step(tile, "speaking", "speaking", 130, 10), "keep")  # mid-answer: no yank
        self.assertEqual(gs.tile_step(tile, "speaking", "thinking", 131, 10), "keep")
        self.assertEqual(gs.tile_step(tile, "thinking", "speaking", 132, 10), "clear")

    def test_response_with_a_new_card_shows_it(self):
        tile = {"shown": 100.0}
        gs.tile_step(tile, "speaking", "listening", 101, 10)
        tile.clear(); tile.update({"card": CARD}, shown=102.0)  # FEED stores the response's tile fresh
        self.assertEqual(gs.tile_step(tile, "thinking", "speaking", 103, 10), "keep")

    def test_fresh_tile_in_its_own_turn_stays(self):
        tile = {"shown": 100.0}
        self.assertEqual(gs.tile_step(tile, "thinking", "speaking", 101, 10), "keep")


if __name__ == "__main__":
    unittest.main()
