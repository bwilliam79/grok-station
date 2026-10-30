#!/usr/bin/env python3
"""NI2: test_handoff passes whether test_persona_route imports relay before it or after it."""
import os
import subprocess
import sys
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))


def _run(*mods):
    return subprocess.run([sys.executable, "-m", "unittest", *mods], cwd=TESTS, capture_output=True, text=True, timeout=600)


class HandoffOrder(unittest.TestCase):
    def test_persona_route_first(self):
        out = _run("test_persona_route", "test_handoff")
        self.assertEqual(out.returncode, 0, out.stderr[-3000:])

    def test_handoff_first(self):
        out = _run("test_handoff", "test_persona_route")
        self.assertEqual(out.returncode, 0, out.stderr[-3000:])


if __name__ == "__main__":
    unittest.main()
