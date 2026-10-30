#!/usr/bin/env python3
"""0.13.12 Noor items: bq-0025 sent-row buttons in one 3-column row at phone width, and the restored
.btn:focus-visible pin (a visible 2 px focus outline, WCAG 2.2 2.4.7 / 2.4.11)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_gs1311_bots_merge import JS, CSS  # noqa: E402
from test_gs1311_noor_qa import _media  # noqa: E402


class SentRowGrid(unittest.TestCase):
    def test_sent_row_is_three_equal_columns_at_phone_width(self):
        phone = _media(599)  # covers the 390 px frame
        self.assertIn("#blist .botrow .cfg.sent3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}", phone)
        self.assertIn("#blist .botrow .cfg.sent3 .setacts{display:contents}", phone)
        self.assertIn("#blist .botrow .cfg.sent3 .btn{width:100%;min-width:0;min-height:44px;", phone)
        self.assertNotIn("sent3", CSS.replace(phone, ""))  # desktop layout unchanged

    def test_only_a_sent_row_gets_the_grid(self):
        self.assertIn('<div class="cfg${su && /setacts sent/.test(su.action) ? " sent3" : ""}">', JS)


class FocusPin(unittest.TestCase):
    def test_btn_focus_visible_outline_is_2px(self):
        self.assertIn(".btn:focus-visible{outline:2px solid var(--gs-teal);outline-offset:2px;box-shadow:none}", CSS)


if __name__ == "__main__":
    unittest.main()
