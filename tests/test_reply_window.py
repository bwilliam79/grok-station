#!/usr/bin/env python3
"""0.12.6 F regression: a home bot's answer at ~70 s was refused because reply_by was 60 s."""
import os, re, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import botreply


class ReplyWindow(unittest.TestCase):
    def test_reply_by_is_about_three_minutes(self):
        self.assertGreaterEqual(botreply.REPLY_BY_S, 170)

    def test_every_pending_outlives_its_reply_by(self):
        """No ask may tell the bot reply_by T while its pending file expires before T."""
        src = open(os.path.join(ROOT, "relay", "relay.py")).read()
        self.assertNotIn("now + botreply.SKEW_S) * 1000", src)
