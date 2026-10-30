#!/usr/bin/env python3
"""P2 (Noor, b8b1fe6 re-check, Sev 2): closing the phone pending-row sheet returns focus to the row's Set up button.

row.focus() on a plain div did nothing, so focus fell to <body>. Every close path (Close, scrim, Esc) is measured.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gs138_pchrome import CHROME, chrome

PROBE = """
  res.paths = {};
  document.querySelector('#pendpop [data-close]').click(); res.paths.close = ae();
  openPendingSheet(row, document.body); document.querySelector('#pendpop .scrim').click(); res.paths.scrim = ae();
  openPendingSheet(row, document.body); key('Escape'); res.paths.esc = ae();
  res.open = !!document.getElementById('pendpop');
"""


@unittest.skipUnless(CHROME, "Chrome not installed")
class FocusReturnsToSetUp(unittest.TestCase):
    def test_every_close_path_focuses_set_up(self):
        for w in (390, 320):
            d = chrome(w, PROBE)
            self.assertEqual(d["paths"], {"close": "BUTTON[setup]", "scrim": "BUTTON[setup]", "esc": "BUTTON[setup]"}, (w, d))
            self.assertFalse(d["open"], (w, d))


if __name__ == "__main__":
    unittest.main()
