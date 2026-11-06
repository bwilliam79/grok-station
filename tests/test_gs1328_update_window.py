#!/usr/bin/env python3
"""0.13.28: update_bot reply window matches the Bots page wait; same-version updater prints status."""
import os, re, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import botreply

from test_finish_update import Fake, REL, status


class UpdateWindow(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(botreply.REPLY_BY_S, 180)
        self.assertEqual(botreply.UPDATE_REPLY_BY_S, 600)

    def test_update_bot_source_uses_update_window(self):
        src = open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8").read()
        fn = src.split("async def admin_bot_update", 1)[1].split("\nasync def ", 1)[0]
        self.assertEqual(fn.count("botreply.UPDATE_REPLY_BY_S"), 2)
        self.assertNotIn("botreply.REPLY_BY_S", fn)


class SameVersionStatus(unittest.TestCase):
    def test_same_release_prints_status_and_instructions(self):
        f = Fake("home")
        open(os.path.join(f.state, "release-jules"), "w").write(REL + "\n")
        r = f.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("BEGIN INSTRUCTIONS", r.stdout)
        self.assertIn("END INSTRUCTIONS", r.stdout)
        m = re.fullmatch(r"(\S+) instr=([0-9a-f]{16}) helper=(\S+) computer=([0-9a-f]{16}|-)", status(r.stdout))
        self.assertTrue(m, r.stdout)
        self.assertEqual(m.group(1), REL)

    def test_older_requested_release_is_still_refused(self):
        f = Fake("home")
        open(os.path.join(f.state, "release-jules"), "w").write("99.0.0\n")
        r = f.run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not newer", r.stderr)
        self.assertNotIn("BEGIN INSTRUCTIONS", r.stdout)


if __name__ == "__main__":
    unittest.main()
