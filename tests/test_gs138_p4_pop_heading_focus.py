#!/usr/bin/env python3
"""P4 (Noor, b8b1fe6 re-check, Sev 1): the sheet heading takes focus on open without a default focus box."""
import re
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gs138_pchrome import CHROME, CSS, chrome

PROBE = """
  const h = document.querySelector('#pendpop h3'); res.focused = document.activeElement === h;
  const cs = getComputedStyle(h); res.outline = cs.outlineStyle + ' ' + cs.outlineWidth;
"""


@unittest.skipUnless(CHROME, "Chrome not installed")
class HeadingNoFocusBox(unittest.TestCase):
    def test_heading_focused_without_outline(self):
        for w in (390, 320):
            d = chrome(w, PROBE)
            self.assertTrue(d["focused"], (w, d))
            self.assertTrue(d["outline"].startswith("none"), (w, d))

    def test_rule_matches_0139(self):
        self.assertRegex(CSS, r"\n\.pop h3:focus\{outline:none\}")


if __name__ == "__main__":
    unittest.main()
