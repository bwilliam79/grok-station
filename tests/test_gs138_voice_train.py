#!/usr/bin/env python3
"""Voice-started wake-word training (gs138).

"Hey Grok, train my wake word for <bot>" used to open the prompt tile and wait
for a Start tap. It now goes straight to get-ready. A tap on Record voice and a
voice start both count 3, 2, 1 once. Two silent clips stop the session. Audio
from a recording window is not a command.
"""
import inspect
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wake"))
import grok_wake as gw  # noqa: E402

JUNIPER = {"slug": "juniper", "name": "Juniper", "phrase": "hey_juniper", "spoken": "Juniper"}


class VoiceStart(unittest.TestCase):
    def test_voice_start_auto_begins(self):
        plan = gw.voice_train_plan(JUNIPER, has_model=False)
        self.assertTrue(plan["begin"])
        self.assertEqual(plan["phase"], "ready")
        self.assertNotEqual(plan["phase"], "prompt")
        self.assertEqual(plan["say"], "Okay. I'll record you 5 times. Say Juniper after each chime. To stop, stay quiet.")
        self.assertEqual(plan["enroll"]["phrase"], "hey_juniper")
        self.assertEqual(plan["enroll"]["persona"], "juniper")
        self.assertIs(plan["enroll"]["voice"], True)
        self.assertIs(plan["enroll"]["topup"], False)
        self.assertFalse(plan["tile"])
        more = gw.voice_train_plan(JUNIPER, has_model=True)
        self.assertTrue(more["begin"])
        self.assertEqual(more["say"], "Okay. I'll add 5 more. Say Juniper after each chime. To stop, stay quiet.")
        self.assertIs(more["enroll"]["topup"], True)
        missing = gw.voice_train_plan(
            {"slug": "", "name": "", "phrase": None, "spoken": "Nova"}, has_model=False)
        self.assertFalse(missing["begin"])
        self.assertIsNone(missing["enroll"])
        self.assertEqual(missing["say"], "I don't have a bot called Nova.")
        # The relay speaks that plan and queues recording. It does not stop at the prompt tile.
        src = open(os.path.join(os.path.dirname(gw.__file__), "..", "relay", "relay.py"), encoding="utf-8").read()
        i_train = src.find("train = train_wake_request(heard, connect_bots(roster, list_source_slug()))")
        block = src[i_train:src.find('return "user ended"', i_train)]
        self.assertIn("voice_train_plan(", block)
        self.assertIn("play_fixed(plan", block)
        self.assertIn("post_wake_enroll(", block)
        self.assertNotIn("open_train_tile(train)", block)

    def test_tap_and_voice_both_count_down(self):
        for start in ("voice", "tap"):
            steps = gw.train_steps(start, n=5)
            counts = [s for s in steps if s["kind"] == "count"]
            self.assertEqual([s["n"] for s in counts], [3, 2, 1], start)
            self.assertTrue(all(s["caption"] == "Recording in 3…" for s in counts), start)
            self.assertTrue(all(s["announce"] is None for s in counts), start)
            listens = [s for s in steps if s["kind"] == "listen"]
            saved = [s for s in steps if s["kind"] == "saved"]
            self.assertEqual(len(listens), 5, start)
            self.assertEqual(listens[0]["line"], "Listening… 1 of 5", start)
            self.assertEqual(saved[0]["line"], "1 of 5 saved", start)
            self.assertEqual(listens[0]["announce"], "Listening… 1 of 5", start)
            self.assertEqual(saved[0]["announce"], "1 of 5 saved", start)
            between = steps[steps.index(listens[0]) + 1:steps.index(listens[1])]
            self.assertFalse(any(s["kind"] == "count" for s in between), start)
            self.assertEqual(between[0]["kind"], "saved", start)
            self.assertEqual(between[0].get("cue"), "Next", start)
            self.assertEqual(between[0]["gap_s"], gw.RETRY_S, start)

    def test_silent_clip_counts_down_then_stops(self):
        # 0.13.9 (gs-voice-train rev1.6): one silent clip starts the 3 s countdown; silence through it stops.
        self.assertEqual(gw.silent_clip(1, "Juniper"), {"action": "countdown", "seconds": 3})
        second = gw.silent_clip(2, "Juniper")
        self.assertEqual(second["action"], "stop")
        self.assertEqual(second["say"], "Stopped. Nothing was saved.")
        self.assertEqual(second["restart"], "Say 'Hey Grok, train my wake word for Juniper' to try again.")
        self.assertEqual(gw.silent_clip(2, "Juniper", topup=True)["say"], "Stopped. Nothing new was saved.")

    def test_enroll_never_scores_and_the_relay_ends(self):
        """A recording window is not a command because enroll owns the mic.

        scores() and Detector.feed never run during enroll. The relay speaks the
        plan, posts the enrollment, and ends the session. It does not keep the turn
        open for the model. command_during is not a gate: nothing calls it.
        """
        flow = open(os.path.join(os.path.dirname(gw.train_flow.__file__), "train_flow.py"), encoding="utf-8").read()
        enroll_src = inspect.getsource(gw.enroll)
        self.assertNotIn("def command_during", flow)
        self.assertNotIn("command_during(", enroll_src)
        self.assertNotIn("command_during(", inspect.getsource(gw.main))
        self.assertFalse(hasattr(gw, "command_during"))

        fed = []
        old = (gw.scores, gw.Detector.feed, gw.post_state, gw.subprocess.Popen, gw.ENROLL_DIR,
               gw.time.sleep, gw.ENROLL_STATE.get("last"), gw.ENROLL_STATE.get("busy"))
        gw.scores = lambda *a, **k: fed.append("scores") or {}
        gw.Detector.feed = lambda self, sc: fed.append("feed")
        gw.post_state = lambda *a, **k: None
        gw.subprocess.Popen = lambda *a, **k: None
        gw.ENROLL_DIR = tempfile.mkdtemp()
        gw.time.sleep = lambda s: None
        gw.ENROLL_STATE["busy"] = False
        gw.ENROLL_STATE["last"] = None

        def read16(raw=False):
            return gw.np.zeros(1280, gw.np.int16)

        try:
            gw.enroll({"phrase": "hey_juniper", "persona": "juniper", "count": 5, "voice": True}, read16, "default")
        finally:
            (gw.scores, gw.Detector.feed, gw.post_state, gw.subprocess.Popen, gw.ENROLL_DIR,
             gw.time.sleep) = old[:6]
            gw.ENROLL_STATE["last"] = old[6]
            gw.ENROLL_STATE["busy"] = old[7]
        self.assertEqual(fed, [])

        handle = inspect.getsource(gw.main)
        enroll_at = handle.find("enroll(")
        back = handle.find("return None", enroll_at)
        self.assertLess(enroll_at, back)
        self.assertLess(back, handle.find("sc = scores(", back))
        self.assertLess(back, handle.find("det.feed(", back))

        relay_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "relay")
        sys.path.insert(0, relay_dir)
        os.environ.setdefault("RELAY_TOKEN", "test-token")
        os.environ.setdefault("XAI_API_KEY", "test-key")
        already = "relay" in sys.modules
        import relay
        if not already:
            self.addCleanup(sys.modules.pop, "relay", None)
        plan = gw.voice_train_plan(JUNIPER, has_model=False)
        result = relay.train_session_result(plan)
        self.assertEqual(result["end"], "user ended")
        self.assertEqual(result["done"], {"type": "done", "local": True})
        self.assertEqual(result["enroll"]["phrase"], "hey_juniper")
        self.assertEqual(result["enroll"]["voice"], True)
        src = open(relay.__file__, encoding="utf-8").read()
        i_train = src.find("train = train_wake_request(heard, connect_bots(roster, list_source_slug()))")
        block = src[i_train:src.find("taken = recipe_taken(", i_train)]
        self.assertIn("train_session_result(", block)
        self.assertLess(block.find("post_wake_enroll("), block.find('return "user ended"'))
        self.assertLess(block.find("train_session_result("), block.find("post_wake_enroll("))
        self.assertNotIn("conversation.item", block)


if __name__ == "__main__":
    unittest.main()
