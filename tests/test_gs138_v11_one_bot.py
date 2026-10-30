#!/usr/bin/env python3
"""V11 (Noor gs138 QA): the update callout uses the singular for one bot."""
import json
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_gs138_bots_header import _run  # noqa: E402


def _upd(n):
    behind = [{"slug": "b%d" % i, "name": "B%d" % i} for i in range(n)]
    return _run("botsCalloutHTML(%s)" % json.dumps({"bots": behind, "pending": [], "behind": behind}))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class OneBot(unittest.TestCase):
    def test_singular(self):
        self.assertIn(">1 bot needs an update<", _upd(1))

    def test_plural(self):
        self.assertIn(">3 bots need an update<", _upd(3))


if __name__ == "__main__":
    unittest.main()
