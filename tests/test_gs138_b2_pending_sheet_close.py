#!/usr/bin/env python3
"""Bugbot (PR 2 @400f364): the phone pending-row sheet (#pendpop) never stacks and closes when the page changes."""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PendingSheetClose(unittest.TestCase):
    def setUp(self):
        self.js = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()

    def test_open_replaces_existing_sheet(self):
        body = self.js[self.js.index("function openPendingSheet("):]
        body = body[:body.index("\n}\n")]
        self.assertIn("closePendingSheet();", body.split("const wrap")[0])

    def test_page_change_closes_sheet(self):
        self.assertIn('window.addEventListener("hashchange", closePendingSheet);', self.js)
        fn = self.js[self.js.index("function closePendingSheet("):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn('document.getElementById("pendpop")', fn)
        self.assertIn(".remove()", fn)


if __name__ == "__main__":
    unittest.main()
