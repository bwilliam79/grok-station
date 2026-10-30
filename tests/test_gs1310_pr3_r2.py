"""0.13.10: PR 3 re-review r2 fixes (R2-3, R2-4, R2-5, R2-6, R2-7, #12). Sample dishes are invented."""
import os
import sys
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("app", "relay", "wake"):
    sys.path.insert(0, os.path.join(ROOT, sub))
import cards  # noqa: E402
import grok_station as gs  # noqa: E402
import grok_wake as gw  # noqa: E402
import tempfile  # noqa: E402
# Same relay env as tests/test_gs1311_recipe_tile.py::_ensure_relay, so import order doesn't matter.
os.environ.setdefault("RELAY_TOKEN", "test-token")
os.environ.setdefault("XAI_API_KEY", "test-key")
os.environ.setdefault("NO_SPEECH", "1.0")
os.environ.setdefault("ALERT_DIR", os.path.join(tempfile.gettempdir(), "gs1311-alerts"))
os.environ.setdefault("PERSONAS_FILE", os.path.join(tempfile.gettempdir(), "gs1311-personas.json"))
import relay  # noqa: E402
import pygame  # noqa: E402

RECIPE = {
    "v": 1, "type": "recipe", "title": "Lemon herb orzo", "servings": 4,
    "ingredients": ["orzo", "lemon", "garlic"],
    "steps": ["Boil the orzo for 10 minutes.", "Fold in the herbs.", "Serve warm."],
    "step": 1,
}
STEP1 = {"ok": True, "say": "Boil the orzo for 10 minutes.", "closed": False,
         "bot": "juniper", "id": "orzo01", "step": 1, "dish": "Orzo"}


class R2_3_OfferOncePerRecipe(unittest.TestCase):
    def setUp(self):
        relay.reset_offered_steps()

    def test_a_new_voice_session_does_not_offer_the_same_step_again(self):
        self.assertIsNotNone(relay.plan_step_offer({}, "next", dict(STEP1), {}))
        self.assertIsNone(relay.plan_step_offer({}, "next", dict(STEP1), {}))

    def test_repeat_still_offers_and_closing_the_recipe_resets(self):
        relay.plan_step_offer({}, "next", dict(STEP1), {})
        self.assertIsNotNone(relay.plan_step_offer({}, "repeat", dict(STEP1), {}))
        relay.remember_recipe({}, {"ok": True, "closed": True, "id": "orzo01"})
        self.assertIsNotNone(relay.plan_step_offer({}, "next", dict(STEP1), {}))


class R2_4_YesAtEightTimers(unittest.TestCase):
    def test_a_refused_set_is_spoken(self):
        self.assertEqual(relay.offer_missed_say({"ok": False, "error": "Eight timers are already running."}),
                         "Eight timers are already running.")
        self.assertIsNone(relay.offer_missed_say({"ok": False}))


class R2_5_BadDishKeepsTheCard(unittest.TestCase):
    def test_a_malformed_dish_is_dropped_not_the_recipe(self):
        for bad in ("orzo", "McRib", "x" * 17, "", 5):
            out = cards.validate_card(dict(RECIPE, dish=bad))
            self.assertEqual(out["type"], "recipe")
            self.assertNotIn("dish", out)
        self.assertEqual(cards.validate_card(dict(RECIPE, dish="Orzo"))["dish"], "Orzo")


class R2_6_DeadCode(unittest.TestCase):
    def test_unused_helpers_are_gone(self):
        self.assertFalse(hasattr(gs, "fitted_step_lines"))
        self.assertFalse(hasattr(gw, "alarm_repeat_due"))


class F12_PainterDoesNotMutateTheRail(unittest.TestCase):
    def test_empty_slot_paint_leaves_the_slot_dict_alone(self):
        pygame.display.init()
        pygame.font.init()
        slot = {"kind": "empty"}
        gs.LAST_RECIPE_COPY = []
        gs._paint_recipe_slot(pygame.Surface((800, 400)), gs.Layout(2560, 1706),
                              pygame.Rect(0, 0, 700, 300), slot, 0, 0, True)
        self.assertEqual(slot, {"kind": "empty"})
        self.assertIn("No timers", gs.LAST_RECIPE_COPY)


class R2_7_NotBeforeTheWindow(unittest.TestCase):
    def _st(self):
        return {"recipe_offer": {"say": "x", "label": "Orzo", "seconds": 600, "minutes": 10,
                                 "speak_at": 100.0, "playback_s": 2.0, "pending": True}}

    def test_an_early_yes_or_noise_is_held_while_the_offer_plays(self):
        for heard in ("yes", "", "mm"):
            st = self._st()
            self.assertEqual(relay.classify_offer_reply(heard, st, 100.5), "hold")
            self.assertIn("recipe_offer", st)

    def test_a_real_command_while_pending_still_cancels(self):
        st = self._st()
        self.assertEqual(relay.classify_offer_reply("next step", st, 100.5), "cancel")
        self.assertNotIn("recipe_offer", st)

    def test_after_the_window_opens_yes_is_yes(self):
        st = self._st()
        relay.arm_offer_window(st, 103.0)
        self.assertEqual(relay.classify_offer_reply("yes", st, 104.0), "yes")


if __name__ == "__main__":
    unittest.main()
