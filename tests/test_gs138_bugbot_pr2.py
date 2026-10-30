#!/usr/bin/env python3
"""Bugbot findings on PR 2 (0.13.8 RC).

B1/B2 (Configure all callout) are held for 0.13.9 with that callout: see test_gs138_c_hold_callout.py.
B3 Medium: a failed wake enroll start was ignored after the spoken intro.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class EnrollStartFailure(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("RELAY_TOKEN", "test-token")
        os.environ.setdefault("XAI_API_KEY", "test-key")
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        import relay
        self.relay = relay

    def test_ok_status_says_nothing(self):
        self.assertEqual(self.relay.enroll_start_failed_say(200), "")
        self.assertEqual(self.relay.enroll_start_failed_say(204), "")

    def test_failed_status_gets_a_spoken_line(self):
        for status in (400, 503, None):
            self.assertEqual(self.relay.enroll_start_failed_say(status),
                             "I couldn't start recording. Try again in a moment.")

    def test_session_speaks_the_failure(self):
        src = open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8").read()
        i = src.index('if result["enroll"]:')
        block = src[i:i + 600]
        self.assertIn("enroll_start_failed_say(post_wake_enroll(", block)
        self.assertIn("play_fixed(", block)


if __name__ == "__main__":
    unittest.main()
