"""0.13.11 recipe tile: fixed rail, voice hints, re-pack, stop-all, ringing banner.

Geometry is the SPEC's 2560×1706 panel. Sample dishes are invented.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse

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


def _timer(tid, label, left, duration=None, ringing=False, source=None):
    rec = {"id": tid, "label": label, "ends_at_epoch": EPOCH + left, "ringing": ringing}
    if duration is not None:
        rec["duration"] = duration
    if source is not None:
        rec["source"] = source
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

    def test_a_ringing_timer_in_the_rest_is_promoted(self):
        order = ["aaaa01", "aaaa02", "aaaa03", "aaaa04"]
        timers = [
            _timer("aaaa01", "orzo", 500, 600),
            _timer("aaaa02", "garlic bread", 400, 400),
            _timer("aaaa03", "timer", 300, 300),
            _timer("aaaa04", "Timer 2", -1, 60, ringing=True),
        ]
        rail = gs.recipe_rail(timers, EPOCH, order)
        self.assertEqual(rail["slots"][0]["kind"], "ringing")
        self.assertEqual(rail["slots"][0]["id"], "aaaa04")
        self.assertEqual(rail["slots"][0]["done"], "Timer 2 is done")
        self.assertEqual(rail["slots"][2]["kind"], "more")

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
        self.assertEqual(msg["type"], "timer_cancel")
        self.assertEqual(msg["ids"], ["aaaa01", "aaaa02", "aaaa03"])
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


def _long_step():
    text = ("Chop the herbs and fold them into the warm orzo with lemon, garlic, and a spoon of oil "
            "until the pan looks glossy and the orzo is tender. Serve. more words here to pad.")
    return text[:160]


class ReviewFixes(unittest.TestCase):
    def setUp(self):
        self.old_chips = list(gs.TIMER_CHIPS)
        self.old_epoch = gs.TIMER_EPOCH
        self.old_order = list(gs.RECIPE_RAIL_ORDER)
        self.old_slide = dict(gs._RAIL_SLIDE)
        self.old_motion = os.environ.get("GROK_STATION_REDUCE_MOTION")
        gs.TIMER_EPOCH = EPOCH
        gs.TIMER_CHIPS[:] = []
        os.environ.pop("GROK_STATION_REDUCE_MOTION", None)

    def tearDown(self):
        gs.TIMER_CHIPS[:] = self.old_chips
        gs.TIMER_EPOCH = self.old_epoch
        gs.RECIPE_RAIL_ORDER[:] = self.old_order
        gs._RAIL_SLIDE.clear()
        gs._RAIL_SLIDE.update(self.old_slide)
        if self.old_motion is None:
            os.environ.pop("GROK_STATION_REDUCE_MOTION", None)
        else:
            os.environ["GROK_STATION_REDUCE_MOTION"] = self.old_motion

    def _record_font(self, match):
        seen = []
        real = gs.font

        def wrapped(kind, px):
            inner = real(kind, px)

            class Recording:
                def render(self, text, *args, **kwargs):
                    if match(kind, int(px), str(text)):
                        seen.append(str(text))
                    return inner.render(text, *args, **kwargs)

                def __getattr__(self, name):
                    return getattr(inner, name)

            return Recording()

        return seen, wrapped, real

    def test_long_step_is_ellipsized_on_the_last_full_line(self):
        step = _long_step()
        self.assertEqual(len(step), 160)
        L = gs.Layout(*PANEL)
        body_px = gs.recipe_px(L, 112)
        seen, wrapped, real = self._record_font(lambda kind, px, text: kind == "display" and px == body_px)
        gs.font = wrapped
        try:
            surf = pygame.Surface(PANEL)
            card = dict(RECIPE, steps=[step, "Serve warm."], step=1)
            gs.draw_feed(surf, L, NOW, card)
        finally:
            gs.font = real
        f = real("display", body_px)
        gap = gs.recipe_px(L, 8)
        box_h = gs.recipe_px(L, 828)
        fit = 0
        while (fit + 1) * f.get_height() + fit * gap <= box_h:
            fit += 1
        self.assertGreater(len(gs.wrap(f, step, 1040)), fit)
        self.assertEqual(len(seen), fit)
        self.assertTrue(seen[-1].endswith("\u2026"), seen)

    def test_ringing_name_fits_the_slot_and_keeps_is_done(self):
        L = gs.Layout(*PANEL)
        wide = "W" * 24
        done_px = gs.recipe_px(L, 46)
        seen, wrapped, real = self._record_font(
            lambda kind, px, text: kind == "display" and px == done_px and "is done" in text)
        gs.font = wrapped
        gs.TIMER_CHIPS[:] = [_timer("aaaa01", wide, -1, 60, ringing=True)]
        try:
            surf = pygame.Surface(PANEL)
            gs.draw_feed(surf, L, NOW, dict(RECIPE))
        finally:
            gs.font = real
        self.assertEqual(len(seen), 1, seen)
        line = seen[0]
        self.assertTrue(line.endswith(" is done"), line)
        self.assertIn("\u2026", line)
        slot = gs.LAST_RECIPE_FRAME["slots"][0]
        room = slot.w - gs.recipe_px(L, 48)
        self.assertLessEqual(real("display", done_px).size(line)[0], room)

    def test_ringing_slot_draws_a_200px_amber_ring(self):
        calls = []
        real = gs._paint_countdown_ring

        def wrap(diameter, fraction, color, pulse):
            calls.append((int(diameter), float(fraction), color))
            return real(diameter, fraction, color, pulse)

        gs._paint_countdown_ring = wrap
        gs.TIMER_CHIPS[:] = [_timer("aaaa01", "orzo", -1, 60, ringing=True)]
        try:
            surf = pygame.Surface(PANEL)
            gs.draw_feed(surf, gs.Layout(*PANEL), NOW, dict(RECIPE))
        finally:
            gs._paint_countdown_ring = real
        want = gs.recipe_px(gs.Layout(*PANEL), 200)
        self.assertIn((want, 1.0, gs.AMBER), calls)

    def test_hint_stop_and_banner_share_recipe_scale(self):
        size = (1920, 1200)
        L = gs.Layout(*size)
        hints = []
        real_hint = gs.draw_voice_hint

        def wrap(surface, segments, x, y, px=36, max_w=None):
            hints.append(int(px))
            return real_hint(surface, segments, x, y, px=px, max_w=max_w)

        banner_px = []
        real_font = gs.font

        def font(kind, px):
            inner = real_font(kind, px)

            class Recording:
                def render(self, text, *args, **kwargs):
                    if kind == "display" and "timer is done" in str(text):
                        banner_px.append(int(px))
                    return inner.render(text, *args, **kwargs)

                def __getattr__(self, name):
                    return getattr(inner, name)

            return Recording()

        gs.draw_voice_hint = wrap
        gs.font = font
        gs.TIMER_CHIPS[:] = [_timer("aaaa01", "orzo", -1, 60, ringing=True)]
        try:
            surf = pygame.Surface(size)
            gs.draw_feed(surf, L, NOW, dict(RECIPE))
        finally:
            gs.draw_voice_hint = real_hint
            gs.font = real_font
        self.assertGreaterEqual(len(hints), 2)  # recipe hint and Say "stop"
        self.assertTrue(all(px == gs.recipe_px(L, 48) for px in hints), hints)
        self.assertEqual(banner_px, [gs.recipe_px(L, 54)])

    def test_slide_repaint_is_faster_than_the_idle_tick(self):
        gs._RAIL_SLIDE.clear()
        gs._RAIL_SLIDE.update(sig=None, t0=0.0, before={}, positions=None)
        gs._slide_offsets({"aaaa01": 0}, False, [216, 639, 1062])
        gs._slide_offsets({"aaaa01": 1}, False, [216, 639, 1062])
        self.assertTrue(gs.rail_slide_running())
        self.assertEqual(gs.feed_frame_gap(True, False, False), 0.12)
        self.assertEqual(gs.feed_frame_gap(False, False, False), 0.9)
        self.assertEqual(gs.feed_frame_gap(True, False, True), 0.9)
        gs._RAIL_SLIDE["t0"] = time.monotonic() - 1
        self.assertFalse(gs.rail_slide_running())

    def test_closing_or_opening_a_recipe_resets_the_slide(self):
        gs.RECIPE_FEED_OPEN = True
        gs.RECIPE_RAIL_ORDER[:] = ["stale"]
        gs._RAIL_SLIDE.clear()
        gs._RAIL_SLIDE.update(sig=(("stale", 0),), t0=1.0, before={"stale": 1}, positions={"stale": 0})
        gs.arm_recipe_screen(False)
        self.assertEqual(gs.RECIPE_RAIL_ORDER, [])
        self.assertIsNone(gs._RAIL_SLIDE.get("positions"))
        self.assertFalse(gs.RECIPE_FEED_OPEN)
        gs.RECIPE_RAIL_ORDER[:] = ["stale"]
        gs.arm_recipe_screen(True)
        self.assertEqual(gs.RECIPE_RAIL_ORDER, [])
        self.assertTrue(gs.RECIPE_FEED_OPEN)


class ToneDriver(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(gw.TIMERS), gw.play_file, gw.configured_wav, gw.LIVE.get("session"), gw.LIVE.get("play"))
        gw.TIMERS.clear()
        gw.LIVE["session"] = False
        gw.LIVE["play"] = "dummy"
        self.calls = []
        gw.configured_wav = lambda key: "alarm.wav"
        gw.play_file = lambda dev, path, block=False: self.calls.append(bool(block))

    def tearDown(self):
        saved, play, wav, session, dev = self.saved
        gw.TIMERS.clear()
        gw.TIMERS.update(saved)
        gw.play_file = play
        gw.configured_wav = wav
        gw.LIVE["session"] = session
        gw.LIVE["play"] = dev

    def _ring(self, tid, elapsed, last=0.0):
        now_m = 5000.0
        gw.TIMERS[tid] = {"label": "orzo", "ringing": True, "tone_mono": now_m - elapsed, "tone_at": last,
                          "due": 1, "mono_due": 1}
        return now_m

    def test_a_missed_gap_jumps_to_the_latest_mark(self):
        now_m = self._ring("aaaa01", 65)
        gw._tone_repeats(now_m)
        self.assertEqual(self.calls, [False])
        self.assertEqual(gw.TIMERS["aaaa01"]["tone_at"], 60.0)

    def test_one_nonblocking_tone_covers_every_ringing_timer(self):
        now_m = 5000.0
        for i, tid in enumerate(("aaaa01", "aaaa02", "aaaa03")):
            gw.TIMERS[tid] = {"label": "orzo", "ringing": True, "tone_mono": now_m - 15, "tone_at": 0.0,
                              "due": 1, "mono_due": 1}
        gw._tone_repeats(now_m)
        self.assertEqual(self.calls, [False])
        self.assertTrue(all(gw.TIMERS[tid]["tone_at"] == 10.0 for tid in ("aaaa01", "aaaa02", "aaaa03")))


def _ensure_relay():
    os.environ.setdefault("RELAY_TOKEN", "test-token")
    os.environ.setdefault("XAI_API_KEY", "test-key")
    os.environ.setdefault("NO_SPEECH", "1.0")
    os.environ.setdefault("ALERT_DIR", os.path.join(tempfile.gettempdir(), "gs1311-alerts"))
    os.environ.setdefault("PERSONAS_FILE", os.path.join(tempfile.gettempdir(), "gs1311-personas.json"))
    sys.path.insert(0, os.path.join(ROOT, "relay"))
    import relay
    return relay


class RelayPath(unittest.IsolatedAsyncioTestCase):
    async def _drive(self, transcript, timers, recipe_post=None):
        from aiohttp import web, WSMsgType
        from aiohttp.test_utils import TestClient, TestServer
        relay = _ensure_relay()
        relay.stats["busy"] = 0
        posted = []
        real_post = relay.post_recipe_cmd
        real_tts = relay.tts_pcm

        async def no_tts(*args, **kwargs):
            return None

        def post(cmd):
            posted.append(cmd)
            if recipe_post is None:
                return {"ok": False}
            return recipe_post(cmd)

        relay.post_recipe_cmd = post
        relay.tts_pcm = no_tts
        state = os.path.join(tempfile.mkdtemp(), "timers.json")
        os.environ["GROK_TIMER_STATE"] = state
        xapp = web.Application()

        async def xai(req):
            s = web.WebSocketResponse()
            await s.prepare(req)
            armed = True
            async for m in s:
                if m.type != WSMsgType.TEXT:
                    continue
                ev = json.loads(m.data)
                if ev.get("type") == "session.update":
                    await s.send_json({"type": "session.updated"})
                elif ev.get("type") == "input_audio_buffer.clear":
                    armed = True
                elif ev.get("type") == "input_audio_buffer.append" and armed:
                    armed = False
                    await s.send_json({"type": "input_audio_buffer.speech_started"})
                    await s.send_json({"type": "input_audio_buffer.speech_stopped"})
                    await s.send_json({"type": "response.created"})
                    await s.send_json({"type": "conversation.item.input_audio_transcription.completed",
                                       "transcript": transcript})
                    await s.send_json({"type": "response.done"})
            return s

        xapp.router.add_get("/rt", xai)
        xs = TestServer(xapp)
        client = ws = mic = None
        events = []
        try:
            await xs.start_server()
            relay.URL = str(xs.make_url("/rt")).replace("http", "ws")
            rapp = web.Application()
            rapp.router.add_get("/ws", relay.session)
            client = TestClient(TestServer(rapp))
            await client.start_server()
            params = {"timers": json.dumps(timers)}
            ws = await client.ws_connect("/ws", headers={"Authorization": "Bearer test-token"}, params=params)

            async def pump():
                while not ws.closed:
                    await ws.send_bytes(b"\0\0" * 1920)
                    await asyncio.sleep(0.05)

            mic = asyncio.create_task(pump())
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                ev = json.loads(msg.data)
                events.append(ev)
                if ev.get("type") in ("turn_done", "done", "timeout", "error"):
                    break
        finally:
            relay.post_recipe_cmd = real_post
            relay.tts_pcm = real_tts
            if mic:
                mic.cancel()
            if ws is not None and not ws.closed:
                await ws.close()
            if client is not None:
                await client.close()
            await xs.close()
            relay.stats["busy"] = 0
        return events, posted

    async def test_session_stop_clears_a_ringing_timer(self):
        timers = [{"id": "aaaa01", "label": "orzo", "left": 0, "ringing": True, "duration": 60}]
        events, posted = await self._drive("stop", timers)
        cancels = [e for e in events if e.get("type") == "timer_cancel"]
        self.assertEqual([e.get("id") for e in cancels], ["aaaa01"])
        replies = [e.get("text") for e in events if e.get("type") == "reply"]
        self.assertIn("Stopped Orzo.", replies)
        self.assertEqual(posted, [])

    async def test_session_dismiss_with_nothing_ringing_drops_the_chip(self):
        timers = [{"id": "aaaa04", "label": "rice", "left": 90, "ringing": False, "duration": 90}]

        def recipe_post(cmd):
            return {"ok": True, "say": "", "closed": False, "bot": "juniper", "step": 1}

        events, posted = await self._drive("dismiss", timers, recipe_post=recipe_post)
        self.assertEqual(posted, ["dismiss"])
        self.assertFalse(any(e.get("type") == "timer_cancel" for e in events))


SRC = {"recipe_id": "orzo01", "step": 3}


def _four_rest_ringing():
    """Oldest order puts both ringing timers in the overflow."""
    order = ["aaaa01", "aaaa02", "aaaa03", "aaaa04"]
    timers = [
        _timer("aaaa01", "orzo", 500, 600),
        _timer("aaaa02", "garlic bread", 400, 400),
        _timer("aaaa03", "rice", -30, 60, ringing=True),
        _timer("aaaa04", "beans", -5, 60, ringing=True),
    ]
    return order, timers


def _five_three_ringing():
    order = ["aaaa01", "aaaa02", "aaaa03", "aaaa04", "aaaa05"]
    timers = [
        _timer("aaaa01", "orzo", 500, 600),
        _timer("aaaa02", "garlic bread", 200, 300),
        _timer("aaaa03", "rice", -40, 60, ringing=True),
        _timer("aaaa04", "beans", -20, 60, ringing=True),
        _timer("aaaa05", "brisket", -5, 60, ringing=True),
    ]
    return order, timers


class Rev4Rail(unittest.TestCase):
    def setUp(self):
        self.old_chips = list(gs.TIMER_CHIPS)
        self.old_epoch = gs.TIMER_EPOCH
        self.old_order = list(gs.RECIPE_RAIL_ORDER)
        self.old_slide = dict(gs._RAIL_SLIDE)
        self.old_open = gs.RECIPE_FEED_OPEN
        gs.TIMER_EPOCH = EPOCH
        gs.TIMER_CHIPS[:] = []
        gs.RECIPE_RAIL_ORDER[:] = []
        gs._RAIL_SLIDE.clear()
        gs._RAIL_SLIDE.update(sig=None, t0=0.0, before={}, positions=None)

    def tearDown(self):
        gs.TIMER_CHIPS[:] = self.old_chips
        gs.TIMER_EPOCH = self.old_epoch
        gs.RECIPE_RAIL_ORDER[:] = self.old_order
        gs._RAIL_SLIDE.clear()
        gs._RAIL_SLIDE.update(self.old_slide)
        gs.RECIPE_FEED_OPEN = self.old_open

    def test_two_ringing_in_the_rest_take_the_first_slots(self):
        order, timers = _four_rest_ringing()
        rail = gs.recipe_rail(timers, EPOCH, order)
        self.assertEqual(rail["slots"][0]["kind"], "ringing")
        self.assertEqual(rail["slots"][0]["id"], "aaaa03")  # ended first
        self.assertEqual(rail["slots"][1]["kind"], "ringing")
        self.assertEqual(rail["slots"][1]["id"], "aaaa04")
        more = rail["slots"][2]
        self.assertEqual(more["kind"], "more")
        self.assertNotEqual(more.get("treatment"), "ringing")
        self.assertEqual(more["label"], "+2 more")
        self.assertEqual([r["id"] for r in more["rows"]], ["aaaa02", "aaaa01"])  # soonest first
        self.assertTrue(all(tid in rail["positions"] for tid in order))
        self.assertEqual(rail["banner_names"], ["Rice", "Beans"])

    def test_three_ringing_put_the_rest_in_an_amber_more_slot(self):
        order, timers = _five_three_ringing()
        rail = gs.recipe_rail(timers, EPOCH, order)
        self.assertEqual([s["id"] for s in rail["slots"][:2]], ["aaaa03", "aaaa04"])
        self.assertTrue(all(s["kind"] == "ringing" for s in rail["slots"][:2]))
        more = rail["slots"][2]
        self.assertEqual(more["kind"], "more")
        self.assertEqual(more.get("treatment"), "ringing")
        self.assertEqual(more["label"], "+3 more")
        self.assertEqual(more["n"], 3)
        self.assertEqual([r["id"] for r in more["rows"]], ["aaaa05", "aaaa02", "aaaa01"])
        self.assertEqual(more["rows"][0]["done"], "Brisket · done")
        self.assertTrue(more["rows"][0]["ringing"])
        self.assertFalse(more["rows"][1]["ringing"])
        for tid in order:
            self.assertIn(tid, rail["positions"])
        rings = []
        real = gs._paint_countdown_ring

        def wrap(diameter, fraction, color, pulse):
            rings.append(int(diameter))
            return real(diameter, fraction, color, pulse)

        gs._paint_countdown_ring = wrap
        gs.RECIPE_RAIL_ORDER[:] = list(order)
        gs.TIMER_CHIPS[:] = [dict(t) for t in timers]
        try:
            surf = pygame.Surface(PANEL)
            gs.draw_feed(surf, gs.Layout(*PANEL), NOW, dict(RECIPE))
        finally:
            gs._paint_countdown_ring = real
        slot = gs.LAST_RECIPE_FRAME["slots"][2]
        self.assertEqual(len(rings), 2)  # full bell rings on slots 1 and 2 only
        fill = surf.get_at((slot.left + 48, slot.top + 16))
        edge = surf.get_at((slot.left + 4, slot.centery))
        self.assertEqual((fill.r, fill.g, fill.b), (gs.ALERT_FILL.r, gs.ALERT_FILL.g, gs.ALERT_FILL.b))
        self.assertEqual((edge.r, edge.g, edge.b), (gs.AMBER.r, gs.AMBER.g, gs.AMBER.b))
        self.assertIn("Brisket · done", gs.LAST_RECIPE_COPY)

    def test_a_later_timer_that_rings_is_promoted(self):
        order = ["aaaa01", "aaaa02", "aaaa03", "aaaa04"]
        running = [
            _timer("aaaa01", "orzo", 500, 600),
            _timer("aaaa02", "garlic bread", 400, 400),
            _timer("aaaa03", "rice", 120, 180),
            _timer("aaaa04", "beans", 300, 300),
        ]
        before = gs.recipe_rail(running, EPOCH, order)
        self.assertEqual(before["slots"][0]["id"], "aaaa01")
        self.assertEqual(before["slots"][1]["id"], "aaaa02")
        self.assertEqual(before["positions"]["aaaa04"], 2)
        promoted = [dict(t) for t in running]
        promoted[3]["ringing"] = True
        promoted[3]["ends_at_epoch"] = EPOCH - 5
        after = gs.recipe_rail(promoted, EPOCH, order)
        self.assertEqual(after["slots"][0]["id"], "aaaa04")
        self.assertEqual(after["slots"][0]["kind"], "ringing")
        self.assertEqual(after["slots"][1]["id"], "aaaa01")
        self.assertEqual(after["positions"]["aaaa04"], 0)
        ys = [216, 639, 1062]
        dy = gs.rail_translate(before["positions"], after["positions"], 0, False, ys)
        self.assertEqual(dy["aaaa04"], ys[2] - ys[0])  # one slide, from the more pile up to slot 1
        self.assertEqual(dy["aaaa03"], 0)  # the other overflow timer stays put
        self.assertEqual(set(dy), set(order))

    def test_stop_repacks_every_ringing_timer_in_one_slide(self):
        order, timers = _five_three_ringing()
        gs.RECIPE_RAIL_ORDER[:] = list(order)
        gs.TIMER_CHIPS[:] = [dict(t) for t in timers]
        surf = pygame.Surface(PANEL)
        gs.draw_feed(surf, gs.Layout(*PANEL), NOW, dict(RECIPE))
        self.assertFalse(gs.rail_slide_running())
        gs.TIMER_CHIPS[:] = [t for t in gs.TIMER_CHIPS if not t.get("ringing")]
        gs.draw_feed(surf, gs.Layout(*PANEL), NOW, dict(RECIPE))
        self.assertTrue(gs.rail_slide_running())
        self.assertEqual([s.get("id") for s in gs.LAST_RECIPE_RAIL["slots"]], ["aaaa01", "aaaa02", None])
        now = 1000.0
        book = _book([
            ("aaaa01", "orzo", -30, True),
            ("aaaa02", "garlic bread", -10, True),
            ("aaaa03", "timer", -1, True),
            ("aaaa04", "rice", 90, False),
        ], now)
        cleared = T.clear_ringing_utterance("stop", book, now)
        self.assertEqual(len(cleared["messages"]), 1)
        self.assertEqual(cleared["messages"][0]["ids"], ["aaaa01", "aaaa02", "aaaa03"])
        self.assertEqual(set(book), {"aaaa04"})
        saved = (dict(gw.TIMERS), gw.post_timers, gw.timers_save)
        gw.TIMERS.clear()
        posts = []
        gw.post_timers = lambda: posts.append(gw.timer_rows())
        gw.timers_save = lambda: None
        try:
            for tid, label in (("aaaa01", "orzo"), ("aaaa02", "garlic bread"), ("aaaa03", "timer"), ("aaaa04", "rice")):
                gw.TIMERS[tid] = {"label": label, "due": 1, "mono_due": 1, "duration": 60,
                                  "ringing": tid != "aaaa04"}
            gw.timer_event({"type": "timer_cancel", "ids": ["aaaa01", "aaaa02", "aaaa03"]})
        finally:
            timers_saved, post, save = saved
            gw.TIMERS.clear()
            gw.TIMERS.update(timers_saved)
            gw.post_timers = post
            gw.timers_save = save
        self.assertEqual(len(posts), 1)
        self.assertEqual([r["id"] for r in posts[0]], ["aaaa04"])


class Rev4Dish(unittest.TestCase):
    def test_dish_is_optional_title_case_and_names_the_timer(self):
        plain = cards.validate_card(dict(RECIPE))
        self.assertNotIn("dish", plain)
        named = cards.validate_card(dict(RECIPE, dish="Orzo"))
        self.assertEqual(named["dish"], "Orzo")
        self.assertEqual(cards.validate_card(dict(RECIPE, dish="Garlic Bread"))["dish"], "Garlic Bread")
        for bad in ("orzo", "ORZO", "Garlic bread", "x" * 17, ""):
            with self.assertRaises(cards.CardError):
                cards.validate_card(dict(RECIPE, dish=bad))
        hit = cards.step_time("Boil the orzo for 10 minutes, then rest 5 minutes.")
        self.assertEqual(hit["seconds"], 600)
        self.assertEqual(hit["minutes"], 10)
        self.assertEqual(cards.step_time("Rest 20 min.")["seconds"], 1200)
        self.assertEqual(cards.step_time("Bake 1 hour.")["seconds"], 3600)
        self.assertEqual(cards.step_time("Simmer twenty minutes.")["minutes"], 20)
        self.assertIsNone(cards.step_time("Fold in the herbs."))
        self.assertEqual(cards.build_timer_offer("Boil for 10 minutes.", "Orzo", 3)["say"],
                         "Want a 10-minute timer for the orzo?")
        self.assertEqual(cards.build_timer_offer("Boil for 10 minutes.", None, 3)["say"],
                         "Want a 10-minute timer for this step?")
        self.assertEqual(cards.build_timer_offer("Bake 1 hour.", None, 2)["say"],
                         "Want a 60-minute timer for this step?")
        self.assertEqual(cards.build_timer_offer("Boil for 10 minutes.", "Orzo", 3)["label"], "Orzo")
        self.assertEqual(cards.build_timer_offer("Boil for 10 minutes.", None, 3)["label"], "Step 3")
        self.assertEqual(cards.timer_set_say("Orzo", 10), "Orzo timer set for 10 minutes.")
        for word in ("yes", "Yeah.", "yep", "sure", "okay", "OK", "please", "do it", "set it",
                     "hey juniper, yes please"):
            self.assertEqual(cards.offer_answer(word), "yes", word)
        for word in ("no", "nope", "no thanks", "not now"):
            self.assertEqual(cards.offer_answer(word), "no", word)
        self.assertIsNone(cards.offer_answer("next step"))
        self.assertIsNone(cards.offer_answer("yes please set a timer for the soup"))

    def test_source_round_trips_and_reads_from_step(self):
        now = 1000.0
        timers = {}
        out, msg = T.set_timer({"seconds": 600, "label": "Orzo", "source": dict(SRC)}, timers, now)
        self.assertTrue(out["ok"])
        self.assertEqual(msg["source"], SRC)
        self.assertEqual(msg["label"], "Orzo")
        self.assertEqual(timers[msg["id"]]["source"], SRC)
        path = os.path.join(tempfile.mkdtemp(), "timers.json")
        self.assertTrue(T.save_timer_state(path, timers, now, wall_now=1_700_000_000))
        loaded = T.load_timer_state(path, 1_700_000_000)
        self.assertEqual(loaded[msg["id"]]["source"], SRC)
        restored = T.restore_timers(path, now, 1_700_000_000)
        handed = T.timer_restore_messages(restored, now)
        self.assertEqual(handed[0]["source"], SRC)
        voice, voice_msg = T.set_timer({"seconds": 300, "label": "pasta"}, {}, now)
        self.assertTrue(voice["ok"])
        self.assertNotIn("source", voice_msg)
        chip = _timer("bbbb01", "Orzo", 600, 600, source=dict(SRC))
        rail = gs.recipe_rail([chip], EPOCH, ["bbbb01"])
        self.assertEqual(rail["slots"][0]["title"], "Orzo")
        self.assertEqual(rail["slots"][0]["source"], "From step 3")
        step = _timer("bbbb02", "Step 2", 100, 120, source={"recipe_id": "orzo01", "step": 2})
        self.assertEqual(gs.recipe_rail([step], EPOCH)["slots"][0]["source"], "From step 2")
        self.assertEqual(gs.recipe_rail([step], EPOCH)["slots"][0]["title"], "Step 2")
        self.assertEqual(gs.recipe_rail([_timer("bbbb03", "pasta", 50, 60)], EPOCH)["slots"][0]["source"],
                         "Set by voice")
        cleaned = gs.clean_timers([{"id": "bbbb01", "label": "Orzo", "ends_at_epoch": EPOCH + 10,
                                    "ringing": False, "source": dict(SRC), "nope": 1}])
        self.assertEqual(cleaned[0]["source"], SRC)
        saved = (dict(gw.TIMERS), gw.post_timers, gw.TIMERS_FILE)
        wake_path = os.path.join(tempfile.mkdtemp(), "wake-timers.json")
        gw.TIMERS.clear()
        gw.TIMERS_FILE = wake_path
        gw.post_timers = lambda: None
        try:
            gw.timer_event({"type": "timer_set", "id": "bbbb01", "seconds": 600, "label": "Orzo", "source": dict(SRC)})
            self.assertEqual(gw.TIMERS["bbbb01"]["source"], SRC)
            self.assertEqual(gw.timer_rows()[0]["source"], SRC)
            parsed = T.parse_timers(urllib.parse.unquote(gw.timers_query()), now)
            self.assertEqual(parsed["bbbb01"]["source"], SRC)
            gw.timers_save()
            with open(wake_path) as f:
                disk = json.load(f)
            self.assertEqual(disk["bbbb01"]["source"], SRC)
        finally:
            book, post, old_file = saved
            gw.TIMERS.clear()
            gw.TIMERS.update(book)
            gw.post_timers = post
            gw.TIMERS_FILE = old_file


class Rev4Highlight(unittest.TestCase):
    def setUp(self):
        self.old_chips = list(gs.TIMER_CHIPS)
        self.old_epoch = gs.TIMER_EPOCH
        self.old_shown = getattr(gs, "RECIPE_SHOWN", None)
        self.old_open = gs.RECIPE_FEED_OPEN
        gs.TIMER_EPOCH = EPOCH
        gs.TIMER_CHIPS[:] = []
        gs.RECIPE_SHOWN = {"id": "orzo01", "step": 1}

    def tearDown(self):
        gs.TIMER_CHIPS[:] = self.old_chips
        gs.TIMER_EPOCH = self.old_epoch
        gs.RECIPE_SHOWN = self.old_shown
        gs.RECIPE_FEED_OPEN = self.old_open
        if hasattr(gs, "RECIPE_CAPTION"):
            gs.RECIPE_CAPTION = None

    def _card(self):
        return dict(RECIPE, step=1, steps=["Boil the orzo for 10 minutes.", "Fold in the herbs.", "Serve warm."])

    def _colors(self, card=None):
        card = card or self._card()
        surf = pygame.Surface(PANEL)
        gs.draw_feed(surf, gs.Layout(*PANEL), NOW, card)
        box = gs.LAST_RECIPE_FRAME["step_text"]
        L = gs.Layout(*PANEL)
        face = gs.font("display", gs.recipe_px(L, 112))
        text = card["steps"][0]
        hit = cards.step_time(text)
        sx = gs.LAST_RECIPE_FRAME["step"].left + gs.recipe_px(L, 64)
        gap = gs.recipe_px(L, 8)
        spans = gs._step_spans(face, text, box.w)
        ink = dots = 0
        right = box.left
        ty = box.top
        for line, a, b in spans:
            y_ink = ty + face.get_ascent() // 2
            if hit and line == text[a:b]:
                lo, hi = max(hit["start"], a), min(hit["end"], b)
                if lo < hi:
                    frag_at = lo - a
                    frag = line[frag_at:frag_at + (hi - lo)]
                    ox = face.size(line[:frag_at])[0]
                    fw = face.size(frag)[0]
                    y_line = ty + face.get_height() - 3
                    for x in range(sx + ox + 1, sx + ox + fw - 1, 2):
                        if not box.collidepoint(x, y_ink):
                            continue
                        c = surf.get_at((x, y_ink))
                        if c.g > 160 and c.g > c.r + 20:
                            ink += 1
                        d = surf.get_at((x, min(y_line, box.bottom - 1)))
                        if d.g > 160 and d.g > d.r + 20:
                            dots += 1
            for x in range(sx, min(sx + face.size(line)[0] + 1, box.right)):
                c = surf.get_at((x, y_ink))
                if c.r > 40 or c.g > 40:
                    right = max(right, x)
            ty += face.get_height() + gap
        return ink, dots, right, face.size(text)[0]

    def test_time_is_teal_until_that_steps_timer_exists(self):
        ink, dots, right, width = self._colors()
        self.assertGreater(ink, 8)
        self.assertGreater(dots, 4)  # dotted, not a solid bar: gaps keep this modest but present
        self.assertLess(right, gs.LAST_RECIPE_FRAME["step_text"].left + width + gs.LAST_RECIPE_FRAME["step"].left)
        gs.TIMER_CHIPS[:] = [_timer("bbbb01", "pasta", 400, 600, source={"recipe_id": "other", "step": 1})]
        other_ink, _, other_right, _ = self._colors()
        self.assertGreater(other_ink, 8)  # a different recipe does not mute
        self.assertEqual(other_right, right)
        gs.TIMER_CHIPS[:] = [_timer("bbbb01", "Orzo", 400, 600, source={"recipe_id": "orzo01", "step": 1})]
        muted_ink, _, muted_right, _ = self._colors()
        self.assertEqual(muted_ink, 0)
        self.assertEqual(muted_right, right)
        gs.TIMER_CHIPS[:] = [_timer("bbbb01", "Orzo", -5, 600, ringing=True,
                                    source={"recipe_id": "orzo01", "step": 1})]
        ring_ink, _, _, _ = self._colors()
        self.assertEqual(ring_ink, 0)
        gs.TIMER_CHIPS[:] = []
        again, _, _, _ = self._colors()
        self.assertGreater(again, 8)
        gs.RECIPE_FEED_OPEN = True
        gs.RECIPE_CAPTION = {"text": "Want a 10-minute timer for the orzo?", "until": 10**12}
        gs.arm_recipe_screen(False)
        self.assertIsNone(gs.RECIPE_SHOWN)
        self.assertIsNone(gs.RECIPE_CAPTION)

    def test_offer_caption_replaces_the_hint_until_it_clears(self):
        gs.apply_recipe_caption("Want a 10-minute timer for the orzo?", 8, now=time.monotonic())
        calls = []
        real = gs.draw_voice_hint

        def wrap(surface, segments, x, y, px=36, max_w=None):
            calls.append(list(segments))
            return real(surface, segments, x, y, px=px, max_w=max_w)

        seen = []
        real_font = gs.font

        def font(kind, px):
            inner = real_font(kind, px)

            class Recording:
                def render(self, text, *a, **k):
                    seen.append(str(text))
                    return inner.render(text, *a, **k)

                def __getattr__(self, name):
                    return getattr(inner, name)

            return Recording()

        gs.draw_voice_hint = wrap
        gs.font = font
        try:
            surf = pygame.Surface(PANEL)
            gs.draw_feed(surf, gs.Layout(*PANEL), NOW, dict(RECIPE))
            self.assertIn("Want a 10-minute timer for the orzo?", seen)
            self.assertEqual(gs.LAST_RECIPE_FRAME["hint"], pygame.Rect(128, 1494, 2304, 84))
            gs.apply_recipe_caption("", 0)
            gs.draw_feed(surf, gs.Layout(*PANEL), NOW, dict(RECIPE))
        finally:
            gs.draw_voice_hint = real
            gs.font = real_font
        self.assertEqual(calls[-1], list(gs.RECIPE_HINT_SEGMENTS))


class Rev4Offer(unittest.IsolatedAsyncioTestCase):
    async def _turns(self, lines, recipe_for, *, silence=False):
        from aiohttp import web, WSMsgType
        from aiohttp.test_utils import TestClient, TestServer
        relay = _ensure_relay()
        relay.stats["busy"] = 0
        posted = []
        pending = list(lines)
        real_post = relay.post_recipe_cmd
        real_tts = relay.tts_pcm

        async def no_tts(*args, **kwargs):
            return None

        def post(cmd):
            posted.append(cmd)
            return recipe_for(cmd, len(posted))

        relay.post_recipe_cmd = post
        relay.tts_pcm = no_tts
        os.environ["GROK_TIMER_STATE"] = os.path.join(tempfile.mkdtemp(), "timers.json")
        xapp = web.Application()

        async def xai(req):
            s = web.WebSocketResponse()
            await s.prepare(req)
            armed = True
            async for m in s:
                if m.type != WSMsgType.TEXT:
                    continue
                ev = json.loads(m.data)
                if ev.get("type") == "session.update":
                    await s.send_json({"type": "session.updated"})
                elif ev.get("type") == "input_audio_buffer.clear":
                    armed = True
                elif ev.get("type") == "input_audio_buffer.append" and armed and pending:
                    armed = False
                    heard = pending.pop(0)
                    await s.send_json({"type": "input_audio_buffer.speech_started"})
                    await s.send_json({"type": "input_audio_buffer.speech_stopped"})
                    await s.send_json({"type": "response.created"})
                    await s.send_json({"type": "conversation.item.input_audio_transcription.completed",
                                       "transcript": heard})
                    await s.send_json({"type": "response.done"})
            return s

        xapp.router.add_get("/rt", xai)
        xs = TestServer(xapp)
        client = ws = mic = None
        events = []
        try:
            await xs.start_server()
            relay.URL = str(xs.make_url("/rt")).replace("http", "ws")
            rapp = web.Application()
            rapp.router.add_get("/ws", relay.session)
            client = TestClient(TestServer(rapp))
            await client.start_server()
            ws = await client.ws_connect("/ws", headers={"Authorization": "Bearer test-token"},
                                         params={"timers": "[]"})

            async def pump():
                while not ws.closed:
                    await ws.send_bytes(b"\0\0" * 1920)
                    await asyncio.sleep(0.05)

            mic = asyncio.create_task(pump())
            turn_done = 0
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                ev = json.loads(msg.data)
                events.append(ev)
                if ev.get("type") == "turn_done":
                    turn_done += 1
                    if silence or turn_done >= len(lines):
                        if silence:
                            await ws.send_json({"type": "listening"})
                        else:
                            break
                    else:
                        await ws.send_json({"type": "listening"})
                elif ev.get("type") in ("done", "timeout", "error"):
                    break
        finally:
            relay.post_recipe_cmd = real_post
            relay.tts_pcm = real_tts
            if mic:
                mic.cancel()
            if ws is not None and not ws.closed:
                await ws.close()
            if client is not None:
                await client.close()
            await xs.close()
            relay.stats["busy"] = 0
        return events, posted

    def _step(self, cmd, n):
        if cmd == "repeat":
            return {"ok": True, "say": "Boil the orzo for 10 minutes.", "closed": False,
                    "bot": "juniper", "id": "orzo01", "step": 1, "dish": "Orzo"}
        if n == 1:
            return {"ok": True, "say": "Boil the orzo for 10 minutes.", "closed": False,
                    "bot": "juniper", "id": "orzo01", "step": 1, "dish": "Orzo"}
        return {"ok": True, "say": "Fold in the herbs.", "closed": False,
                "bot": "juniper", "id": "orzo01", "step": 2, "dish": "Orzo"}

    def _replies(self, events):
        return [e.get("text") for e in events if e.get("type") == "reply"]

    async def test_yes_sets_the_timer_before_the_model(self):
        events, posted = await self._turns(["next step", "yes"], self._step)
        replies = self._replies(events)
        self.assertIn("Want a 10-minute timer for the orzo?", replies)
        self.assertIn("Orzo timer set for 10 minutes.", replies)
        sets = [e for e in events if e.get("type") == "timer_set"]
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0]["label"], "Orzo")
        self.assertEqual(sets[0]["seconds"], 600)
        self.assertEqual(sets[0]["source"], {"recipe_id": "orzo01", "step": 1})
        self.assertFalse(any(e.get("type") == "heard" for e in events))
        self.assertEqual(posted, ["next"])

    async def test_no_says_okay_and_repeat_offers_again_until_a_timer_exists(self):
        events, posted = await self._turns(["next step", "no thanks", "repeat that step"], self._step)
        replies = self._replies(events)
        self.assertEqual(replies.count("Want a 10-minute timer for the orzo?"), 2)
        self.assertIn("Okay.", replies)
        self.assertFalse(any(e.get("type") == "timer_set" for e in events))
        self.assertEqual(posted, ["next", "repeat"])
        events, posted = await self._turns(["next step", "yes", "repeat"], self._step)
        replies = self._replies(events)
        self.assertEqual(replies.count("Want a 10-minute timer for the orzo?"), 1)
        self.assertIn("Orzo timer set for 10 minutes.", replies)

    async def test_another_command_cancels_the_offer_and_silence_says_nothing(self):
        events, posted = await self._turns(["next step", "next step"], self._step)
        replies = self._replies(events)
        self.assertIn("Want a 10-minute timer for the orzo?", replies)
        self.assertIn("Fold in the herbs.", replies)
        self.assertNotIn("Okay.", replies)
        self.assertEqual(posted, ["next", "next"])
        self.assertFalse(any(e.get("type") == "timer_set" for e in events))
        events, _posted = await self._turns(["next step"], self._step, silence=True)
        replies = self._replies(events)
        self.assertIn("Want a 10-minute timer for the orzo?", replies)
        self.assertNotIn("Okay.", replies)
        captions = [e for e in events if e.get("type") == "recipe_caption"]
        self.assertTrue(any(e.get("text") == "Want a 10-minute timer for the orzo?" for e in captions))
        self.assertTrue(any(e.get("text") == "" for e in captions))

    async def test_without_a_dish_the_offer_names_this_step(self):
        def recipe_for(cmd, n):
            return {"ok": True, "say": "Simmer twenty minutes.", "closed": False,
                    "bot": "juniper", "id": "soup01", "step": 2}

        events, _posted = await self._turns(["next step", "sure"], recipe_for)
        replies = self._replies(events)
        self.assertIn("Want a 20-minute timer for this step?", replies)
        self.assertIn("Step 2 timer set for 20 minutes.", replies)
        sets = [e for e in events if e.get("type") == "timer_set"]
        self.assertEqual(sets[0]["label"], "Step 2")
        self.assertEqual(sets[0]["source"], {"recipe_id": "soup01", "step": 2})
        self.assertEqual(sets[0]["seconds"], 1200)


if __name__ == "__main__":
    unittest.main()
