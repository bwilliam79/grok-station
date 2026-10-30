#!/usr/bin/env python3
"""N2: the bots-row popover paints above the wake column.

.botrow .ww is position:relative; z-index:2, and a grid item at that level paints over
a sibling cell's tooltip even when the tooltip's own z-index is higher. The status
popover's grid item must be a stacking context at z-index >= 3. The message popover
is portaled to body; its host has to be one too, or the inner .pop z-index stays under .ww.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "admin", "web", "admin.css")
JS = os.path.join(ROOT, "admin", "web", "app.js")


class PopoverAboveWakeColumn(unittest.TestCase):
    def test_popover_stacks_above_ww(self):
        with open(CSS, encoding="utf-8") as f:
            css = f.read()
        with open(JS, encoding="utf-8") as f:
            js = f.read()
        ww = re.search(r"\.botrow \.ww\{[^}]*z-index:\s*(\d+)", css)
        self.assertIsNotNone(ww)
        ww_z = int(ww.group(1))
        self.assertEqual(ww_z, 2)

        tip = re.search(r"\.botrow \.tipwrap\{[^}]*z-index:\s*(\d+)", css)
        self.assertIsNotNone(tip, "status popover grid item has no z-index above .ww")
        self.assertGreaterEqual(int(tip.group(1)), 3)
        self.assertGreater(int(tip.group(1)), ww_z)

        host = re.search(r"#bmsgpop\{[^}]*\}", css)
        self.assertIsNotNone(host, "message popover host does not create a stacking context")
        block = host.group(0)
        self.assertRegex(block, r"position:\s*(?:absolute|fixed|relative)")
        host_z = int(re.search(r"z-index:\s*(\d+)", block).group(1))
        self.assertGreaterEqual(host_z, 3)
        self.assertGreater(host_z, ww_z)
        self.assertIn("pointer-events:none", block.replace(" ", ""))

        show = js.split("function showBotMessage(", 1)[1].split("function paintBots(", 1)[0]
        self.assertIn('wrap.id = "bmsgpop"', show)
        self.assertIn("document.body.appendChild(wrap)", show)


if __name__ == "__main__":
    unittest.main()
