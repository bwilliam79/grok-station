"""0.13.42: a follow-up sports score refreshes the tile that is already up."""
import os
import sys
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "relay"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import pygame  # noqa: E402
from test_gs1216_bottom_zone import SPORTS, Harness  # noqa: E402


def setUpModule():
    pygame.display.init()
    pygame.font.init()


class FollowupScore(unittest.TestCase):
    def test_two_sports_asks_both_show_the_tile(self):
        second = dict(SPORTS)
        second["home_score"] = 10
        second["away_score"] = 9
        h = Harness()
        frames = h.run(
            h.state("speaking", text="Harbor City leads 3 to 2."),
            h.put("jules", "game", SPORTS),
            h.state("listening"),
            h.put("jules", "game", second),
            h.state("listening"),
            h.state("thinking", heard="what is the score now"),
            h.state("speaking", heard="what is the score now", text="Harbor City now leads 10 to 9."),
        )
        sports = [f for f in frames if (f.get("zone") or {}).get("tile")
                  and f["zone"]["tile"].get("type") == "sports"]
        self.assertTrue(any(f["zone"]["tile"]["home_score"] == 3 for f in sports))
        last = frames[-1]["zone"]["tile"]
        self.assertIsNotNone(last)
        self.assertEqual(last["type"], "sports")
        self.assertEqual(last["home_score"], 10)
        self.assertEqual(last["away_score"], 9)
        kept = h.deck.get("jules", "game")
        self.assertEqual(kept["home_score"], 10)
        self.assertEqual(kept["away_score"], 9)

    def test_listening_refresh_keeps_the_tile_and_a_later_turn_clears_it(self):
        """tile_step itself: a second listening post keeps the tile; a real turn still clears it."""
        import grok_station as station
        tile = {"shown": 0.0, "card": {"type": "sports", "home_score": 3}}
        self.assertEqual(station.tile_step(tile, "listening", "listening", 1.0, 30.0), "keep")
        self.assertNotIn("stale", tile)
        self.assertEqual(station.tile_step(tile, "speaking", "listening", 2.0, 30.0), "keep")
        self.assertTrue(tile.get("stale"))
        self.assertEqual(station.tile_step(tile, "listening", "speaking", 3.0, 30.0), "clear")


if __name__ == "__main__":
    unittest.main()
