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
        self.assertEqual(plan["say"], "Okay. I'll record you 5 times. Say Juniper after each chime.")
        self.assertEqual(plan["enroll"]["phrase"], "hey_juniper")
        self.assertEqual(plan["enroll"]["persona"], "juniper")
        self.assertIs(plan["enroll"]["voice"], True)
        self.assertIs(plan["enroll"]["topup"], False)
        self.assertFalse(plan["tile"])
        more = gw.voice_train_plan(JUNIPER, has_model=True)
        self.assertTrue(more["begin"])
        self.assertEqual(more["say"], "Okay. I'll add 5 more. Say Juniper after each chime.")
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

    def test_two_silent_clips_stop(self):
        first = gw.silent_clip(1, "Juniper")
        self.assertEqual(first["action"], "retry")
        self.assertEqual(first["say"], "I didn't hear it. Again after the chime.")
        second = gw.silent_clip(2, "Juniper")
        self.assertEqual(second["action"], "stop")
        self.assertEqual(
            second["say"],
            "Stopped. Say 'Hey Grok, train my wake word for Juniper' to try again.")
        self.assertEqual(gw.silent_clip(1, "Juniper")["action"], "retry")

    def test_recording_window_audio_is_not_a_command(self):
        said = "Hey Grok, train my wake word for Juniper"
        self.assertIsNone(gw.command_during("recording", said))
        self.assertIsNone(gw.command_during("recording", "Hey Grok, stop"))
        # The 1.5 s gap is still inside enroll's capture read, so it is not a command either.
        self.assertIsNone(gw.command_during("gap", "Hey Grok, stop"))
        self.assertIsNone(gw.command_during("count", "stop"))
        src = inspect.getsource(gw.enroll)
        self.assertIn('command_during("recording"', src)
        handle = inspect.getsource(gw.main)
        self.assertLess(handle.find("enroll("), handle.find("scores("))
        self.assertNotIn("train_wake_request", src)


if __name__ == "__main__":
    unittest.main()
