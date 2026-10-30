#!/usr/bin/env python3
"""Noor rev3.7 admin UI (0.13.9 QA at 77ba820).

Pure helpers run under node; wiring is checked in the source. Each test failed on 77ba820.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()


def _fn(name):
    key = "\nfunction " + name + "("
    start = JS.index(key) if key in JS else JS.index("\nasync function " + name + "(")
    depth, j = 0, JS.index("{", start)
    for k in range(j, len(JS)):
        depth += {"{": 1, "}": -1}.get(JS[k], 0)
        if depth == 0:
            return JS[start:k + 1]
    raise AssertionError("unclosed " + name)


PRE = ("const esc = s => String(s); const ICO_KEY = '<k>', ICO_ERR = '<e>', ICO_OK = '<o>';"
       "const SETUP_STUCK_S = 300; let S = {};"
       "const setupCopy = () => ({}); const joinBotNames = a => a.join(', ');")


def _run(names, expr, setup=""):
    prog = PRE + "\n" + "\n".join(_fn(n) for n in names) + "\n" + setup + \
        "\nprocess.stdout.write(JSON.stringify(" + expr + "));"
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


NODE = unittest.skipUnless(shutil.which("node"), "node not installed")


class SessionModeLabel(unittest.TestCase):
    """A6: no "Unlocked for N min" in session mode; a timer keeps its label."""

    @NODE
    def test_label_only_with_a_timer(self):
        names = ["stepupLeft", "stepupText", "stepupOpen"]
        pre = "const GRACE_MAX_S = 120 * 60;"
        self.assertEqual(_run(names, "stepupText()", pre + "S.stepupUntil = Date.now() / 1000 + 600;"), "Unlocked for 10 min")
        self.assertEqual(_run(names, "stepupText()", pre + "S.stepupUntil = Date.now() / 1000 + 7200;"), "Unlocked for 120 min")
        far = pre + "S.stepupUntil = Date.now() / 1000 + 400 * 86400;"
        self.assertEqual(_run(names, "stepupText()", far), "")
        self.assertTrue(_run(names, "stepupOpen()", far))  # the window is still open, just unlabelled
        self.assertFalse(_run(names, "stepupOpen()", pre + "S.stepupUntil = Date.now() / 1000 - 5;"))


class NoSignOutEverywhere(unittest.TestCase):
    """Removed by the owner (2026-10-30): the sidebar Sign out is enough. A password change or reset ends other sessions."""

    def test_row_and_sheet_are_gone(self):
        for gone in ("signOutAll", "soego", "logout-all", "Sign out everywhere", "signinNote", "Signed out everywhere"):
            self.assertNotIn(gone, JS)
        for gone in (".soe", "soepop", "#lnote"):
            self.assertNotIn(gone, CSS)
        self.assertIn('<a href="#" id="out">Sign out</a>', JS)


class VoiceTip(unittest.TestCase):
    def test_phone_sheet_uses_pendpop_pattern(self):
        hdr = _fn("botsHeaderShell")
        self.assertIn('data-vtipx>Close</button>', hdr)
        body = _fn("bindVoiceTip")
        self.assertIn('wrap.id = "vtipwrap"', body)
        self.assertIn('class="scrim"', body)
        self.assertIn('pop.setAttribute("tabindex", "-1")', body)
        self.assertIn("pop.focus()", body)
        self.assertIn("trapTab(pop)", body)
        self.assertIn("e.stopPropagation()", body)  # a scrim tap does not reach the page
        self.assertIn("btn.focus()", body)
        self.assertIn("#vtipwrap{position:absolute;left:0;top:0;z-index:6;pointer-events:none}", CSS)
        self.assertIn("#vtipwrap .scrim{position:fixed;inset:0;background:rgba(0,0,0,.55)", CSS)
        self.assertIn("#vtipwrap .vtipx .btn{width:100%;min-height:44px}", CSS)
        self.assertNotIn("--vtip-top", CSS + JS)

    def test_desktop_closes_when_focus_leaves(self):
        self.assertIn("popoverDismiss(pop, btn, pop, false,", _fn("bindVoiceTip"))


class ServiceStatus(unittest.TestCase):
    @NODE
    def test_empty_state_reads_unknown(self):
        self.assertEqual(_run(["svcPill"], "svcPill('')"), '<span class="right pill warn"><i></i>unknown</span>')
        self.assertIn("unknown", _run(["svcPill"], "svcPill(null)"))
        self.assertIn("pill ok", _run(["svcPill"], "svcPill('active')"))
        self.assertIn(">failed<", _run(["svcPill"], "svcPill('failed')"))
        self.assertIn("${svcPill(v)}", _fn("pageSystem"))


class SetupListToggle(unittest.TestCase):
    NAMES = ["setupStuck", "setupNeedsOwner", "botsCalloutKind", "botsCalloutHTML"]

    @NODE
    def test_needs_owner_rule(self):
        f = lambda rows: _run(self.NAMES, "setupNeedsOwner(%s, 10000)" % json.dumps(rows))
        self.assertTrue(f([{"status": "not_set_up"}]))
        self.assertTrue(f([{"status": "failed"}]))
        self.assertTrue(f([{"status": "sent", "sent_at": 10000 - 300}]))  # stuck
        self.assertFalse(f([{"status": "sent", "sent_at": 10000 - 60}]))
        self.assertFalse(f([{"status": "connected"}, {"status": "already"}]))

    @NODE
    def test_button_label_and_aria(self):
        pend = [{"slug": "a", "name": "A"}, {"slug": "b", "name": "B"}]
        base = {"bots": [{"slug": "m"}], "pending": pend}
        html = lambda o: _run(self.NAMES, "botsCalloutHTML(%s)" % json.dumps(dict(base, **o)))
        # Noor re-check A: before any send the button only opens; once the list is open it is hidden.
        self.assertNotIn('id="cfgall"', html({"everSent": False, "listOpen": True}))
        self.assertIn("2 bots not connected", html({"everSent": False, "listOpen": True}))
        first = html({"everSent": False, "listOpen": False})
        self.assertIn('aria-expanded="false">Configure all</button>', first)
        self.assertIn('aria-controls="setuphost"', first)
        self.assertIn('aria-expanded="true">Hide list</button>', html({"everSent": True, "listOpen": True}))
        self.assertIn('aria-expanded="false">Show list</button>', html({"everSent": True, "listOpen": False}))

    def test_choice_holds_for_the_page(self):
        vis = _fn("setupListVisible")
        self.assertIn('typeof S.setupChoice === "boolean"', vis)
        self.assertIn("setupNeedsOwner(", vis)
        self.assertIn("S.setupChoice = !setupListVisible(el)", _fn("paintBotCallout"))
        self.assertNotIn("_setupOpen", JS)

    def test_recheck_styles(self):  # Noor re-check B-E
        self.assertIn("@media (max-width:767px){#cfgall{min-height:44px}}", CSS)
        self.assertIn(".btn:focus-visible{outline:2px solid var(--gs-teal);outline-offset:2px;box-shadow:none}", CSS)
        self.assertIn("#out{display:inline-block;padding:4px 0;min-height:24px}", CSS)
        self.assertIn(".vtippop{right:0;left:auto;max-width:min(240px,calc(100vw - 32px))}", CSS)
        self.assertIn("if (!setupEverSent(el)) openConfigureAll(el);", _fn("paintBotCallout"))

    def test_unused_setup_message_removed(self):  # Nix (a)
        self.assertIsNone(re.search(r"\nfunction setupMessage\(", JS))


if __name__ == "__main__":
    unittest.main()
