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
class N1FixedRowHeight(unittest.TestCase):
    def test_message_is_one_tappable_line_in_the_info_column(self):
        for row in (STUCK, FAILED, UNKNOWN):
            html = _html(row)
            info, btns = _cols(html)
            self.assertNotIn('class="help"', btns, row["status"])
            self.assertEqual(html.count('class="ssub"'), 1, row["status"])
            self.assertIn('class="ssub"', info)
            self.assertIn('aria-haspopup="dialog"', info)

    def test_failure_extras_move_to_the_popover(self):
        html = _html(FAILED)
        self.assertNotIn("Still can't reach", html)
        self.assertNotIn('data-act="maintest"', html)
        self.assertIn("function showSetupMessage(", JS)
        pop = _fn("setupMessagePopHTML")
        self.assertIn('data-act="maintest"', pop)

    def test_row_height_is_fixed(self):
        self.assertRegex(FLAT, r"\.setr\{[^}]*height:48px")
        self.assertRegex(FLAT, r"\.ssub\.tx\{[^}]*white-space:nowrap[^}]*text-overflow:ellipsis")
        phone = _phone_rules()
        self.assertRegex(phone, r"\.setr\{[^}]*height:164px")
        self.assertRegex(phone, r"\.racts\{[^}]*flex-direction:row")

    def test_tap_opens_full_text(self):
        paint = _fn("paintSetupList")
        self.assertIn(".ssub", paint)
        self.assertIn("showSetupMessage(", paint)
        d = _run("setupMessageDetail(setupRowModel(%s, {mainName: 'Juniper', now: 1000}))" % json.dumps(FAILED))
        self.assertEqual(d["title"], "Couldn't reach Juniper")
        self.assertEqual(d["body"], "Otto's setup wasn't sent. Check that Juniper's computer is on, then try again.")
        self.assertEqual(d["again"], "Still can't reach Juniper.")
        pop = _run("setupMessagePopHTML(%s, true)" % json.dumps(d))
        self.assertIn("Test Juniper's connection", pop)
        self.assertIn('aria-modal="true"', pop)



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



@unittest.skipUnless(shutil.which("node"), "node not installed")
class N3FixedButtonColumn(unittest.TestCase):
    def _slots(self, html):
        _, btns = _cols(html)
        return re.findall(r'<(?:button|span)[^>]*data-slot="(\w)"[^>]*?(?:data-act="(\w+)")?[^>]*>', btns)

    def test_send_and_send_again_share_one_slot(self):
        fresh = _cols(_html(base._row("not_set_up")))[1]
        stuck = _cols(_html(STUCK))[1]
        unknown = _cols(_html(UNKNOWN))[1]
        failed = _cols(_html(FAILED))[1]
        for btns, act in ((fresh, "send"), (stuck, "again"), (unknown, "again"), (failed, "retry")):
            self.assertRegex(btns, r'data-slot="a"[^>]*data-act="%s"' % act)
        self.assertRegex(stuck, r'data-slot="b"[^>]*data-act="paste"')
        # Both slots are always drawn, so slot a never moves when slot b is empty.
        for btns in (fresh, unknown, failed):
            self.assertIn('data-slot="b"', btns)

    def test_button_column_has_a_fixed_width(self):
        m = re.search(r"\.sendc2\{([^}]*)\}", FLAT)
        self.assertIsNotNone(m)
        self.assertRegex(m.group(1), r"(^|;)width:\d+px")
        self.assertRegex(FLAT, r"\.racts\{[^}]*grid-template-columns:\d+px\d+px")

    def test_help_text_never_in_the_button_column(self):
        for row in (base._row("not_set_up"), FAILED, STUCK):
            html = _html(row, sendBlocked=True)
            info, btns = _cols(html)
            self.assertNotIn("help", btns)
            self.assertIn("Update Juniper first", info)
            hid = re.search(r'aria-describedby="([^"]+)"', btns).group(1)
            self.assertIn('id="%s"' % hid, info)


if __name__ == "__main__":
    unittest.main()
