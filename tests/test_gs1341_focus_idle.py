"""0.13.41: a focused deck card gets its idle deadline back when the screen copy is dropped.

focus_content_tile suspends the idle clock. release_focused_tile is what used to
start it again, and only when the conversation ended on that same screen copy.
These tests drive the real main loop.
"""
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

T0 = time.mktime(time.strptime("2026-10-18 14:00", "%Y-%m-%d %H:%M"))
SPORTS = {"v": 1, "type": "sports", "league": "City league", "home": {"name": "Harbor City", "abbr": "HC"},
          "away": {"name": "Ridgeview", "abbr": "RV"}, "home_score": 3, "away_score": 2, "status": "live",
          "period": "2nd", "schedule": [{"label": "Harbor City at Ridgeview", "when": "Sat 7pm"}]}
RECIPE = {"v": 1, "type": "recipe", "title": "Weeknight soup", "servings": 4, "ingredients": ["onion", "stock"],
          "steps": ["Chop the vegetables.", "Simmer twenty minutes.", "Season and serve."], "step": 1}
HOME = {"v": 1, "type": "home", "title": "Stocks",
        "items": [{"label": "Northwind", "value": "12.40", "trend": "up"}]}


def setUpModule():
    pygame.display.init()
    pygame.font.init()


class Stop(Exception):
    pass


class FakeTime:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t

    def localtime(self, secs=None):
        return time.localtime(self.t if secs is None else secs)

    def sleep(self, _s):
        pass

    def __getattr__(self, name):
        return getattr(time, name)


class Harness:
    def __init__(self):
        self.clock = FakeTime(T0)
        self.script = []

    def state(self, st=None, **kw):
        body = dict(kw)
        if st:
            body["state"] = st
        return lambda: pygame.event.Event(gs.STATE, **gs.state_event(body))

    def focus(self, kind):
        return self.state(focus=kind)

    def put(self, bot, cid, card, ttl=3600):
        def step():
            gs.deck().put(bot, cid, 10, ttl, card, self.clock.t)
            page = gs.deck().get(bot, cid)
            tile = {"bot": bot, "id": cid, "card": page, "expires": gs.deck().expiry(bot, cid)}
            return pygame.event.Event(gs.FEED, tile=tile)
        return step

    def recipe_cmd(self, cmd):
        def step():
            result = cards.apply_recipe_command(gs.deck(), cmd, self.clock.t)
            if result.get("closed"):
                return pygame.event.Event(gs.FEED, tile=None, recipe_closed=True)
            page = gs.deck().get(result["bot"], result["id"])
            return pygame.event.Event(gs.FEED, tile={"bot": result["bot"], "id": result["id"], "card": page,
                                                     "step_cmd": True}, recipe_cmd=True)
        return step

    def advance(self, seconds):
        def step():
            self.clock.t += seconds
            return pygame.event.Event(pygame.NOEVENT)
        return step

    def call(self, fn):
        def step():
            fn()
            return pygame.event.Event(pygame.NOEVENT)
        return step

    def run(self, *steps):
        self.script = list(steps)
        h = self
        real = {k: getattr(gs, k) for k in (
            "Display", "Weather", "ThreadingHTTPServer", "status_loop", "refresh_dim",
            "read_setup", "FADE", "SNAP", "time", "DECK", "CHIPS", "night_face")}
        real_wait = pygame.event.wait
        real_rot = dict(gs.UPD_ROT)

        class Disp(real["Display"]):
            def __init__(self):
                super().__init__(fb=None, size=(1280, 800))

            def show(self, painter, *args, fade=False, snapshot=True):
                painter(self.screen, self.L, *args)

        class Wx:
            data = {"temp": 64, "code": 2, "day": True, "hi": 71, "lo": 52}

            def offline(self, now=None):
                return False

            def loop(self):
                pass

        class Srv:
            def __init__(self, *a, **k):
                pass

            def serve_forever(self):
                pass

        def wait(_ms):
            ev = pygame.event.poll()
            if ev.type != pygame.NOEVENT:
                return ev
            if not h.script:
                raise Stop
            return h.script.pop(0)()

        gs.Display, gs.Weather, gs.ThreadingHTTPServer = Disp, Wx, Srv
        gs.status_loop = lambda: None
        gs.refresh_dim = lambda force=False: None
        gs.read_setup = lambda: None
        gs.FADE, gs.SNAP, gs.time = False, "", self.clock
        gs.night_face = lambda now: False
        gs.DECK = cards.Deck(rotate_s=20, ttl_max=86400)
        gs.CHIPS = gs.ChipTray()
        self.deck = gs.DECK
        gs.TILE.clear()
        gs.TIMER_CHIPS[:] = []
        pygame.event.wait = wait
        pygame.event.clear()
        try:
            gs.main()
        except Stop:
            pass
        finally:
            pygame.event.wait = real_wait
            for k, v in real.items():
                setattr(gs, k, v)
            gs.TILE.clear()
            gs.TIMER_CHIPS[:] = []
            gs.UPD_ROT.clear()
            gs.UPD_ROT.update(real_rot)
        return self.deck


class FocusIdle(unittest.TestCase):
    def test_a_home_card_during_the_conversation_rearms_the_focused_score(self):
        # A home card stays in the deck beside a score. The voice feed still drops the focused screen copy.
        h = Harness()
        seen = {}

        def suspended():
            seen["idle"] = h.deck.idle_deadline("wren", "sports1")

        h.run(
            h.put("wren", "sports1", SPORTS),
            h.state("speaking", heard="sports update", text="Harbor leads."),
            h.focus("sports"),
            h.call(suspended),
            h.advance(10),
            h.put("wren", "home1", HOME),
        )
        self.assertIsNone(seen["idle"])
        self.assertIsNotNone(h.deck.get("wren", "sports1"))
        self.assertEqual(h.deck.idle_deadline("wren", "sports1"), T0 + 10 + cards.CONVO_IDLE_S)

    def test_training_start_rearms_the_focused_score(self):
        h = Harness()
        h.run(
            h.put("wren", "sports1", SPORTS),
            h.state("speaking", heard="sports update", text="Harbor leads."),
            h.focus("sports"),
            h.advance(10),
            h.state("enroll", heard="1/5|recording|", text="Say Harbor"),
        )
        self.assertIsNotNone(h.deck.get("wren", "sports1"))
        self.assertEqual(h.deck.idle_deadline("wren", "sports1"), T0 + 10 + cards.CONVO_IDLE_S)

    def test_recipe_close_rearms_the_focused_score(self):
        h = Harness()
        h.run(
            h.put("wren", "soup", RECIPE, ttl=7200),
            h.put("wren", "sports1", SPORTS),
            h.state("speaking", heard="sports update", text="Harbor leads."),
            h.focus("sports"),
            h.advance(10),
            h.recipe_cmd("close"),
        )
        self.assertIsNone(h.deck.get("wren", "soup"))
        self.assertIsNotNone(h.deck.get("wren", "sports1"))
        self.assertEqual(h.deck.idle_deadline("wren", "sports1"), T0 + 10 + cards.CONVO_IDLE_S)

    def test_focus_after_the_conversation_ends_keeps_an_idle_deadline(self):
        h = Harness()
        h.run(
            h.put("wren", "sports1", SPORTS),
            h.state("speaking", heard="sports update", text="Harbor leads."),
            h.focus("sports"),
            h.state("idle"),
            h.advance(10),
            h.focus("sports"),
        )
        self.assertIsNotNone(h.deck.get("wren", "sports1"))
        self.assertEqual(h.deck.idle_deadline("wren", "sports1"), T0 + 10 + cards.CONVO_IDLE_S)


if __name__ == "__main__":
    unittest.main()
