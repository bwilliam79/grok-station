#!/usr/bin/env python3
"""Bugbot findings on PR 2 (0.13.8 RC).

B1 Medium: Configure all (2+ names not set up) did nothing.
B2 Low: the not-connected callout always named Juniper.
B3 Medium: a failed wake enroll start was ignored after the spoken intro.
"""
import json
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_gs138_bots_header import _run  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ConfigureAllCallout(unittest.TestCase):
    def test_configure_all_starts_setup_for_the_first_pending_name(self):
        stub = ("const calls = [];\n"
                "function pendingTeammates(el) { return [{name: 'Otto'}, {name: 'Pip'}]; }\n"
                "function addBot(voices, el, o) { calls.push(o && o.name); }\n")
        got = _run("(openConfigureAll({_voices: []}), calls)", stub)
        self.assertEqual(got, ["Otto"])

    def test_callout_names_the_main_bot(self):
        opts = {"bots": [{"slug": "w", "name": "Wren", "main": True}, {"slug": "j", "name": "Juniper"}],
                "pending": [{"name": "Otto"}, {"name": "Pip"}], "behind": []}
        html = _run("botsCalloutHTML(%s)" % json.dumps(opts))
        self.assertIn("Wren can send each one its setup message.", html)
        self.assertNotIn("Juniper can", html)

    def test_callout_without_a_main_bot_is_generic(self):
        opts = {"bots": [{"slug": "j", "name": "Juniper"}], "pending": [{"name": "Otto"}], "behind": []}
        html = _run("botsCalloutHTML(%s)" % json.dumps(opts))
        self.assertIn("Your main bot can send each one its setup message.", html)


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
