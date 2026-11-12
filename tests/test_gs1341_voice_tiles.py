"""0.13.41: a voice ask for a content tile goes through the relay intent and shows that tile."""
import asyncio
import os
import sys
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import cards
import grok_station as gs
import pygame
import relay

SAMPLE = {"current": {"temperature_2m": 61.4, "apparent_temperature": 59.2, "weather_code": 2,
                      "wind_speed_10m": 8.6, "relative_humidity_2m": 55},
          "daily": {"time": ["2026-10-19", "2026-10-20"], "weather_code": [3, 61],
                    "temperature_2m_max": [66.2, 58.9], "temperature_2m_min": [44.6, 41.0],
                    "precipitation_probability_max": [10, 80]}}


def setUpModule():
    pygame.display.init()
    pygame.font.init()


class Pi:
    def __init__(self):
        self.msgs = []

    async def send_json(self, msg):
        self.msgs.append(msg)


class Http:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def get(self, url, timeout=None):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return self.body


def _sports():
    return {"type": "sports", "v": 1, "league": "City league",
            "home": {"name": "Harbor City", "abbr": "HC"},
            "away": {"name": "Ridgeview", "abbr": "RV"},
            "home_score": 3, "away_score": 2, "status": "live", "period": "2nd",
            "schedule": [{"label": "Harbor City at Ridgeview", "when": "Sat 7pm"}]}


def _calendar():
    return {"type": "calendar", "v": 1, "date": "2026-10-22",
            "events": [{"time": "9:00", "title": "Market"}, {"time": "18:30", "title": "Practice"}]}


def _recipe():
    return {"type": "recipe", "v": 1, "title": "Weeknight soup", "servings": 4,
            "ingredients": ["onion", "carrots", "stock"],
            "steps": ["Chop the vegetables.", "Simmer twenty minutes.", "Season and serve."], "step": 2}


def _home():
    return {"type": "home", "v": 1, "title": "Stocks",
            "items": [{"label": "Northwind", "value": "12.40", "trend": "up"}]}


class Intents(unittest.TestCase):
    def test_every_supported_tile_has_a_phrase(self):
        phrases = {
            "sports": "sports update",
            "recipe": "show the recipe",
            "calendar": "what's on today",
            "home": "home readings",
            "weather": "what's the weather",
            "time": "what time is it",
            "convert": "convert 2 cups to tablespoons",
        }
        self.assertEqual(set(phrases), set(cards.CONTENT_TILES))
        for kind, phrase in phrases.items():
            self.assertEqual(cards.content_tile_intent(phrase), kind, phrase)
            plan = relay.content_tile_plan("hey grok, " + phrase)
            self.assertEqual(plan["tile"], kind, phrase)

    def test_a_five_thousand_digit_number_does_not_raise(self):
        heard = "convert " + ("9" * 5000) + " meters to feet"
        self.assertIsNone(cards.content_tile_args(heard))
        self.assertIsNone(relay.content_tile_plan(heard))

    def test_a_question_that_only_mentions_the_word_is_not_a_tile(self):
        self.assertIsNone(cards.content_tile_intent("can you tell me something about sports later"))
        self.assertIsNone(relay.content_tile_plan("what should I cook"))


class Routing(unittest.TestCase):
    def setUp(self):
        self.deck = gs.deck()
        self._items = list(self.deck._items)
        self.deck._items = []
        self.deck._anchor = None
        self.old_feed = gs.UPDATES
        gs.UPDATES = cards.UpdateFeed()
        gs.UPD_ROT.update(step=0, at=None)
        gs.TILE.clear()
        gs.TIMER_CHIPS[:] = []
        self.lat, self.lon = relay.station_tools.LAT, relay.station_tools.LON
        relay.station_tools.LAT, relay.station_tools.LON = "41.8", "-87.6"

    def tearDown(self):
        self.deck._items = self._items
        self.deck._anchor = None
        gs.UPDATES = self.old_feed
        gs.TILE.clear()
        relay.station_tools.LAT, relay.station_tools.LON = self.lat, self.lon

    def _put(self, kind, card):
        now = time.time()
        self.deck.put("wren", kind + "1", 10, 600, card, now)
        return now

    def test_focus_does_not_delete_the_deck_card_when_the_reply_starts(self):
        now = time.time()
        later = now + gs.SCORE_HOLD + 5
        self._put("sports", _sports())
        self.assertTrue(gs.focus_content_tile("sports", now))
        self.assertTrue(gs.TILE.get("focused"))
        self.assertFalse(gs.apply_speaking_clear("listening", "speaking", later, gs.SCORE_HOLD))
        self.assertIsNotNone(gs.deck().get("wren", "sports1"))
        self.assertTrue(gs.TILE)
        self.assertFalse(gs.release_or_clear_tile(later))
        self.assertIsNotNone(gs.deck().get("wren", "sports1"))
        gs.TILE.clear()
        self.deck._items = []
        self._put("recipe", _recipe())
        self.assertTrue(gs.focus_content_tile("recipe", now))
        self.assertFalse(gs.apply_speaking_clear("thinking", "speaking", later, gs.SCORE_HOLD))
        self.assertIsNotNone(gs.deck().get("wren", "recipe1"))

    def test_focused_card_survives_a_nearly_spent_idle_clock(self):
        now = 1700000000.0
        self.deck.put("wren", "sports1", 10, 3600, _sports(), now)
        self.assertEqual(self.deck.arm_idle("wren", "sports1", now, sports_idle=1), now + 1)
        self.assertTrue(gs.focus_content_tile("sports", now + 0.5))
        later = now + 30
        self.assertEqual(gs.tile_step(gs.TILE, "listening", "speaking", later, gs.SCORE_HOLD), "keep")
        self.assertIsNotNone(gs.deck().current(later))
        self.assertIsNotNone(gs.deck().get("wren", "sports1"))
        self.assertTrue(gs.release_focused_tile(later))
        self.assertFalse(gs.TILE)
        self.assertIsNotNone(gs.deck().current(later + 1))
        self.assertIsNone(gs.deck().current(later + cards.CONVO_IDLE_S + 1))
        self.deck._items = []
        self.deck.put("wren", "recipe1", 10, 7200, _recipe(), now)
        self.deck._items[0]["step_at"] = now - 100000
        self.deck._items[0]["idle_at"] = now + 1
        self.assertTrue(gs.focus_content_tile("recipe", now + 0.5))
        self.assertIsNotNone(gs.deck().current(later))
        self.assertTrue(gs.release_focused_tile(later))
        self.assertIsNotNone(gs.deck().get("wren", "recipe1"))
        self.assertIsNotNone(gs.deck().current(later + 1))

    def test_sports_update_shows_the_sports_tile_over_the_update_card(self):
        now = time.time()
        gs.UPDATES.put("juniper", "u1", {"type": "done", "line": "Filed the list", "time": now})
        self._put("sports", _sports())
        pi = Pi()
        plan = asyncio.run(relay.dispatch_content_tile("sports update", pi, None, {}, now))
        self.assertIsNone(plan)
        self.assertEqual(pi.msgs, [{"type": "show_tile", "tile": "sports"}])
        self.assertTrue(gs.focus_content_tile("sports", now))
        L = gs.Layout(2560, 1706)
        surf = pygame.Surface((2560, 1706))
        voice = {"voice": True, "state": "speaking", "from": "Wren", "text": "Harbor leads.", "heard": "sports update"}
        zone = gs.zone_view(voice, gs.TILE.get("card"))
        gs.draw_zone(surf, L, time.localtime(), type("W", (), {"data": None, "offline": lambda s: False})(), zone=zone)
        self.assertEqual(zone["tile"]["type"], "sports")
        self.assertEqual(gs.LAST_ZONE_RECT, gs.bottom_panel_rect(L))

    def test_each_deck_tile_is_focused_from_its_phrase(self):
        now = time.time()
        samples = (("show the recipe", "recipe", _recipe()),
                   ("what's on today", "calendar", _calendar()),
                   ("sports scores", "sports", _sports()))
        for phrase, kind, card in samples:
            self.deck._items = []
            gs.TILE.clear()
            self._put(kind, card)
            pi = Pi()
            asyncio.run(relay.dispatch_content_tile(phrase, pi, None, {}, now))
            self.assertEqual(pi.msgs[-1], {"type": "show_tile", "tile": kind})
            self.assertTrue(gs.focus_content_tile(kind, now))
            self.assertEqual(gs.TILE["card"]["type"], kind)

    def test_home_readings_rotate_to_the_home_card(self):
        now = time.time()
        self._put("home", _home())
        gs.UPDATES.put("wren", "u1", {"type": "done", "line": "Done", "time": now})
        pi = Pi()
        asyncio.run(relay.dispatch_content_tile("home readings", pi, None, {}, now))
        self.assertEqual(pi.msgs[-1]["tile"], "home")
        self.assertTrue(gs.focus_content_tile("home", now))
        rows = gs.idle_band_rows(now)
        self.assertEqual(rows[0].get("kind"), "home")

    def test_weather_time_and_convert_send_their_cards(self):
        import json

        async def ask(phrase, http):
            pi = Pi()
            await relay.dispatch_content_tile(phrase, pi, http, {}, time.time(), {"turn": 1})
            for _ in range(8):
                await asyncio.sleep(0)
            return pi

        http = Http(200, json.dumps(SAMPLE).encode())
        asks = (("what's the weather", "weather"),
                ("what time is it", "time"),
                ("convert 2 cups to tablespoons", "convert"))
        for phrase, kind in asks:
            pi = asyncio.run(ask(phrase, http))
            self.assertEqual(pi.msgs[-1]["type"], "card", phrase)
            self.assertEqual(pi.msgs[-1]["kind"], kind, phrase)
            L = gs.Layout(1280, 800)
            surf = pygame.Surface((1280, 800))
            msg = {"from": "Grok", "voice": True, "heard": phrase, "text": "Here.", "state": "speaking",
                   "card": pi.msgs[-1]}
            gs.draw_session(surf, L, time.localtime(), msg)
            self.assertIsNotNone(gs.LAST_CARD_RECT, phrase)

    def test_weather_does_not_block_the_turn_or_fetch_twice(self):
        import json

        class Slow(Http):
            def __init__(self):
                super().__init__(200, json.dumps(SAMPLE).encode())
                self.gets = 0
                self.entered = asyncio.Event()

            def get(self, url, timeout=None):
                self.gets += 1
                self.entered.set()
                return self

            async def read(self):
                await asyncio.sleep(0.4)
                return self.body

        async def run():
            http = Slow()
            pi = Pi()
            st = {"turn": 1}
            started = time.monotonic()
            await relay.dispatch_content_tile("what's the weather", pi, http, {}, time.time(), st)
            self.assertLess(time.monotonic() - started, 0.2)
            await http.entered.wait()
            out, card = await relay.take_tool_card("get_weather", {}, http, {}, time.time(), st)
            self.assertTrue(out.get("ok"))
            for _ in range(20):
                if any(m.get("type") == "card" for m in pi.msgs):
                    break
                await asyncio.sleep(0.05)
            cards_sent = [m for m in pi.msgs if m.get("type") == "card"]
            if card:
                cards_sent.append(card)
            self.assertEqual(http.gets, 1)
            self.assertEqual(len(cards_sent), 1)
            self.assertEqual(cards_sent[0]["kind"], "weather")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
