#!/usr/bin/env python3
"""Item 2 (approved): copy names the configured main bot, never a hard-coded Juniper.

Fallback: "your main bot" mid-sentence. The callout copy (sentence-start "Your main bot") returns in 0.13.9.
"""
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_gs138_bots_header import JS, _fn, _run  # noqa: E402

STUBS = "function routineForBot() { return ''; }\nfunction botIdFromName(n) { return String(n).toLowerCase(); }\n" + \
    _fn("teamSetupMessage")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class MainBotName(unittest.TestCase):
    def test_no_hard_coded_juniper_in_copy(self):
        for line in JS.splitlines():
            if "Juniper" in line:
                self.assertTrue(line.strip().startswith("//"), line)

    def test_team_setup_message_mid_sentence(self):
        named = _run("teamSetupMessage([], {_bots: [{slug: 'w', name: 'Wren', main: true}]})", STUBS)
        self.assertTrue(named.startswith("Paste this to Wren. "), named)
        none = _run("teamSetupMessage([], {_bots: []})", STUBS)
        self.assertTrue(none.startswith("Paste this to your main bot. "), none)


if __name__ == "__main__":
    unittest.main()
