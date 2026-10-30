#!/usr/bin/env python3
"""V10 (Noor gs138 QA): re-ask minutes field shows only the "minutes" suffix; its accessible name is "Re-ask after, in minutes"."""
import os
import re
import unittest

JS = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "web", "app.js"), encoding="utf-8").read()


class GminLabel(unittest.TestCase):
    def test_no_visible_minutes_label(self):
        block = re.search(r'<div class="gmin" id="gmin"[^\n]*', JS).group(0)
        self.assertNotIn('<label for="gminn">', block)
        self.assertIn("<span>minutes</span>", block)

    def test_accessible_name(self):
        inp = re.search(r'<input class="inp" id="gminn"[^>]*>', JS).group(0)
        self.assertIn('aria-label="Re-ask after, in minutes"', inp)


if __name__ == "__main__":
    unittest.main()
