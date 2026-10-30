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
    def test_bottom_offset_is_tab_bar_plus_safe_area(self):
        # P6: one --tabbar-h token sets the bar height and the sheet's bottom offset (was an 84px pad).
        self.assertRegex(CSS, r"\.tabs\{[^}]*height:var\(--tabbar-h\)")
        pop = re.search(r"  \.pop\{([^}]*)\}", _phone_css()).group(1)
        self.assertIn("bottom:calc(var(--tabbar-h) + env(safe-area-inset-bottom))", pop)

    def test_close_is_at_least_44(self):
        rule = re.search(r"\.pop \.acts \.btn\{([^}]*)\}", _phone_css()).group(1)
        self.assertGreaterEqual(int(re.search(r"min-height:(\d+)px", rule).group(1)), 44)


if __name__ == "__main__":
    unittest.main()
