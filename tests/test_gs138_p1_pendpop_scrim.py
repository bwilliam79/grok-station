#!/usr/bin/env python3
"""P1 (Noor, b8b1fe6 re-check, Sev 2): the phone pending-row sheet (#pendpop) stacks above the sticky header.

The .top header is z5 and the sheet's scrim z4, so the header stayed bright and tappable behind an aria-modal
sheet. #pendpop takes the message sheet's z6 wrapper; elementFromPoint on the header now hits the scrim.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gs138_pchrome import CHROME, chrome

PROBE = """
  const h = document.getElementById('hdr').getBoundingClientRect();
  res.header = who(document.elementFromPoint(h.left + h.width / 2, h.top + h.height / 2));
  res.headerLeft = who(document.elementFromPoint(h.left + 8, h.top + 8));
  const wrap = document.getElementById('pendpop'); const cs = getComputedStyle(wrap);
  res.wrap = {position: cs.position, z: cs.zIndex};
"""


@unittest.skipUnless(CHROME, "Chrome not installed")
class PendingSheetAboveHeader(unittest.TestCase):
    def test_header_is_under_the_scrim(self):
        for w in (390, 320):
            d = chrome(w, PROBE)
            self.assertEqual(d["header"], "scrim", (w, d))
            self.assertEqual(d["headerLeft"], "scrim", (w, d))
            self.assertEqual(d["wrap"], {"position": "absolute", "z": "6"}, (w, d))


if __name__ == "__main__":
    unittest.main()
