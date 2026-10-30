#!/usr/bin/env python3
"""V2 (Noor gs138 QA): on phone, Not set up rows show Set up as a 44 pt button; Remove is in the row's sheet."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()


def _phone_css():
    out = []
    for m in re.finditer(r"@media \(max-width:767px\)\{", CSS):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(CSS[i], 0)
            i += 1
        out.append(CSS[m.end():i - 1])
    return "\n".join(out)


class PendingRowPhone(unittest.TestCase):
    def test_set_up_shows_at_44(self):
        rule = re.search(r"\.botrow\[data-pending\] \.cfg \.btn\[data-setup\]\{([^}]*)\}", _phone_css())
        self.assertIsNotNone(rule)
        self.assertIn("display:inline-flex", rule.group(1))
        self.assertIn("min-height:44px", rule.group(1))
        self.assertIn("height:44px", rule.group(1))

    def test_remove_stays_hidden_in_row_on_phone(self):
        self.assertIn(".botrow .cfg .btn{display:none}", _phone_css())
        self.assertNotRegex(_phone_css(), r"data-remove[^{]*\{[^}]*display:(inline-)?flex")

    def test_row_tap_opens_sheet_with_remove(self):
        self.assertIn('if (row.getAttribute("data-pending")) return phone ? openPendingSheet(row, el) : undefined;', JS)
        i = JS.index("function openPendingSheet(row, el) {")
        body = JS[i:JS.index("\nfunction ", i + 10)]
        self.assertIn('class="pop"', body)
        self.assertIn('data-remove="${esc(pend)}">Remove</button>', body)
        self.assertIn("removePending(pend, el)", body)

    def test_row_height_unchanged(self):
        # No height rule for pending rows: they keep the shared 110 px phone row.
        self.assertNotRegex(_phone_css(), r"\.botrow\[data-pending\]\{[^}]*height")


if __name__ == "__main__":
    unittest.main()
