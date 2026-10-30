#!/usr/bin/env python3
"""V3 (Noor gs138 QA): the phone message sheet clears the tab bar; Close is 44 pt and fully visible."""
import os
import re
import unittest

sys_path = os.path.dirname(os.path.abspath(__file__))
import sys  # noqa: E402
sys.path.insert(0, sys_path)
from test_gs138_v2_pending_phone import CSS, _phone_css  # noqa: E402


class SheetClearsTabBar(unittest.TestCase):
    def test_bottom_padding_is_tab_bar_plus_safe_area(self):
        tab_h = re.search(r"\.tabs\{[^}]*height:(\d+)px", CSS).group(1)
        pop = re.search(r"  \.pop\{([^}]*)\}", _phone_css()).group(1)
        pad = re.search(r"padding:([^;]*)", pop).group(1).split(" ")
        bottom = " ".join(pad[2:]) if len(pad) > 2 else pad[0]
        self.assertEqual(bottom, "calc(%spx + env(safe-area-inset-bottom))" % tab_h)

    def test_close_is_at_least_44(self):
        rule = re.search(r"\.pop \.acts \.btn\{([^}]*)\}", _phone_css()).group(1)
        self.assertGreaterEqual(int(re.search(r"min-height:(\d+)px", rule).group(1)), 44)


if __name__ == "__main__":
    unittest.main()
