#!/usr/bin/env python3
"""V8 (Noor gs138 QA): the Integrations back link's focus ring is solid #3FD3B6."""
import os
import re
import unittest

CSS = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "web", "admin.css"), encoding="utf-8").read()


class BackLinkFocus(unittest.TestCase):
    def test_solid_teal_ring(self):
        rule = re.search(r"a\.iback:focus-visible\{([^}]*)\}", CSS).group(1)
        self.assertIn("outline:3px solid #3FD3B6", rule)
        self.assertNotIn("rgba(", rule)


if __name__ == "__main__":
    unittest.main()
