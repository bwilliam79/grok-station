#!/usr/bin/env python3
"""Bugbot (PR 2 @400f364): the voice-train intro ends with "To stop, stay quiet." and is longer than
the 6 s filler cap, so a train intro that begins recording plays under the reply cap, not TTS_SHORT_S."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class IntroCap(unittest.TestCase):
    def test_begin_intro_uses_reply_cap(self):
        src = open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8").read()
        block = src[src.index("result = train_session_result(plan)"):src.index('log("train wake")  # no phrase')]
        m = re.search(r"cap = (.+)\n", block)
        self.assertIsNotNone(m)
        self.assertIn('plan.get("begin")', m.group(1))
        self.assertIn("TTS_REPLY_S if", m.group(1))


if __name__ == "__main__":
    unittest.main()
