#!/usr/bin/env python3
"""V8 (Noor gs138 QA), 0.13.9 item 6: the Integrations back button's focus ring is a solid 2px teal outline."""
import os
import re
import unittest

CSS = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "web", "admin.css"), encoding="utf-8").read()


class BackLinkFocus(unittest.TestCase):
    def test_solid_teal_ring(self):
        rule = re.search(r"a\.iback:focus-visible\{([^}]*)\}", CSS).group(1)
        self.assertIn("outline:2px solid var(--gs-teal);outline-offset:2px", rule)
        self.assertNotIn("rgba(", rule)


if __name__ == "__main__":
    unittest.main()
