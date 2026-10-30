#!/usr/bin/env python3
"""Noor rev3.7 admin UI (0.13.9 QA at 77ba820) and the Sign out everywhere row (Nix B3).

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


class SignOutEverywhereUI(unittest.TestCase):
    @NODE
    def test_row_and_sheet_copy(self):
        row = _run(["signOutAllHTML"], "signOutAllHTML()")
        self.assertIn("Ends every admin sign-in, including this one. You'll need your password to get back in.", row)
        self.assertIn('id="soego" type="button">Sign out everywhere\u2026</button>', row)
        sheet = _run(["signOutAllSheetHTML"], "signOutAllSheetHTML()")
        self.assertIn(">Sign out of every device?<", sheet)
        self.assertIn("Every browser signed in to this station's admin page will need the password again.", sheet)
        self.assertIn('data-close>Cancel</button>', sheet)
        self.assertIn('class="btn soeok" type="button" data-soe>Sign out everywhere</button>', sheet)
        self.assertIn('class="soeerr" id="soee"', sheet)  # reserved space for the error

    def test_wiring_and_styles(self):
        body = _fn("openSignOutAll")
        self.assertIn('wrap.id = "pendpop"', body)
        self.assertIn('api("POST", "/api/logout-all"', body)
        self.assertIn('"Signing out\\u2026"', body)
        self.assertIn("go.disabled = true", body)
        self.assertIn("Couldn't sign out. Try again.", body)
        self.assertIn("ICO_ERR", body)
        self.assertIn('S.signinNote = "Signed out everywhere."', body)
        self.assertIn("cancel.focus()", body)  # Cancel has default focus
        self.assertIn('ev.key === "Escape"', body)
        self.assertIn("btn.focus()", body)  # Esc or Cancel returns focus to the row button
        self.assertIn("trapTab(pop)", body)
        sysp = _fn("pageSystem")
        sec = sysp[sysp.index('sysGroup("g-sec"'):sysp.index('sysGroup("g-trouble"')]
        self.assertTrue(sec.rstrip().endswith("${signOutAllHTML()}`, false) +"), sec[-120:])
        self.assertIn('openSignOutAll(soe)', sysp)
        self.assertIn("S.signinNote", _fn("loginScreen"))
        self.assertNotIn("Log out", _fn("botsHeaderShell"))
        self.assertIn(".soebtn{min-height:32px;height:32px}", CSS)
        self.assertIn(".soebtn:focus-visible,.soeok:focus-visible{outline:2px solid var(--gs-teal)", CSS)
        self.assertIn(".soeok{border:1px solid var(--gs-amber)", CSS)
        self.assertIn(".soebtn{width:100%;height:44px;min-height:44px}", CSS)
        self.assertIn(".soeerr{min-height:22px", CSS)


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
