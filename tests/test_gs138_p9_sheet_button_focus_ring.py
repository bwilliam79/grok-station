#!/usr/bin/env python3
"""P9 (Noor, 0a1fc0d re-check, Sev 1): sheet buttons get the solid 2px teal :focus-visible outline.

The 1px teal border plus a 30% glow (1.95:1) did not give a 3:1 focus indicator; .disclose and .iconbtn
already use outline:2px solid var(--gs-teal), so the sheet's buttons do too.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gs138_pchrome import CHROME, CSS, chrome

PROBE = """
  res.out = [];
  for (const b of document.querySelectorAll('#pendpop .pop .btn')) {
    b.focus(); const cs = getComputedStyle(b);
    res.out.push({fv: b.matches(':focus-visible'), style: cs.outlineStyle, width: cs.outlineWidth, color: cs.outlineColor});
  }
  res.teal = getComputedStyle(document.documentElement).getPropertyValue('--gs-teal').trim();
"""


@unittest.skipUnless(CHROME, "Chrome not installed")
class SheetButtonFocusRing(unittest.TestCase):
    def test_focused_sheet_buttons_have_solid_2px_teal_outline(self):
        for w in (390, 1280):
            d = chrome(w, PROBE)
            self.assertTrue(d["out"], (w, d))
            for b in d["out"]:
                self.assertTrue(b["fv"], (w, b))
                self.assertEqual(b["style"], "solid", (w, b))
                self.assertEqual(b["width"], "2px", (w, b))
                self.assertEqual(b["color"], "rgb(63, 211, 182)", (w, b))

    def test_rule_scoped_to_pop(self):
        self.assertRegex(CSS, r"\n\.pop \.btn:focus-visible\{outline:2px solid var\(--gs-teal\)")


if __name__ == "__main__":
    unittest.main()
