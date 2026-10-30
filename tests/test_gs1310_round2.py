"""0.13.10 round 2: R2-8, R2-9, R2-10, R2-2, dish casing, updates pause, band label and card height."""
import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("app", "relay", "wake", "tests"):
    sys.path.insert(0, os.path.join(ROOT, sub))
os.environ.setdefault("RELAY_TOKEN", "test-token")
os.environ.setdefault("XAI_API_KEY", "test-key")
os.environ.setdefault("NO_SPEECH", "1.0")
os.environ.setdefault("ALERT_DIR", os.path.join(tempfile.gettempdir(), "gs1311-alerts"))
os.environ.setdefault("PERSONAS_FILE", os.path.join(tempfile.gettempdir(), "gs1311-personas.json"))
import cards  # noqa: E402
import grok_station as gs  # noqa: E402
import grok_wake as gw  # noqa: E402
import relay  # noqa: E402
import tools as T  # noqa: E402
import pygame  # noqa: E402

EPOCH = 1_700_000_000
RECIPE = {"v": 1, "type": "recipe", "title": "Lemon herb orzo", "servings": 4,
          "ingredients": ["orzo", "lemon"], "steps": ["Boil 10 minutes.", "Fold.", "Serve."], "step": 1}


def setUpModule():
    pygame.display.init()
    pygame.font.init()


class R2_8_SpokenDurations(unittest.TestCase):
    def test_whole_hours_are_said_as_hours(self):
        self.assertEqual(cards.build_timer_offer("Bake 1 hour.", "Bread", 2)["say"],
                         "Want a 1-hour timer for the bread?")
        self.assertEqual(cards.timer_set_say("Bread", 60), "Bread timer set for 1 hour.")
        self.assertEqual(cards.timer_set_say("Bread", 120), "Bread timer set for 2 hours.")
        self.assertEqual(cards.timer_set_say("Bread", 90), "Bread timer set for 90 minutes.")

    def test_a_range_offers_the_lower_bound(self):
        for text in ("Simmer 10-12 minutes.", "Simmer 10 to 12 minutes.", "Simmer 10\u201312 minutes."):
            self.assertEqual(cards.step_time(text)["minutes"], 10, text)


class R2_9_NoRepeatOnTheFirstRingTick(unittest.TestCase):
    def test_repeat_tone_waits_a_tick_after_a_first_ring(self):
        saved = (dict(gw.TIMERS), gw._ring_one, gw._tone_repeats, dict(gw.LIVE), gw._expire_rings)
        calls = []
        try:
            gw.TIMERS.clear()
            gw.TIMERS["aaaa01"] = {"label": "orzo", "due": time.time() - 1, "mono_due": time.monotonic() - 1,
                                   "ringing": False}
            gw._ring_one = lambda k, v: calls.append("ring")
            gw._tone_repeats = lambda now_m: calls.append("repeat")
            gw._expire_rings = lambda now: None
            gw.LIVE["session"], gw.LIVE["play"] = None, "dev"
            gw.timers_tick()
            self.assertEqual(calls, ["ring"])
            gw.TIMERS.clear()
            calls.clear()
            gw.timers_tick()
            self.assertEqual(calls, ["repeat"])
        finally:
            gw.TIMERS.clear()
            gw.TIMERS.update(saved[0])
            gw._ring_one, gw._tone_repeats = saved[1], saved[2]
            gw.LIVE.clear()
            gw.LIVE.update(saved[3])
            gw._expire_rings = saved[4]


# Noor rules the exact-3-ringing overlap later. Flip this one constant to re-point the R2-10 tests.
THREE_RINGING_SLOT3 = "ringing"   # "ringing" = amber "+N more" (current); "plain" = the ordinary "+N more"


def _t(tid, label, left, ringing=False):
    return {"id": tid, "label": label, "ends_at_epoch": EPOCH + left, "ringing": ringing}


class R2_10_ThreeRinging(unittest.TestCase):
    TIMERS = [_t("aaaa01", "Orzo", -30, True), _t("aaaa02", "Garlic Bread", -20, True),
              _t("aaaa03", "Roast Carrots", -10, True), _t("aaaa04", "Rice", 300)]

    def test_slot3_treatment(self):
        rail = gs.recipe_rail(self.TIMERS, EPOCH)
        self.assertEqual(rail["slots"][2]["kind"], "more")
        self.assertEqual(rail["slots"][2].get("treatment", "plain"), THREE_RINGING_SLOT3)

    def test_each_ringing_timer_is_readable_and_has_its_own_44px_target(self):
        gs.TIMER_CHIPS[:] = [dict(t) for t in self.TIMERS]
        old = gs.TIMER_EPOCH
        gs.TIMER_EPOCH = EPOCH
        try:
            gs.draw_feed(pygame.Surface((2560, 1706)), gs.Layout(2560, 1706),
                         time.localtime(EPOCH), dict(RECIPE))
        finally:
            gs.TIMER_CHIPS[:] = []
            gs.TIMER_EPOCH = old
        got = {t["id"]: t for t in gs.LAST_RECIPE_TARGETS}
        for tid in ("aaaa01", "aaaa02", "aaaa03"):
            self.assertIn(tid, got)
            self.assertGreaterEqual(got[tid]["h"], 44, tid)
            self.assertTrue(got[tid]["fits"], tid)
            self.assertNotIn("\u2026", got[tid]["text"])

    def test_stop_stops_all_and_stop_name_stops_one(self):
        now = 1000.0
        book = {"aaaa01": {"label": "orzo", "due": now - 30, "duration": 60, "ringing": True},
                "aaaa02": {"label": "garlic bread", "due": now - 20, "duration": 60, "ringing": True},
                "aaaa03": {"label": "roast carrots", "due": now - 10, "duration": 60, "ringing": True}}
        one = dict((k, dict(v)) for k, v in book.items())
        out, msg = T.cancel_timer({"label": "stop"}, book, now)
        self.assertTrue(out["ok"])
        self.assertEqual(sorted(msg["ids"]), ["aaaa01", "aaaa02", "aaaa03"])
        self.assertEqual(book, {})
        out, msg = T.cancel_timer({"label": "garlic bread"}, one, now)
        self.assertEqual(msg, {"type": "timer_cancel", "id": "aaaa02"})
        self.assertEqual(sorted(one), ["aaaa01", "aaaa03"])


class R2_2_StepTimer(unittest.TestCase):
    ST = {"recipe": {"id": "orzo01", "step": 3, "dish": "Orzo"}}

    def test_unnamed_timer_is_the_steps_timer(self):
        args = relay.recipe_timer_args({"seconds": 600}, dict(self.ST))
        self.assertEqual(args["label"], "Step 3 \u00b7 10 min")
        self.assertEqual(args["source"], {"recipe_id": "orzo01", "step": 3})
        self.assertEqual(relay.step_timer_say(args), "Timer for step 3, 10 minutes.")
        out, msg = T.set_timer(args, {}, 1000.0)
        self.assertTrue(out["ok"])
        self.assertEqual(msg["label"], "Step 3 \u00b7 10 min")

    def test_named_timer_and_no_recipe_are_unchanged(self):
        self.assertEqual(relay.recipe_timer_args({"seconds": 300, "label": "tea"}, dict(self.ST)),
                         {"seconds": 300, "label": "tea"})
        self.assertEqual(relay.recipe_timer_args({"seconds": 300}, {}), {"seconds": 300})
        self.assertIsNone(relay.step_timer_say({"seconds": 300, "label": "tea"}))


class DishCasing(unittest.TestCase):
    def test_dish_is_kept_as_given_and_all_caps_or_lower_is_title_cased_once(self):
        v = lambda d: cards.validate_card(dict(RECIPE, dish=d)).get("dish")
        self.assertEqual(v("Garlic bread"), "Garlic bread")
        self.assertEqual(v("McRib"), "McRib")
        self.assertEqual(v("GARLIC BREAD"), "Garlic Bread")
        self.assertEqual(v("garlic bread"), "Garlic Bread")
        self.assertIsNone(v("x" * 17))

    def test_station_does_not_recase_it(self):
        self.assertEqual(gs.recipe_name("McRib"), "McRib")
        self.assertEqual(gs.recipe_name("Garlic bread"), "Garlic bread")


class UpdatesPause(unittest.TestCase):
    def test_voice_pause_and_resume(self):
        st = {}
        self.assertEqual(relay.updates_pause_reply("Hey Grok, pause updates", st, now=100.0)[0], "pause")
        self.assertEqual(relay.updates_pause_reply("resume updates", st, now=110.0)[0], "resume")
        self.assertIsNone(relay.updates_pause_reply("pause the music", st, now=120.0))


class BandRev2(unittest.TestCase):
    def test_counter_never_names_a_type(self):
        needs = [{"id": "n"}]
        others = [{"id": "o%d" % i} for i in range(3)]
        self.assertEqual(cards.update_marker(needs, others, 0), "Updates \u00b7 2\u20133 of 4")
        self.assertEqual(cards.update_marker([], others + [{"id": "x"}], 0), "Updates \u00b7 1\u20133 of 4")
        self.assertEqual(cards.update_marker(needs, others, 0, paused=True), "Paused \u00b7 Updates \u00b7 2\u20133 of 4")
        self.assertEqual(cards.update_marker([], others, 0), "")

    def test_cards_are_header_plus_two_lines_all_the_same_height(self):
        L = gs.Layout(2560, 1706)
        h = gs.update_tile_height(L)
        self.assertLess(h, L(280))
        lines = gs.update_title_lines(L, "word " * 40, 700)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("\u2026"))


class TalkingKeepsTheRowPaused(unittest.TestCase):
    def setUp(self):
        import test_gs1310_updates_feed as uf
        self.uf = uf
        self.old = gs.UPDATES
        gs.UPDATES = cards.UpdateFeed()
        gs.UPD_ROT.update(step=0, at=None)

    def tearDown(self):
        gs.UPDATES = self.old

    def test_row_stays_visible_and_paused_while_a_bot_talks(self):
        now = time.time()
        for i in range(5):
            gs.apply_update(dict(self.uf.upd("u%d" % i, "done", "Line %d" % i, now - i), bot="otto"), gs.UPDATES)
        zone = gs.zone_view({"voice": True, "state": "speaking", "from": "Juniper", "text": "Your list has 14 items."},
                            None)
        surf = pygame.Surface((2560, 1706))
        gs.draw_zone(surf, gs.Layout(2560, 1706), time.localtime(), gs.WX if hasattr(gs, "WX") else _Wx(), zone=zone)
        self.assertTrue(gs.LAST_UPDATE_BAND["visible"])
        self.assertTrue(gs.LAST_UPDATE_BAND["paused"])
        self.assertTrue(gs.LAST_UPDATE_BAND["marker"].startswith("Paused \u00b7 Updates"))
        self.assertEqual(gs.LAST_REPLY_LINE["who"], "Juniper")
        step = gs.UPD_ROT["step"]
        self.assertEqual(gs.update_rotation_step(now + 60, paused=True), step)

    def test_voice_pause_op_holds_for_10_minutes(self):
        f = cards.UpdateFeed()
        gs.apply_update({"v": 1, "op": "updates_pause", "on": True}, f, now=100.0)
        self.assertEqual(f.paused_until, 700.0)
        gs.apply_update({"v": 1, "op": "updates_pause", "on": False}, f, now=110.0)
        self.assertIsNone(f.paused_until)
        with self.assertRaises(cards.CardError):
            gs.apply_update({"v": 1, "op": "updates_pause", "on": True, "bot": "otto"}, f)


class _Wx:
    data = None

    def offline(self):
        return False


if __name__ == "__main__":
    unittest.main()
