"""0.13.11 recipe tile: fixed rail, voice hints, re-pack, stop-all, ringing banner.

Geometry is the SPEC's 2560×1706 panel. Sample dishes are invented.
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
sys.path.insert(0, os.path.join(ROOT, "wake"))
import cards  # noqa: E402
import grok_station as gs  # noqa: E402
import grok_wake as gw  # noqa: E402
import tools as T  # noqa: E402
import pygame  # noqa: E402

EPOCH = 1_700_000_000
NOW = time.strptime("2026-10-28 19:42", "%Y-%m-%d %H:%M")
PANEL = (2560, 1706)
RECIPE = {
    "v": 1, "type": "recipe", "title": "Lemon herb orzo", "servings": 4,
    "ingredients": ["orzo", "lemon", "garlic"],
    "steps": ["Boil the orzo for 10 minutes.", "Fold in the herbs.", "Serve warm."],
    "step": 2,
}
SPORTS = {
    "v": 1, "type": "sports", "league": "City league",
    "home": {"name": "Harbor City", "abbr": "HC"},
    "away": {"name": "Ridgeview", "abbr": "RV"},
    "home_score": 3, "away_score": 2, "status": "live", "period": "2nd",
    "schedule": [{"label": "Harbor City at Ridgeview", "when": "Sat 7pm"}],
}


def setUpModule():
    pygame.display.init()
    pygame.font.init()


def _timer(tid, label, left, duration=None, ringing=False):
    rec = {"id": tid, "label": label, "ends_at_epoch": EPOCH + left, "ringing": ringing}
    if duration is not None:
        rec["duration"] = duration
    return rec


def _book(rows, now=1000.0):
    """Relay timer book. `left` is seconds from now (negative once it has ended)."""
    timers = {}
    for tid, label, left, ringing in rows:
        timers[tid] = {"label": label, "due": now + left, "duration": max(abs(left), 1), "ringing": ringing}
    return timers


class Layout(unittest.TestCase):
    def setUp(self):
        self.old_chips = list(gs.TIMER_CHIPS)
        self.old_epoch = gs.TIMER_EPOCH
        self.old_motion = os.environ.get("GROK_STATION_REDUCE_MOTION")
        gs.TIMER_CHIPS[:] = []
        gs.TIMER_EPOCH = EPOCH
        os.environ.pop("GROK_STATION_REDUCE_MOTION", None)

    def tearDown(self):
        gs.TIMER_CHIPS[:] = self.old_chips
        gs.TIMER_EPOCH = self.old_epoch
        if self.old_motion is None:
            os.environ.pop("GROK_STATION_REDUCE_MOTION", None)
        else:
            os.environ["GROK_STATION_REDUCE_MOTION"] = self.old_motion

    def _draw(self, card=None, chips=(), timers=()):
        gs.TIMER_CHIPS[:] = [dict(t) for t in timers]
        gs.TIMER_EPOCH = EPOCH
        surf = pygame.Surface(PANEL)
        gs.draw_feed(surf, gs.Layout(*PANEL), NOW, card or dict(RECIPE), True, None, None, 1.0, chips)
        return surf

    def test_step_card_height_is_unchanged_when_timers_are_up(self):
        self._draw(timers=[])
        empty = pygame.Rect(gs.LAST_RECIPE_FRAME["step"])
        self._draw(timers=[
            _timer("aaaa01", "orzo", 600, 600),
            _timer("aaaa02", "garlic bread", 300, 300),
            _timer("aaaa03", "timer", 120, 120),
        ])
        full = pygame.Rect(gs.LAST_RECIPE_FRAME["step"])
        self.assertEqual(empty, pygame.Rect(712, 216, 1168, 1246))
        self.assertEqual(full, empty)
        self.assertEqual(gs.LAST_RECIPE_FRAME["ingredients"], pygame.Rect(128, 216, 560, 1246))
        self.assertEqual(gs.LAST_RECIPE_FRAME["rail"], pygame.Rect(1904, 216, 528, 1246))
        self.assertEqual([pygame.Rect(r) for r in gs.LAST_RECIPE_FRAME["slots"]], [
            pygame.Rect(1904, 216, 528, 400),
            pygame.Rect(1904, 639, 528, 400),
            pygame.Rect(1904, 1062, 528, 400),
        ])

    def test_outside_the_rail_nothing_shifts_when_timers_change(self):
        keys = ("title", "banner", "clock", "status", "ingredients", "step", "hint")
        self._draw(timers=[])
        before = {k: pygame.Rect(gs.LAST_RECIPE_FRAME[k]) for k in keys}
        self._draw(timers=[
            _timer("aaaa01", "orzo", 400, 600),
            _timer("aaaa02", "garlic bread", 200, 300),
        ])
        after = {k: pygame.Rect(gs.LAST_RECIPE_FRAME[k]) for k in keys}
        self.assertEqual(before, after)
        self.assertEqual(before["banner"], pygame.Rect(780, 56, 1130, 104))
        self.assertEqual(before["hint"], pygame.Rect(128, 1494, 2304, 84))
        self.assertEqual(before["title"], pygame.Rect(128, 56, 620, 104))

    def test_hint_row_copy_does_not_reflow_on_step_1(self):
        calls = []
        real = gs.draw_voice_hint

        def wrap(surface, segments, x, y, px=36, max_w=None):
            calls.append(list(segments))
            return real(surface, segments, x, y, px=px, max_w=max_w)

        gs.draw_voice_hint = wrap
        try:
            step1 = dict(RECIPE, step=1)
            step2 = dict(RECIPE, step=2)
            self._draw(card=step1)
            rect1 = pygame.Rect(gs.LAST_RECIPE_FRAME["hint"])
            segs1 = calls[-1]
            self._draw(card=step2)
            rect2 = pygame.Rect(gs.LAST_RECIPE_FRAME["hint"])
            segs2 = calls[-1]
        finally:
            gs.draw_voice_hint = real
        self.assertEqual(segs1, list(gs.RECIPE_HINT_SEGMENTS))
        self.assertEqual(segs2, list(gs.RECIPE_HINT_SEGMENTS))
        plain = gs.voice_hint_plain(segs1)
        for phrase in ("next step", "previous step", "close recipe"):
            self.assertIn("\u201c" + phrase + "\u201d", plain)
        for banned in ("set a timer", "minute", "dismiss timer", "what's running"):
            self.assertNotIn(banned, plain.lower())
        self.assertEqual(rect1, pygame.Rect(128, 1494, 2304, 84))
        self.assertEqual(rect1, rect2)

    def test_previous_step_on_step_1_keeps_the_hint_and_says_first(self):
        deck = cards.Deck()
        deck.put("juniper", "orzo01", 1, 3600, dict(RECIPE, step=1), 1000.0)
        out = cards.apply_recipe_command(deck, "prev", 1000.0)
        self.assertEqual(out["say"], cards.FIRST_STEP_LINE)
        self.assertEqual(out["step"], 1)
        self.assertIn("previous step", gs.voice_hint_plain(gs.RECIPE_HINT_SEGMENTS))


class Rail(unittest.TestCase):
    def test_three_timers_repack_up_when_the_first_is_dismissed(self):
        order = ["aaaa01", "aaaa02", "aaaa03"]
        timers = [
            _timer("aaaa01", "orzo", 600, 600),
            _timer("aaaa02", "timer", 300, 300),
            _timer("aaaa03", "garlic bread", 180, 180),
        ]
        rail = gs.recipe_rail(timers, EPOCH, order)
        self.assertEqual([s["id"] for s in rail["slots"]], order)
        left = [t for t in timers if t["id"] != "aaaa01"]
        after = gs.recipe_rail(left, EPOCH, ["aaaa02", "aaaa03"])
        self.assertEqual([s.get("id") for s in after["slots"]], ["aaaa02", "aaaa03", None])
        self.assertEqual(after["slots"][0]["title"], "Timer · 5 min")
        self.assertEqual(after["slots"][0]["source"], "Set by voice")
        self.assertEqual(after["slots"][2]["kind"], "empty")
        ys = [216, 639, 1062]
        before = {"aaaa01": 0, "aaaa02": 1, "aaaa03": 2}
        now_at = {"aaaa02": 0, "aaaa03": 1}
        self.assertEqual(gs.rail_translate(before, now_at, 0, False, ys)["aaaa02"], 423)
        self.assertEqual(gs.rail_translate(before, now_at, 0, False, ys)["aaaa03"], 423)
        self.assertEqual(gs.rail_translate(before, now_at, 240, False, ys)["aaaa02"], 0)
        self.assertEqual(gs.rail_translate(before, now_at, 0, True, ys)["aaaa02"], 0)
        mid = gs.rail_translate(before, now_at, 120, False, ys)["aaaa02"]
        self.assertGreater(mid, 0)
        self.assertLess(mid, 423)

    def test_four_timers_use_plus_n_and_unfold_when_the_oldest_goes(self):
        order = ["aaaa01", "aaaa02", "aaaa03", "aaaa04"]
        timers = [
            _timer("aaaa01", "orzo", 500, 600),
            _timer("aaaa02", "timer", 300, 300),
            _timer("aaaa03", "garlic bread", 120, 180),
            _timer("aaaa04", "Timer 2", 400, 400),
        ]
        rail = gs.recipe_rail(timers, EPOCH, order)
        self.assertEqual(rail["slots"][0]["id"], "aaaa01")
        self.assertEqual(rail["slots"][1]["id"], "aaaa02")
        more = rail["slots"][2]
        self.assertEqual(more["kind"], "more")
        self.assertEqual(more["label"], "+2 more")
        self.assertEqual([r["id"] for r in more["rows"]], ["aaaa03", "aaaa04"])  # soonest first
        left = [t for t in timers if t["id"] != "aaaa01"]
        after = gs.recipe_rail(left, EPOCH, order[1:])
        self.assertEqual([s["id"] for s in after["slots"]], ["aaaa02", "aaaa03", "aaaa04"])
        self.assertEqual(after["slots"][0]["title"], "Timer · 5 min")
        self.assertEqual(after["slots"][1]["title"], "Garlic bread")
        self.assertEqual(after["slots"][2]["title"], "Timer 2")
        ys = [216, 639, 1062]
        before = rail["positions"]
        dy = gs.rail_translate(before, after["positions"], 0, False, ys)
        self.assertEqual(dy["aaaa02"], ys[1] - ys[0])
        self.assertEqual(dy["aaaa03"], ys[2] - ys[1])
        self.assertEqual(len(dy), 3)  # one slide for everything that remains

    def test_a_listed_timer_that_rings_takes_slot_3(self):
        order = ["aaaa01", "aaaa02", "aaaa03", "aaaa04"]
        timers = [
            _timer("aaaa01", "orzo", 500, 600),
            _timer("aaaa02", "garlic bread", 400, 400),
            _timer("aaaa03", "timer", 300, 300),
            _timer("aaaa04", "Timer 2", -1, 60, ringing=True),
        ]
        rail = gs.recipe_rail(timers, EPOCH, order)
        self.assertEqual(rail["slots"][2]["kind"], "ringing")
        self.assertEqual(rail["slots"][2]["id"], "aaaa04")
        self.assertEqual(rail["slots"][2]["done"], "Timer 2 is done")

    def test_empty_rail_says_no_timers_only_in_slot_1(self):
        rail = gs.recipe_rail([], EPOCH)
        self.assertEqual([s["kind"] for s in rail["slots"]], ["empty", "empty", "empty"])
        self._drawn = None
        surf = pygame.Surface(PANEL)
        gs.TIMER_CHIPS[:] = []
        gs.TIMER_EPOCH = EPOCH
        gs.draw_feed(surf, gs.Layout(*PANEL), NOW, dict(RECIPE))
        self.assertIn("No timers", gs.LAST_RECIPE_COPY)
        self.assertEqual(gs.LAST_RECIPE_RAIL["slots"][0]["label"], "No timers")
        self.assertNotIn("label", gs.LAST_RECIPE_RAIL["slots"][1])


class Banner(unittest.TestCase):
    def setUp(self):
        self.old_chips = list(gs.TIMER_CHIPS)
        self.old_epoch = gs.TIMER_EPOCH
        gs.TIMER_EPOCH = EPOCH

    def tearDown(self):
        gs.TIMER_CHIPS[:] = self.old_chips
        gs.TIMER_EPOCH = self.old_epoch

    def test_banner_copy_for_one_two_and_many(self):
        self.assertEqual(gs.banner_copy(["Orzo"]), "Orzo timer is done")
        self.assertEqual(gs.banner_copy(["Orzo", "Garlic bread"]), "Orzo and Garlic bread are done")
        self.assertEqual(gs.banner_copy(["Orzo", "Garlic bread", "Timer"]), "Orzo, Garlic bread + 1 more")
        self.assertEqual(gs.banner_copy(["Brisket rest", "Rice", "Timer", "Timer 2"]), "Brisket rest, Rice + 2 more")

    def test_names_cap_at_350_and_plus_n_more_does_not(self):
        font = gs.font("display", 54)
        wide = "W" * 24
        names = [wide, wide] + ["Rice"] * 6
        parts = gs.banner_parts(names, font, limit=350)
        self.assertLessEqual(font.size(parts["names"][0])[0], 350)
        self.assertLessEqual(font.size(parts["names"][1])[0], 350)
        self.assertNotEqual(parts["names"][0], wide)
        self.assertIn("\u2026", parts["names"][0])
        self.assertEqual(parts["suffix"], "+ 6 more")
        self.assertNotIn("\u2026", parts["suffix"])
        self.assertTrue(parts["text"].endswith("+ 6 more"))
        self.assertEqual(font.size(parts["suffix"])[0], font.size("+ 6 more")[0])

    def test_banner_names_follow_end_order(self):
        timers = [
            _timer("aaaa01", "orzo", -5, 60, ringing=True),
            _timer("aaaa02", "garlic bread", -30, 60, ringing=True),
            _timer("aaaa03", "timer", -1, 60, ringing=True),
            _timer("aaaa04", "rice", 90, 90),
        ]
        # garlic bread ended first, then orzo, then timer
        rail = gs.recipe_rail(timers, EPOCH, ["aaaa04", "aaaa01", "aaaa02", "aaaa03"])
        self.assertEqual(rail["banner_names"], ["Garlic bread", "Orzo", "Timer"])
        self.assertEqual(gs.banner_copy(rail["banner_names"]), "Garlic bread, Orzo + 1 more")

    def test_chips_hide_while_ringing_and_return_after(self):
        chip = {"bot": "juniper", "id": "game01", "card": SPORTS, "until": EPOCH + 999, "at": EPOCH}
        ringing = [_timer("aaaa01", "orzo", -2, 60, ringing=True)]
        surf = pygame.Surface(PANEL)
        gs.TIMER_CHIPS[:] = ringing
        gs.draw_feed(surf, gs.Layout(*PANEL), NOW, dict(RECIPE), True, None, None, 1.0, [chip])
        self.assertEqual(gs.LAST_CHIP_RECTS, [])
        self.assertEqual(gs.LAST_BANNER_TEXT, "Orzo timer is done")
        gs.TIMER_CHIPS[:] = [_timer("aaaa04", "rice", 90, 90)]
        gs.draw_feed(surf, gs.Layout(*PANEL), NOW, dict(RECIPE), True, None, None, 1.0, [chip])
        self.assertEqual(gs.LAST_BANNER_TEXT, "")
        self.assertTrue(gs.LAST_CHIP_RECTS)
        self.assertGreaterEqual(gs.LAST_CHIP_RECTS[0].right, gs.LAST_RECIPE_FRAME["banner"].left)


class Motion(unittest.TestCase):
    def setUp(self):
        self.old_chips = list(gs.TIMER_CHIPS)
        self.old_epoch = gs.TIMER_EPOCH
        self.old_motion = os.environ.get("GROK_STATION_REDUCE_MOTION")
        gs.TIMER_EPOCH = EPOCH

    def tearDown(self):
        gs.TIMER_CHIPS[:] = self.old_chips
        gs.TIMER_EPOCH = self.old_epoch
        if self.old_motion is None:
            os.environ.pop("GROK_STATION_REDUCE_MOTION", None)
        else:
            os.environ["GROK_STATION_REDUCE_MOTION"] = self.old_motion

    def _paint(self, epoch):
        gs.TIMER_CHIPS[:] = [_timer("aaaa01", "orzo", -1, 60, ringing=True)]
        gs.TIMER_EPOCH = epoch
        surf = pygame.Surface(PANEL)
        gs.draw_feed(surf, gs.Layout(*PANEL), NOW, dict(RECIPE))
        return surf

    def test_reduced_motion_is_a_static_outline_not_a_pulse(self):
        os.environ["GROK_STATION_REDUCE_MOTION"] = "1"
        a = self._paint(float(EPOCH))
        b = self._paint(float(EPOCH) + 0.5)
        self.assertEqual(a.get_buffer().raw, b.get_buffer().raw)
        slot = gs.LAST_RECIPE_FRAME["slots"][0]
        outline = a.get_at((slot.left - 13, slot.centery))
        self.assertGreater(outline.r, 180)
        self.assertGreater(outline.g, 140)
        os.environ["GROK_STATION_REDUCE_MOTION"] = "0"
        still = self._paint(float(EPOCH))
        peak = self._paint(float(EPOCH) + 0.5)
        self.assertNotEqual(still.get_buffer().raw, peak.get_buffer().raw)
        quiet = still.get_at((slot.left - 13, slot.centery))
        self.assertLess(quiet.r, 80)


class StopAndClose(unittest.TestCase):
    def test_stop_clears_every_ringing_timer_and_leaves_running(self):
        now = 1000.0
        timers = _book([
            ("aaaa01", "orzo", -30, True),
            ("aaaa02", "garlic bread", -10, True),
            ("aaaa03", "timer", -1, True),
            ("aaaa04", "rice", 90, False),
        ], now)
        out, msg = T.cancel_timer({"label": "stop"}, timers, now + 1)
        self.assertTrue(out["ok"])
        self.assertEqual(set(timers), {"aaaa04"})
        self.assertEqual(timers["aaaa04"]["label"], "rice")
        ids = [m["id"] for m in msg]
        self.assertEqual(ids, ["aaaa01", "aaaa02", "aaaa03"])
        self.assertEqual(out["say"], "Stopped Orzo, Garlic bread and Timer.")
        again = T.clear_ringing_utterance("stop the timer", timers, now + 1)
        self.assertIsNone(again)

    def test_dismiss_timer_clears_all_but_bare_dismiss_clears_one(self):
        now = 1000.0
        timers = _book([
            ("aaaa01", "orzo", -40, True),
            ("aaaa02", "garlic bread", -10, True),
            ("aaaa04", "rice", 90, False),
        ], now)
        one = T.clear_ringing_utterance("dismiss", timers, now)
        self.assertEqual(one["ids"], ["aaaa01"])  # ended first, not the recipe chip
        self.assertNotIn("aaaa01", timers)
        self.assertIn("aaaa02", timers)
        self.assertIn("aaaa04", timers)
        self.assertIsNone(T.clear_ringing_utterance("dismiss", {"aaaa04": timers["aaaa04"]}, now))
        self.assertIsNone(T.clear_ringing_utterance("clear that", timers, now))
        rest = T.clear_ringing_utterance("dismiss timer", timers, now)
        self.assertEqual(rest["ids"], ["aaaa02"])
        self.assertEqual(set(timers), {"aaaa04"})

    def test_close_recipe_keeps_timers_as_idle_rings(self):
        deck = cards.Deck()
        now = 1000.0
        deck.put("juniper", "orzo01", 1, 3600, dict(RECIPE), now)
        timers = [
            _timer("aaaa01", "orzo", 462, 600),
            _timer("aaaa02", "garlic bread", 120, 180),
        ]
        closed = cards.apply_recipe_command(deck, "close", now)
        self.assertTrue(closed["closed"])
        self.assertIsNone(deck.current(now))
        rail = gs.recipe_rail(timers, EPOCH, ["aaaa01", "aaaa02"])
        self.assertEqual([s["id"] for s in rail["slots"] if s.get("id")], ["aaaa01", "aaaa02"])
        self.assertEqual(gs.screen_for(None, False, None, False, None, True), "timers")
        payload = gs.timer_screen(timers, EPOCH)
        self.assertEqual(payload["mode"], "rings")
        self.assertEqual([r["title"] for r in payload["rings"]], ["Garlic Bread", "Orzo"])
        self.assertEqual(gs.timer_aria("Orzo", 462), "Orzo, 7 minutes 42 seconds left")

    def test_tone_repeats_every_10s_then_every_60s_until_the_hour(self):
        self.assertFalse(gw.alarm_repeat_due(0, 0))
        self.assertFalse(gw.alarm_repeat_due(9, 0))
        self.assertTrue(gw.alarm_repeat_due(10, 0))
        self.assertTrue(gw.alarm_repeat_due(120, 110))
        self.assertFalse(gw.alarm_repeat_due(121, 120))
        self.assertTrue(gw.alarm_repeat_due(180, 120))
        self.assertTrue(gw.alarm_repeat_due(3540, 3480))
        self.assertFalse(gw.alarm_repeat_due(3600, 3540))
        self.assertFalse(gw.alarm_repeat_due(4000, 3540))


if __name__ == "__main__":
    unittest.main()
