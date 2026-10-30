#!/usr/bin/env python3
"""gs139 Set up bots: Noor's visual-check fixes on ca5d284 (N1-N7).

The page functions run under node, the same way as test_gs139_setup_bots. Layout is checked from the
rendered markup and admin.css rules; the pixel heights were measured separately in headless Chrome.
"""
import json
import os
import re
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_gs139_setup_bots as base  # noqa: E402

JS = base.JS
CSS = base.CSS
FLAT = re.sub(r"\s+", "", CSS)


def _fn(name):
    return base._fn(name)


def _html(row, **opts):
    return base._html(row, **opts)


NAMES = ("clockTime", "joinBotNames", "setupCopy", "setupStuck", "setupDomId", "setupMark", "setupOwnerLabel",
         "setupSteps", "setupIcon", "setupRowModel", "setupRowHTML", "setupListHTML", "setupMessageDetail",
         "setupMessagePopHTML", "setupKeepUnknown")


def _run(expr):
    src = "\n".join(_fn(n) for n in NAMES if "\nfunction " + n + "(" in JS)
    prog = ("const esc = s => String(s == null ? '' : s);\n" + base._icons() + "\n" + src +
            "\nprocess.stdout.write(JSON.stringify(" + expr + "));")
    out = base.subprocess.run(["node", "-e", prog], capture_output=True, text=True)
    if out.returncode:
        raise AssertionError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "node failed")
    return json.loads(out.stdout)


def _cols(html):
    """(info column, button column) of one rendered row."""
    i = html.index('<div class="sendc2"')
    return html[:i], html[i:]


def _phone_rules():
    m = re.search(r"@media \(max-width:767px\)\{\s*\.setr\{", CSS)
    assert m, "phone block for .setr missing"
    start = m.start()
    depth, j = 0, CSS.index("{", start)
    for k in range(j, len(CSS)):
        if CSS[k] == "{":
            depth += 1
        elif CSS[k] == "}":
            depth -= 1
            if depth == 0:
                return re.sub(r"\s+", "", CSS[start:k + 1])
    raise AssertionError("unclosed phone block")


STUCK = base._row("sent", sent_at=1000 - 600)
FAILED = base._row("failed", fails=2)
UNKNOWN = base._row("not_set_up", unknown=True)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class N4StuckSubLine(unittest.TestCase):
    def test_no_ok_card_sits_next_to_the_sent_chip(self):
        html = _html(STUCK)
        info, btns = _cols(html)
        self.assertNotIn("No OK card yet?", btns)
        self.assertIn("No OK card yet?", info)
        chip = info.index("Sent ")
        self.assertLess(chip, info.index("No OK card yet?"))
        line = info[info.rindex('<div class="sline"', 0, chip):]
        self.assertIn("No OK card yet?", line)
        self.assertNotIn('<div class="lbl"', line)

    def test_fresh_sent_row_has_no_sub_line(self):
        html = _html(base._row("sent", sent_at=1000 - 30))
        self.assertNotIn("No OK card yet?", html)


if __name__ == "__main__":
    unittest.main()
