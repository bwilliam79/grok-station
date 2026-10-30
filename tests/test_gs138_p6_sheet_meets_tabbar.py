#!/usr/bin/env python3
"""P6 (Noor ruling 5:41 AM): one --tabbar-h token (72px, the live tab bar) sets the tab bar height and the phone
bottom sheet's offset, so the sheet's bottom edge meets the tab bar's top with no strip of page between them.
The old 84px pad was a hard-coded guess at the bar height."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gs138_pchrome import CHROME, CSS, chrome  # noqa: E402

PROBE = """
  const p = document.querySelector('#pendpop .pop').getBoundingClientRect();
  const b = document.getElementById('pbar').getBoundingClientRect();
  res.sheetBottom = Math.round(p.bottom); res.barTop = Math.round(b.top); res.barH = Math.round(b.height);
  res.gap = Math.round(b.top - p.bottom);
  const c = document.querySelector('#pendpop [data-close]').getBoundingClientRect();
  res.closeAboveBar = Math.round(c.bottom) <= Math.round(b.top);
"""


@unittest.skipUnless(CHROME, "Chrome not installed")
class SheetMeetsTabBar(unittest.TestCase):
    def test_sheet_bottom_is_tab_bar_top(self):
        for w in (390, 320):
            d = chrome(w, PROBE)
            self.assertEqual(d["sheetBottom"], d["barTop"], (w, d))
            self.assertEqual(d["barH"], 72, (w, d))
            self.assertTrue(d["closeAboveBar"], (w, d))

    def test_one_token(self):
        self.assertRegex(CSS, r":root\{[^}]*--tabbar-h:72px")
        self.assertNotIn("84px", CSS)
        self.assertIn("bottom:calc(var(--tabbar-h) + env(safe-area-inset-bottom))", CSS)
        self.assertRegex(CSS, r"\.tabs\{[^}]*height:var\(--tabbar-h\)")


if __name__ == "__main__":
    unittest.main()
