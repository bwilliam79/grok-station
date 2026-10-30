#!/usr/bin/env python3
"""0.13.11 bq-0012: wake word setup by voice.

1. "Hey Grok, set up my wake word for <bot>" starts training like "train my wake word".
2. The spoken intro before training tells the owner to say "Hey <bot>", not the bare name.
3. Training started from the admin page shows the wake word exactly as it was typed.
Each test fails on 0.13.10 (79fd193).
"""
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ.update(RELAY_TOKEN="test-token", XAI_API_KEY="test-key", ALERT_DIR=os.path.join(_TMP, "none"),
                  PERSONAS_FILE=os.path.join(_TMP, "none.json"), NO_SPEECH="1.0")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "relay"))
import relay  # noqa: E402
import train_flow  # noqa: E402

sys.modules.pop("relay", None)
sys.path.insert(0, os.path.join(HERE, "..", "admin"))
sys.path.insert(0, os.path.join(HERE, "..", "wake"))
sys.path.insert(0, HERE)
import test_admin  # noqa: E402,F401  sets the admin env before grok_admin is imported
import grok_admin as ga  # noqa: E402
import grok_wake as gw  # noqa: E402

BOTS = [{"slug": "juniper", "name": "Juniper", "aliases": []}]


class SetUpSentence(unittest.TestCase):
    def test_set_up_my_wake_word_starts_training(self):
        for said in ("Hey Grok, set up my wake word for Juniper", "hey grok setup my wake word for juniper.",
                     "Set up my wakeboard for Juniper", "Hey Grok, set-up my wake word for Juniper please"):
            hit = relay.train_wake_request(said, BOTS)
            self.assertIsNotNone(hit, said)
            self.assertEqual((hit["slug"], hit["phrase"]), ("juniper", "hey_juniper"), said)

    def test_other_set_up_sentences_do_not_match(self):
        for said in ("Hey Grok, set up my alarm for Juniper", "set up a wake word for Juniper",
                     "set up my wake word", "set up my wakeboard"):
            self.assertIsNone(relay.train_wake_request(said, BOTS), said)


class IntroSaysHey(unittest.TestCase):
    def test_new_and_top_up_intros_say_hey_bot(self):
        hit = {"slug": "juniper", "name": "Juniper", "phrase": "hey_juniper"}
        self.assertEqual(train_flow.voice_train_plan(hit)["say"],
                         "Okay. I'll record you 5 times. Say Hey Juniper after each chime. To stop, stay quiet.")
        self.assertEqual(train_flow.voice_train_plan(hit, has_model=True)["say"],
                         "Okay. I'll add 5 more. Say Hey Juniper after each chime. To stop, stay quiet.")

    def test_hyphen_slug_reads_as_words(self):
        self.assertEqual(train_flow.wake_say_words("hey_den-laptop"), "Hey Den Laptop")
        self.assertEqual(train_flow.wake_say_words("ok_computer"), "Ok Computer")


class AdminTypedWord(unittest.TestCase):
    def test_admin_forwards_the_typed_word(self):
        self.assertEqual(ga.enroll_label("Hey OttieBot", "hey_ottiebot"), "Hey OttieBot")
        self.assertEqual(ga.enroll_label("  hey   juniper ", "hey_juniper"), "hey juniper")
        # A label that doesn't name the same word is dropped; the station falls back to the id.
        self.assertIsNone(ga.enroll_label("Hey Juniper", "hey_otto"))
        self.assertIsNone(ga.enroll_label("Hey <b>x</b>", "hey_bx_b"))
        self.assertIsNone(ga.enroll_label("x" * 60, "x" * 60))

    def test_station_shows_the_typed_word(self):
        self.assertEqual(gw.enroll_say_text("hey_ottiebot", "Hey OttieBot"), "\u201cHey OttieBot\u201d")
        self.assertEqual(gw.enroll_say_text("hey_ottiebot", None), "\u201cHey Ottiebot\u201d")
        self.assertEqual(gw.enroll_say_text("hey_otto", "Hey Juniper"), "\u201cHey Otto\u201d")

    def test_parse_enroll_keeps_a_valid_label(self):
        orig = gw.enroll_phrases
        gw.enroll_phrases = lambda: {"hey_ottiebot": "otto"}
        try:
            got = gw.parse_enroll({"phrase": "hey_ottiebot", "count": gw.ENROLL_COUNT, "label": "Hey OttieBot"})
            self.assertEqual(got.get("label"), "Hey OttieBot")
            bad = gw.parse_enroll({"phrase": "hey_ottiebot", "count": gw.ENROLL_COUNT, "label": "Hey Juniper"})
            self.assertNotIn("label", bad)
        finally:
            gw.enroll_phrases = orig


if __name__ == "__main__":
    unittest.main()
