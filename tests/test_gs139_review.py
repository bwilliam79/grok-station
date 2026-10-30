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
    if "\nasync function " + name + "(" in JS:
        start = JS.index("\nasync function " + name + "(")
        nxt = re.compile(r"\n(?:async )?function ").search(JS, start + 1)
        return JS[start:nxt.start()]
    return base._fn(name)


def _html(row, **opts):
    return base._html(row, **opts)


NAMES = ("clockTime", "joinBotNames", "setupCopy", "setupStuck", "setupDomId", "setupMark", "setupOwnerLabel",
         "setupSteps", "setupIcon", "setupRowModel", "setupKeepUnknown")


def _run(expr):
    src = "\n".join(_fn(n) for n in NAMES if "\nfunction " + n + "(" in JS)
    prog = ("const esc = s => String(s == null ? '' : s);\n" + base._icons() + "\n" + src +
            "\nprocess.stdout.write(JSON.stringify(" + expr + "));")
    out = base.subprocess.run(["node", "-e", prog], capture_output=True, text=True)
    if out.returncode:
        errs = [ln for ln in out.stderr.splitlines() if "Error" in ln]
        raise AssertionError(errs[0] if errs else "node failed")
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


    def test_row_height_is_fixed(self):
        self.assertRegex(FLAT, r"\.setr\{[^}]*height:48px")
        self.assertRegex(FLAT, r"\.ssub\.tx\{[^}]*white-space:nowrap[^}]*text-overflow:ellipsis")
        phone = _phone_rules()
        self.assertRegex(phone, r"\.setr\{[^}]*height:164px")
        self.assertRegex(phone, r"\.racts\{[^}]*flex-direction:row")







@unittest.skipUnless(shutil.which("node"), "node not installed")
class N3FixedButtonColumn(unittest.TestCase):
    def _slots(self, html):
        _, btns = _cols(html)
        return re.findall(r'<(?:button|span)[^>]*data-slot="(\w)"[^>]*?(?:data-act="(\w+)")?[^>]*>', btns)


    def test_button_column_has_a_fixed_width(self):
        m = re.search(r"\.sendc2\{([^}]*)\}", FLAT)
        self.assertIsNotNone(m)
        self.assertRegex(m.group(1), r"(^|;)width:\d+px")
        self.assertRegex(FLAT, r"\.racts\{[^}]*grid-template-columns:\d+px\d+px")




class N2FailedSurvivesRefresh(unittest.TestCase):
    """A no-reason send failure is kept in setup-bots.json (0600) until a send succeeds."""

    def setUp(self):
        ga = base.ga
        ga._SETUP_INFLIGHT.clear()
        self.ga = ga

    def _send(self, doc, ok):
        return self.ga.setup_send(doc, "otto", known=base.KNOWN, main_slug="juniper", main_name="Juniper",
                                  main_outdated=False, now=1000, poster=lambda m: ok, name="Otto",
                                  wake="Hey Otto", computer="Office computer")

    def test_mark_unknown_is_returned_and_cleared_by_a_good_send(self):
        ga = self.ga
        doc = {"rows": {}, "hint": None}
        code, body = ga.setup_mark_unknown(doc, "otto", known=base.KNOWN, now=1000, name="Otto")
        self.assertEqual(code, 200)
        self.assertIs(body["setup"]["rows"]["otto"]["unknown"], True)
        self.assertEqual(body["setup"]["rows"]["otto"]["status"], "not_set_up")
        code, body = self._send(doc, True)
        self.assertEqual(code, 200)
        self.assertIs(body["setup"]["rows"]["otto"]["unknown"], False)

    def test_failure_with_a_reason_replaces_unknown(self):
        ga = self.ga
        doc = {"rows": {}, "hint": None}
        ga.setup_mark_unknown(doc, "otto", known=base.KNOWN, now=1000, name="Otto")
        code, body = self._send(doc, False)
        self.assertEqual(code, 502)
        self.assertIs(body["setup"]["rows"]["otto"]["unknown"], False)

    def test_sent_or_connected_wins_and_bad_input_is_refused(self):
        ga = self.ga
        doc = {"rows": {"otto": {"status": "sent", "sent_at": 900}}, "hint": None}
        code, body = ga.setup_mark_unknown(doc, "otto", known=base.KNOWN, now=1000)
        self.assertEqual(code, 200)
        self.assertIs(body["setup"]["rows"]["otto"]["unknown"], False)
        self.assertEqual(ga.setup_mark_unknown(doc, "nobody", known=base.KNOWN, now=1000)[0], 404)
        self.assertEqual(ga.setup_mark_unknown(doc, "../x", known=base.KNOWN, now=1000)[0], 404)

    def test_file_round_trip_is_validated_and_0600(self):
        ga = self.ga
        target = ga.path(ga.SETUP_FILE)
        saved = open(target).read() if os.path.exists(target) else None
        try:
            ga.setup_store({"rows": {"otto": {"status": "not_set_up", "unknown": True},
                                     "ivy": {"status": "not_set_up", "unknown": "yes"}}, "hint": None})
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
            got = ga.setup_load()
            self.assertIs(got["rows"]["otto"]["unknown"], True)
            self.assertIs(got["rows"]["ivy"]["unknown"], False)
            view = ga.setup_public(got, 1000)
            self.assertIs(view["rows"]["otto"]["unknown"], True)
        finally:
            if saved is None:
                os.path.exists(target) and os.remove(target)
            else:
                ga.setup_store(json.loads(saved))

    def test_route_takes_only_a_slug(self):
        src = base.ADMIN
        self.assertIn('if p == "/api/bots/setup-unknown":', src)
        i = src.index("def bots_setup_unknown(")
        self.assertIn('setup_check_body(j, ("slug",))', src[i:i + 400])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_refresh_keeps_the_failed_row_until_a_send_succeeds(self):
        prev = {"otto": {"status": "not_set_up", "unknown": True, "name": "Otto"}}
        kept = _run("setupKeepUnknown(%s, {otto: {status: 'not_set_up', unknown: false}})" % json.dumps(prev))
        self.assertIs(kept["otto"]["unknown"], True)
        gone = _run("setupKeepUnknown(%s, {})" % json.dumps(prev))
        self.assertIs(gone["otto"]["unknown"], True)
        sent = _run("setupKeepUnknown(%s, {otto: {status: 'sent', sent_at: 5}})" % json.dumps(prev))
        self.assertFalse(sent["otto"].get("unknown"))
        refresh = _fn("refreshBots")
        self.assertIn("setupAdopt(", refresh)
        send = _fn("setupSend")
        self.assertIn("/api/bots/setup-unknown", send)



class N5PhoneTestLinkTarget(unittest.TestCase):
    def test_every_tlnk_is_44pt_on_phone(self):
        phone = _phone_rules()
        m = re.search(r"(?:^|[{}])\.tlnk\{([^}]*)\}", phone)
        self.assertIsNotNone(m, "phone .tlnk rule (not only .steps .tlnk)")
        self.assertIn("min-height:44px", m.group(1))
        self.assertIn("min-width:44px", m.group(1))






@unittest.skipUnless(shutil.which("node"), "node not installed")
class N7MainBodyCapital(unittest.TestCase):
    def test_fallback_is_capitalised_at_sentence_start(self):
        got = _run("setupCopy('your main bot', 'Otto').mainBody")
        self.assertEqual(got, "Your main bot needs the latest station instructions before it can pass setup to other bots.")
        named = _run("setupCopy('Juniper', 'Otto')")
        self.assertTrue(named["mainBody"].startswith("Juniper needs"))
        # Mid-sentence uses stay lowercase.
        self.assertIn("what your main bot passes on", _run("setupCopy('your main bot', 'Otto').intro"))


if __name__ == "__main__":
    unittest.main()
