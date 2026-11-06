#!/usr/bin/env python3
"""0.12.7 regression: a home bot's answer at ~70 s was refused because reply_by was 60 s."""
import os, re, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import botreply


class ReplyWindow(unittest.TestCase):
    def test_reply_by_is_about_three_minutes(self):
        self.assertEqual(botreply.REPLY_BY_S, 180)

    def test_update_reply_by_is_ten_minutes(self):
        self.assertEqual(botreply.UPDATE_REPLY_BY_S, 600)
        self.assertGreater(botreply.UPDATE_REPLY_BY_S, botreply.REPLY_BY_S)

    def test_update_bot_uses_the_longer_window(self):
        src = open(os.path.join(ROOT, "relay", "relay.py")).read()
        fn = src.split("async def admin_bot_update", 1)[1].split("\nasync def ", 1)[0]
        self.assertIn("botreply.UPDATE_REPLY_BY_S", fn)
        self.assertNotIn("botreply.REPLY_BY_S", fn)
        self.assertIn("botreply.REPLY_BY_S", src.split("async def admin_helper_update", 1)[1][:1500])

    def test_every_pending_outlives_its_reply_by(self):
        """No ask may tell the bot reply_by T while its pending file expires before T."""
        src = open(os.path.join(ROOT, "relay", "relay.py")).read()
        self.assertNotIn("now + botreply.SKEW_S) * 1000", src)
