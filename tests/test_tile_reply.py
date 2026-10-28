"""Items 5 and 9: a tile a bot posts during a conversation shows at once, alone, and goes away on time."""
import os, sys, unittest
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import grok_station as gs  # noqa: E402
import cards  # noqa: E402

SPORTS = {"type": "sports", "home": "A", "away": "B", "home_score": 3, "away_score": 2, "status": "live"}

class TileReply(unittest.TestCase):
    def test_tile_wins_over_session_text_and_spinner(self):
        msg = {"voice": True, "state": "speaking", "text": "The score is 3 to 2."}
        self.assertEqual(gs.screen_for(None, False, msg, False, SPORTS, False, SPORTS), "feed")
        self.assertEqual(gs.screen_for(None, True, msg, False, SPORTS, False, SPORTS), "feed")
        # without a tile the session still shows
        self.assertEqual(gs.screen_for(None, False, msg, False, None, False, None), "session")
        # setup still wins; a non-tile card does not take over
        self.assertEqual(gs.screen_for({"mode": "setup"}, False, msg, False, SPORTS, False, SPORTS), "setup")
        self.assertEqual(gs.screen_for(None, False, msg, False, None, False, {"type": "weather"}), "session")

    def test_hold_is_whichever_is_longer(self):
        self.assertEqual(gs.tile_end(100, 10, 105), 110)   # speech ended early: 10 s after it showed
        self.assertEqual(gs.tile_end(100, 10, 130), 130)   # long reply: when speech ends

    def test_hold_setting(self):
        self.assertEqual(gs.score_hold_s({}), 10)
        self.assertEqual(gs.score_hold_s({"GROK_STATION_SCORE_HOLD": "25"}), 25)
        for bad in ("0", "121", "x", "-3", ""):
            self.assertEqual(gs.score_hold_s({"GROK_STATION_SCORE_HOLD": bad}), 10, bad)

    def test_deck_get(self):
        d = cards.Deck()
        self.assertIsNone(d.get("kai", "x"))

    def test_reply_waits_briefly_for_a_card_and_never_on_the_wake_path(self):
        # A card already here skips the text. No previous screen shows the text at once.
        self.assertEqual(gs.reply_visual(10, 11.5, {"type": "sports"}, True), "card")
        self.assertEqual(gs.reply_visual(10, 11.5, None, False), "text")
        self.assertEqual(gs.reply_visual(10, 11.5, None, True), "hold")
        self.assertEqual(gs.reply_visual(11.5, 11.5, None, True), "text")
        wake = open(os.path.join(ROOT, "wake", "grok_wake.py"), encoding="utf-8").read()
        self.assertNotIn("TEXT_CARD_WAIT", wake)

    def test_same_persona_matches_a_display_name_to_a_slug(self):
        self.assertTrue(gs.persona_match("Grok", "grok"))
        self.assertTrue(gs.persona_match("Jules", "jules"))
        self.assertFalse(gs.persona_match("Grok", "jules"))
        self.assertFalse(gs.persona_match("", "grok"))

    def test_loop_wiring(self):
        src = open(os.path.join(ROOT, "app", "grok_station.py"), encoding="utf-8").read()
        self.assertIn("pygame.event.Event(FEED, tile=tile)", src)
        self.assertIn("deck().arm_idle(", src)
        self.assertIn("TEXT_CARD_WAIT", src)
        self.assertIn("reply_visual(", src)
        self.assertNotIn("until = tile_end(TILE[\"shown\"], SCORE_HOLD, time.time())", src)
        self.assertIn("deck().clear(TILE.get(\"bot\"), TILE.get(\"id\"))", src)
