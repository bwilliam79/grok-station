#!/usr/bin/env python3
"""V9 (Noor gs138 QA): the System page log selects #lp and #ls each have a visible label."""
import os
import unittest

JS = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "web", "app.js"), encoding="utf-8").read()


class LogLabels(unittest.TestCase):
    def test_visible_labels(self):
        self.assertIn('<label for="lp">Log</label><select class="sel" id="lp"', JS)
        self.assertIn('<label for="ls">Time range</label><select class="sel" id="ls"', JS)


if __name__ == "__main__":
    unittest.main()
