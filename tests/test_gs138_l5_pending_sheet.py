#!/usr/bin/env python3
"""L5 (gs-voice-train SPEC rev1.4, Noor): the phone pending-row sheet has a 44 pt Remove and a visible Close of
44 pt or more (plus outside tap and Esc); focus moves to the heading on open and back to the row's Set up button on close (P2).
V9: the Logs selects keep visible "Log" and "Time range" labels, not aria-only."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()


def _fn(name):
    body = JS[JS.index("function %s(" % name):]
    return body[:body.index("\n}\n")]


class PendingSheet(unittest.TestCase):
    def test_close_button_visible(self):
        f = _fn("openPendingSheet")
        self.assertIn('data-close', f)
        self.assertRegex(f, r'<button class="btn[^"]*" type="button" data-close>Close</button>')
        self.assertIn('wrap.querySelector("[data-close]").onclick = close;', f)
        self.assertIn('wrap.querySelector(".scrim").onclick = close;', f)
        self.assertIn('ev.key === "Escape"', f)

    def test_buttons_44pt(self):
        m = re.search(r"#pendpop \.btn\{([^}]*)\}", CSS)
        self.assertIsNotNone(m)
        self.assertIn("min-height:44px", m.group(1))
        self.assertIn("min-width:44px", m.group(1))

    def test_focus_heading_then_row(self):
        f = _fn("openPendingSheet")
        self.assertIn('<h3 tabindex="-1">', f)
        self.assertIn('wrap.querySelector("h3").focus();', f)
        self.assertNotIn("rm.focus()", f)
        self.assertIn('row.querySelector("[data-setupbtn], [data-remove]")', f)  # P2: back to Set up, not the div
        self.assertNotIn("row.focus()", f)


class LogLabelsVisible(unittest.TestCase):
    def test_labels_visible(self):
        for lab in ('<label for="lp">Log</label>', '<label for="ls">Time range</label>'):
            self.assertIn(lab, JS)  # plain label: no sr-only class, no hidden attribute
        self.assertNotRegex(CSS, r"(\.right|\.inline|\.card)\s+label\s*\{[^}]*(display:\s*none|clip|sr-only)")


if __name__ == "__main__":
    unittest.main()
