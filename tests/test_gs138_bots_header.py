#!/usr/bin/env python3
"""Bots header + callout (gs138). Configure-all visibility follows rev3, not section 5's main-bot gate.

No browser. The pure functions are taken from app.js and run under node.
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
    start = JS.index("\nfunction " + name + "(")
    nxt = JS.find("\nfunction ", start + 1)
    return JS[start:nxt]


def _icons():
    lines = []
    for name in ("ICO_OK", "ICO_ERR", "ICO_KEY", "ICO_DOTS"):
        i = JS.index("const " + name + " = ")
        lines.append(JS[i:JS.index(";\n", i) + 1])
    return "\n".join(lines)


def _run(expr, extra=""):
    src = "\n".join(_fn(n) for n in (
        "joinBotNames", "botsCalloutKind", "botsQuietHidden", "botsHeaderShell",
        "botsCalloutHTML", "revokeAllCopy", "openConfigureAll"))
    # openConfigureAll calls setupUnconnected / openSetupBots; the expression stubs them when it needs to.
    prog = "const esc = s => String(s == null ? '' : s);\n" + _icons() + "\n" + extra + "\n" + src + \
        "\nprocess.stdout.write(JSON.stringify(" + expr + "));"
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _opts(bots, pending=None, behind=None):
    return {"bots": bots, "pending": pending or [], "behind": behind or []}


@unittest.skipUnless(shutil.which("node"), "node not installed")
class BotsHeader(unittest.TestCase):
    def test_rev3_visibility_update_wins(self):
        bots = [{"slug": "juniper", "name": "Juniper"}]
        pending = [{"name": "Otto"}, {"name": "Pip"}]
        behind = [{"slug": "juniper", "name": "Juniper"}]
        self.assertEqual(_run("botsCalloutKind(%s)" % json.dumps(_opts([]))), "empty")
        self.assertEqual(_run("botsCalloutKind(%s)" % json.dumps(_opts(bots))), "none")
        self.assertEqual(_run("botsCalloutKind(%s)" % json.dumps(_opts(bots, pending))), "configure")
        self.assertEqual(_run("botsCalloutKind(%s)" % json.dumps(_opts(bots, [{"name": "Otto"}]))), "setup")
        # A main bot is not required (rev3). Section 5 would hide this; we do not.
        self.assertEqual(_run("botsCalloutKind(%s)" % json.dumps(
            _opts([{"slug": "otto", "name": "Otto", "main": False}], [{"name": "Pip"}]))), "setup")
        self.assertEqual(_run("botsCalloutKind(%s)" % json.dumps(_opts(bots, pending, behind))), "update")

    def test_callout_copy(self):
        empty = _run("botsCalloutHTML(%s)" % json.dumps(_opts([])))
        self.assertIn("Add your main bot first", empty)
        self.assertIn("You set it up by hand once. Then it can set up the rest.", empty)
        self.assertNotIn("<button", empty)
        upd = _run("botsCalloutHTML(%s)" % json.dumps(_opts(
            [{"slug": "a", "name": "Juniper"}], [],
            [{"name": "Juniper"}, {"name": "Otto"}, {"name": "Pip"}])))
        self.assertIn("3 bots need an update", upd)
        self.assertIn("Juniper, Otto and Pip · Station routine is out of date. Updates keep their settings.", upd)
        self.assertIn(">Update all (3)<", upd)
        self.assertIn('class="callout amb"', upd)
        cfg = _run("botsCalloutHTML(%s)" % json.dumps(_opts(
            [{"slug": "j", "name": "Juniper"}],
            [{"name": "Otto"}, {"name": "Pip"}, {"name": "Maple"}])))
        self.assertIn("3 bots not connected", cfg)
        self.assertIn("Your main bot can send each one its setup message. You review it first.", cfg)
        self.assertIn(">Configure all<", cfg)
        self.assertIn('id="cfgall"', cfg)
        one = _run("botsCalloutHTML(%s)" % json.dumps(_opts(
            [{"slug": "j", "name": "Juniper"}], [{"name": "Otto"}])))
        self.assertIn("1 bot not connected", one)
        self.assertIn(">Set up Otto<", one)
        self.assertEqual(_run("botsCalloutHTML(%s)" % json.dumps(_opts([{"slug": "j", "name": "Juniper"}]))), "")

    def test_header_shell_and_phone_quiet_line(self):
        html = _run("botsHeaderShell()")
        self.assertIn("All bots up to date", html)
        self.assertIn('id="botqs"', html)
        self.assertIn('aria-label="More bot actions"', html)
        self.assertIn(">Add bot</button>", html)
        self.assertIn("Refresh avatars", html)
        self.assertIn("Revoke all bots…", html)
        self.assertLess(html.index("Refresh avatars"), html.index("Revoke all bots"))
        self.assertTrue(_run("botsQuietHidden('none', false)") is False)
        self.assertTrue(_run("botsQuietHidden('none', true)"))
        self.assertTrue(_run("botsQuietHidden('update', false)"))
        flat = re.sub(r"\s+", "", CSS)
        self.assertIn(".qs{display:none!important}", flat)
        i = flat.find(".callout.btn{width:100%;min-height:44px")
        self.assertGreater(i, 0)
        self.assertIn("@media(max-width:767px)", flat[:i])

    def test_revoke_copy_names_the_bots(self):
        got = _run("revokeAllCopy(['Juniper','Otto','Pip','Clover'])")
        self.assertEqual(got["title"], "Revoke all 4 bots?")
        self.assertEqual(got["body"], "Juniper, Otto, Pip and Clover stop replying on the station right away. "
                         "Their keys are deleted from your home computer.")
        self.assertEqual(got["help"], "To bring a bot back, connect it again from the Bots page. Grok keeps working.")
        self.assertEqual(got["action"], "Revoke all 4 bots")
        one = _run("revokeAllCopy(['Otto'])")
        self.assertEqual(one["title"], "Revoke all 1 bot?")
        self.assertIn("Otto stops replying", one["body"])

    def test_configure_all_button_opens_the_setup_list(self):
        # Retired behaviour: one pending name opened addBot, and two or more did nothing.
        # Both the Configure all callout and Set up {bot} now open the same Set up bots list.
        extra = """
        function pendingTeammates() { return PENDING; }
        function setupUnconnected() { return PENDING; }
        function openSetupBots(el) { OPENED = el; }
        let PENDING = [];
        let OPENED = null;
        """
        one = _run("((PENDING = [{name: 'Otto'}]), openConfigureAll({tag: 'one'}), OPENED)", extra)
        self.assertEqual(one, {"tag": "one"})
        two = _run("((PENDING = [{name: 'Otto'}, {name: 'Pip'}]), openConfigureAll({tag: 'two'}), OPENED)", extra)
        self.assertEqual(two, {"tag": "two"})
        src = _fn("paintBotCallout")
        self.assertIn("openConfigureAll(el)", src)
        self.assertIn('id="cfgall"', _fn("botsCalloutHTML"))


if __name__ == "__main__":
    unittest.main()
