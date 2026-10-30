#!/usr/bin/env python3
"""P3 (Noor, b8b1fe6 re-check, Sev 2): the phone pending-row sheet traps Tab and handles Esc on document.

Tab from Remove wraps to Close, Shift+Tab from Close wraps to Remove, and Esc still closes after focus has left
the sheet (the 0.13.9 R4 lesson: a keydown listener on the sheet alone misses it). The message sheet and the
pending sheet share the existing trapTab(sheet) helper.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gs138_pchrome import CHROME, JS, chrome, fn

PROBE = """
  const c = document.querySelector('#pendpop [data-close]'), r = document.querySelector('#pendpop [data-remove]');
  r.focus(); key('Tab'); res.tabFromLast = ae();
  c.focus(); key('Tab', true); res.shiftTabFromFirst = ae();
  c.focus(); key('Tab'); res.tabMiddleDefault = ae();
  document.querySelector('#pbar button').focus(); key('Escape'); res.escOutside = !!document.getElementById('pendpop');
  openPendingSheet(row, document.body); document.getElementById('pendpop').remove();
  document.querySelector('#pbar button').focus(); key('Escape'); res.staleListener = ae();
"""


@unittest.skipUnless(CHROME, "Chrome not installed")
class PendingSheetTrap(unittest.TestCase):
    def test_tab_wraps_and_esc_works_outside(self):
        for w in (390, 320):
            d = chrome(w, PROBE)
            self.assertEqual(d["tabFromLast"], "BUTTON[close]", (w, d))
            self.assertEqual(d["shiftTabFromFirst"], "BUTTON[remove]", (w, d))
            self.assertEqual(d["tabMiddleDefault"], "BUTTON[remove]", (w, d))
            self.assertFalse(d["escOutside"], (w, d))
            self.assertEqual(d["staleListener"], "BUTTON", (w, d))  # removed sheet: Esc does nothing

    def test_message_sheet_shares_the_helper(self):
        self.assertIn("trapTab(pop)", fn("showBotMessage"))
        self.assertIn("trapTab(pop)", fn("openPendingSheet"))
        self.assertEqual(JS.count("\nfunction trapTab("), 1)
        self.assertNotIn('querySelectorAll("button, [href], input, select, textarea")', JS)


if __name__ == "__main__":
    unittest.main()
