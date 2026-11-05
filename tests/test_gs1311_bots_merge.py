#!/usr/bin/env python3
"""0.13.11 bq-0011: setup merged into the Bots list (Noor gs-bots-merge SPEC rev1.1), plus the Updates switch.

No browser. Page functions are taken from app.js and run under node. Every test fails on 0.13.10 (79fd193).
"""
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_admin  # noqa: E402,F401
import grok_admin as ga  # noqa: E402


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\(", JS)
    start = m.start()
    nxt = JS.find("\nfunction ", start + 1)
    return JS[start:nxt]


def _icons():
    out = []
    for name in ("ICO_OK", "ICO_ERR", "ICO_KEY", "ICO_DASH", "ICO_CLOCK", "ICO_PULSE", "ICO_SLASH"):
        i = JS.index("const " + name + " = ")
        out.append(JS[i:JS.index(";\n", i) + 1])
    return "\n".join(out)


FAKE_DOM = r"""
class Node { constructor(tag){ this.tagName=tag; this.children=[]; this.attrs={}; this._text=""; this.className=""; }
  setAttribute(k,v){ this.attrs[k]=String(v); } getAttribute(k){ return k in this.attrs ? this.attrs[k] : null; }
  appendChild(c){ this.children.push(c); return c; }
  set textContent(v){ this._text=String(v); this.children=[]; } get textContent(){ return this._text + this.children.map(c=>c.textContent).join(""); }
  set innerHTML(v){ throw new Error("innerHTML used"); }
  insertAdjacentHTML(){ throw new Error("insertAdjacentHTML used"); }
}
const document = { createElement: t => new Node(t) };
function dump(n){ return {tag:n.tagName, cls:n.className, attrs:n.attrs, text:n._text, kids:n.children.map(dump)}; }
"""


def _run(names, expr, dom=False):
    src = "\n".join(_fn(n) for n in names)
    prog = ("const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;')"
            ".replace(/>/g,'&gt;').replace(/\"/g,'&quot;');\n" + _icons() + "\n" + (FAKE_DOM if dom else "") +
            src + "\nprocess.stdout.write(JSON.stringify(" + expr + "));")
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


SETUP = ("clockTime", "joinBotNames", "setupCopy", "setupStuck", "setupDomId", "setupMark", "setupOwnerLabel",
         "setupSteps", "setupIcon", "setupShortCommit", "setupShortHash", "setupShortPin", "setupPanelValues",
         "setupRowModel", "setupPanelId", "setupRowNeeds", "setupRowActionHTML", "setupRowPanelHTML", "setupRowStatus",
         "setupResendWaitMin", "setupSentHelper", "setupRowStatusHTML", "setupThrottleLine")
OTTO = {"slug": "otto", "name": "Otto", "wake": "Hey Otto", "computer": "Office computer",
        "status": "not_set_up", "sent_at": 0, "fails": 0, "owner": "Sam"}
OPTS = {"mainName": "Juniper", "now": 1000}


def _row(**kw):
    r = dict(OTTO)
    r.update(kw)
    return r


class SectionAndCalloutGone(unittest.TestCase):
    def test_no_separate_set_up_bots_section(self):
        self.assertNotIn('id="setuphost"', JS)
        self.assertNotIn("<h2>Set up bots</h2>", JS)
        self.assertNotIn("\nfunction setupListHTML(", JS)

    def test_no_not_connected_callout(self):
        names = ("joinBotNames", "setupCopy", "botsCalloutKind", "botsCalloutHTML")
        pend = [{"slug": "otto", "name": "Otto"}, {"slug": "pip", "name": "Pip"}]
        for p in (pend[:1], pend):
            got = _run(names, "[botsCalloutKind(%s), botsCalloutHTML(%s)]" % (
                json.dumps({"bots": [{"slug": "j"}], "pending": p}), json.dumps({"bots": [{"slug": "j"}], "pending": p})))
            self.assertEqual(got[0], "none")
            self.assertEqual(got[1], "")
        self.assertNotIn('" not connected"', JS)
        self.assertNotIn('id="cfgall"', JS)


class RowSetup(unittest.TestCase):
    def test_set_up_button_is_the_row_action_and_coloured(self):
        a = _run(SETUP, "setupRowActionHTML(%s, %s)" % (json.dumps(_row()), json.dumps(dict(OPTS, expanded=False))))
        self.assertIn("btn-setup--needs", a)
        self.assertIn('aria-expanded="false"', a)
        self.assertIn('aria-controls="setp-otto"', a)
        self.assertIn("Set up \u25be", a)
        b = _run(SETUP, "setupRowActionHTML(%s, %s)" % (json.dumps(_row()), json.dumps(dict(OPTS, expanded=True))))
        self.assertIn('aria-expanded="true"', b)
        self.assertIn("Set up \u25b4", b)
        f = _run(SETUP, "setupRowActionHTML(%s, %s)" % (json.dumps(_row(status="failed", fails=1)), json.dumps(OPTS)))
        self.assertIn("btn-setup--needs", f)

    def test_panel_shows_the_real_steps_and_send_setup(self):
        p = _run(SETUP, "setupRowPanelHTML(%s, %s)" % (json.dumps(_row()), json.dumps(OPTS)))
        self.assertIn('role="region"', p)
        self.assertIn('aria-label="Set up Otto"', p)
        self.assertIn('id="setp-otto"', p)
        self.assertIn("Set up Otto through Juniper", p)  # 0.13.17 Confirm and send
        self.assertIn("certificate pin and public key", p)
        self.assertIn("Nothing goes out until you press Confirm and send. Juniper then asks Otto to set itself up.", p)
        self.assertNotIn("What Juniper sends Otto", p)
        self.assertNotIn("You see this exact message before it goes", p)
        self.assertIn('data-act="send"', p)
        self.assertIn(">Confirm and send<", p)

    def test_error_panel(self):
        p = _run(SETUP, "setupRowPanelHTML(%s, %s)" % (json.dumps(_row(status="failed", fails=1)), json.dumps(OPTS)))
        self.assertIn("Couldn't send.", p)
        self.assertIn("Juniper didn't answer. Check Juniper is online, then try again.", p)
        self.assertIn(">Try again<", p)
        self.assertIn("setpanel is-err", p)

    def test_sent_and_connected_rows(self):
        st = _run(SETUP, "setupRowStatus(%s, %s)" % (json.dumps(_row(status="sent", sent_at=900)), json.dumps(OPTS)))
        self.assertEqual(st["text"], "Setup request sent to Juniper \u00b7 waiting for its reply")  # 0.13.16 design §6 {main}
        a = _run(SETUP, "setupRowActionHTML(%s, %s)" % (json.dumps(_row(status="sent", sent_at=900)), json.dumps(OPTS)))
        self.assertIn(">Send again in 4 min<", a)  # 0.13.15 Noor QA: sent 100 s ago, countdown in the button
        self.assertNotIn("btn-setup--needs", a)
        for s in ("connected", "already"):
            a = _run(SETUP, "setupRowActionHTML(%s, %s)" % (json.dumps(_row(status=s)), json.dumps(OPTS)))
            self.assertEqual(a, "")
        n = _run(SETUP, "setupRowStatus(%s, %s)" % (json.dumps(_row()), json.dumps(OPTS)))
        self.assertEqual(n["text"], "Not connected")

    def test_css_rev11(self):
        self.assertIn(".btn.btn-setup--needs{", CSS)
        for tok in ("--teal-hover:#62DCC4", "--teal-active:#30B89E"):
            self.assertIn(tok, CSS)

    def test_setup_never_sends_on_its_own(self):
        # One caller of the send endpoint, and setupSend runs only from a click handler.
        self.assertEqual(JS.count('"/api/bots/setup-send"'), 1)
        calls = [m.start() for m in re.finditer(r"\bsetupSend\(", JS)]
        defs = [m.start() for m in re.finditer(r"function setupSend\(", JS)]
        others = [c for c in calls if c - len("function ") not in defs]
        self.assertTrue(others)
        for c in others:
            line = JS[JS.rfind("\n", 0, c):JS.find("\n", c)]
            self.assertIn(".onclick", line)
        for fn in ("paintBots", "refreshBots", "paintSetupList", "paintBotCallout"):
            self.assertNotIn("setupSend(", _fn(fn))


class UpdatesSwitch(unittest.TestCase):
    NAMES = ("updatesRuleLine", "updatesSwitchEl")

    def test_switch_is_built_without_innerhtml(self):
        src = _fn("updatesSwitchEl") + _fn("updatesRuleLine")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(bad, src)
        b = {"slug": "otto", "name": "Otto", "feed_updates": {"on": True, "rule": "<img src=x onerror=alert(1)>"}}
        got = _run(self.NAMES, "dump(updatesSwitchEl(%s))" % json.dumps(b), dom=True)
        sw = got["kids"][0]  # 0.13.14: one button holds the track, the word and the hidden bot name
        self.assertEqual(sw["tag"], "button")
        self.assertEqual(sw["attrs"]["role"], "switch")
        self.assertEqual(sw["attrs"]["aria-checked"], "true")
        self.assertEqual(sw["attrs"]["data-updsw"], "otto")
        flat = json.dumps(got)
        self.assertIn("Updates", flat)
        self.assertEqual(sw["kids"][1]["text"], "Updates")
        self.assertEqual(got["kids"][1]["text"], "")  # 0.13.15 rev3: no caption at rest, so the bot rule is never shown

    def test_rule_lines(self):
        got = _run(self.NAMES, "[updatesRuleLine({on:false}), updatesRuleLine({on:true, rule:''}), "
                   "updatesRuleLine({on:true, rule:'Only Done and Needs you'}), updatesRuleLine(null)]")
        self.assertEqual(got, ["Updates off", "All updates", "Only Done and Needs you", "Updates off"])  # 0.13.14

    def test_admin_passes_the_switch_state(self):
        base = {"slug": "otto", "name": "Otto", "webhook": {}, "status": {}}
        on = ga.safe_bot(dict(base, feed_updates={"on": True, "rule": "Only Done"}))
        self.assertEqual(on["feed_updates"], {"on": True, "rule": "Only Done"})
        off = ga.safe_bot(dict(base, feed_updates={"on": "yes", "rule": 5}))
        self.assertEqual(off["feed_updates"], {"on": False, "rule": ""})
        self.assertEqual(ga.safe_bot(base)["feed_updates"], {"on": False, "rule": ""})
        self.assertEqual(ga.safe_bot(dict(base, feed_updates={"on": True, "rule": "x" * 61}))["feed_updates"]["rule"], "")

    def test_switch_sits_right_of_configure(self):
        row = _fn("paintBots")
        self.assertIn("updatesSwitchEl(", JS)
        # 0.13.15 rev3: Configure is the last DOM child, after the Updates slot
        self.assertRegex(row, r'(?s)<span class="updslot".*?data-bot="\$\{esc\(b\.slug\)\}">Configure</button></div></div>')


if __name__ == "__main__":
    unittest.main()
