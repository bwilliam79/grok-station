#!/usr/bin/env python3
"""0.13.9 bug: the transcriber often hears "train my wake word" as "train my wakeboard" or "wake board".

train_wake_request accepts those as the same request. Fails on 0.13.8 (the sentence fell through to the model).
"""
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ.update(RELAY_TOKEN="test-token", XAI_API_KEY="test-key", ALERT_DIR=os.path.join(_TMP, "none"),
                  PERSONAS_FILE=os.path.join(_TMP, "none.json"), NO_SPEECH="1.0")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "relay"))
import relay  # noqa: E402

sys.modules.pop("relay", None)

BOTS = [{"slug": "juniper", "name": "Juniper", "aliases": []}]


class WakeboardVariant(unittest.TestCase):
    def test_wakeboard_and_wake_board_match(self):
        for said in ("Hey Grok, train my wakeboard for Juniper", "hey grok train my wake board for juniper.",
                     "train my Wake-board for Juniper", "train my wake word for Juniper"):
            hit = relay.train_wake_request(said, BOTS)
            self.assertIsNotNone(hit, said)
            self.assertEqual((hit["slug"], hit["phrase"]), ("juniper", "hey_juniper"), said)

    def test_other_boards_do_not_match(self):
        for said in ("Hey Grok, what's a good wakeboard for beginners?", "Hey Grok, wakeboard for Juniper",
                     "how do I train on my wakeboard for Juniper", "train a wakeboard for Juniper",
                     "train my skateboard for Juniper", "train my wake boards for Juniper", "train my board for Juniper"):
            self.assertIsNone(relay.train_wake_request(said, BOTS), said)


if __name__ == "__main__":
    unittest.main()
