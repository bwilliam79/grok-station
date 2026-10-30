#!/usr/bin/env python3
"""V12 (Noor gs138 QA): at 320 px the wake word text ("Hey Grok") stays on one line with an ellipsis."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_gs138_v2_pending_phone import _phone_css  # noqa: E402


class GrokRowNoWrap(unittest.TestCase):
    def test_wake_text_ellipsis_on_phone(self):
        rule = re.search(r"\.botrow \.ww>span:not\(\.pill\)\{([^}]*)\}", _phone_css())
        self.assertIsNotNone(rule)
        for decl in ("white-space:nowrap", "overflow:hidden", "text-overflow:ellipsis", "min-width:0"):
            self.assertIn(decl, rule.group(1))


if __name__ == "__main__":
    unittest.main()
