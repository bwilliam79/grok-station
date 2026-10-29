#!/usr/bin/env python3
"""WLED and Homebridge manage pages (gs138 integrations nav, rev1).

Quiet parent back link, "N lights/devices saved", de-weighted row Remove with a
confirm sheet, and a bottom danger section that still asks for the admin password.
No browser. Copy helpers are taken from app.js and run under node.
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()


def _fn(name):
    start = JS.index("\nfunction " + name + "(")
    depth, j = 0, JS.index("{", start)
    for k in range(j, len(JS)):
        depth += {"{": 1, "}": -1}.get(JS[k], 0)
        if depth == 0:
            return JS[start:k + 1]
    raise AssertionError("unclosed " + name)


def _run(expr):
    prog = _fn("savedCount") + "\n" + _fn("integrationRemoveCopy") + \
        "\nprocess.stdout.write(JSON.stringify(" + expr + "));"
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


class IntegrationsNavCopy(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_saved_count_and_remove_copy(self):
        self.assertEqual(_run("savedCount(0, 'light', 'lights')"), "No lights saved yet.")
        self.assertEqual(_run("savedCount(1, 'light', 'lights')"), "1 light saved")
        self.assertEqual(_run("savedCount(4, 'light', 'lights')"), "4 lights saved")
        self.assertEqual(_run("savedCount(3, 'device', 'devices')"), "3 devices saved")
        four = _run("integrationRemoveCopy('wled', 4)")
        self.assertEqual(four["body"], "Removes WLED and all 4 lights from the station. Voice commands for them stop working.")
        self.assertEqual(four["title"], "Remove WLED and 4 lights?")
        self.assertEqual(four["sheet"], "Voice commands for these lights stop working. Enter the admin password to confirm.")
        self.assertEqual(four["action"], "Remove WLED")
        one = _run("integrationRemoveCopy('wled', 1)")
        self.assertNotIn("all 1", one["body"])
        self.assertIn("its light", one["body"])
        zero = _run("integrationRemoveCopy('homebridge', 0)")
        self.assertNotIn("all 0", zero["body"])
        self.assertEqual(zero["title"], "Remove Homebridge?")
        hb = _run("integrationRemoveCopy('homebridge', 3)")
        self.assertEqual(hb["body"], "Removes Homebridge and all 3 devices from the station. Voice commands for them stop working.")
        self.assertEqual(hb["title"], "Remove Homebridge and 3 devices?")
        self.assertEqual(hb["action"], "Remove Homebridge")

    def test_manage_markup(self):
        page = JS.split("async function pageIntegrations", 1)[1].split("\nfunction deployScript", 1)[0]
        self.assertNotIn('class="btn g back" id="iback"', page)
        for text in (
            'class="mhead"',
            'savedCount(n, "light", "lights")',
            'savedCount(n, "device", "devices")',
            "Say: ",
            'class="dacts"',
            'class="rdiv"',
            'class="btn sm q"',
            "removeSavedSheet",
            "Couldn't remove. Try again.",
            'noun === "device" ? "device" : "light"',
            "Remove ${thing}",
            "integrationDanger",
            "Remove WLED…",
            "Remove Homebridge…",
            "Blinking…",
            "✓ Blinked",
            "⚠ Didn't respond. Check it's on.",
            "✓ Responded",
            "Checking…",
            "Offline since ",
            'finding(true)',
            'hbBackLabel = "Homebridge"',
            "wledBackLabel === \"WLED\" ? reloadWledManage : null",
            "null, true)",
        ):
            self.assertIn(text, page, text)
        # One light/device is a sheet with no password. The integration still asks.
        sheet = page.split("function removeSavedSheet", 1)[1].split("async function reloadWledManage", 1)[0]
        self.assertNotIn("password", sheet.lower())
        self.assertIn('id="rmno"', sheet)
        self.assertIn("no.focus", sheet)
        css = CSS.replace(" ", "")
        self.assertIn(".mhead{display:flex;align-items:baseline;gap:10px", css)
        self.assertIn(".btn.sm.q{background:transparent", css)
        self.assertIn(".danger{display:flex", css)
        self.assertIn("border:1pxsolid#5A4416", css)
        self.assertIn(".rdiv{display:none}", css)
        self.assertIn(".drow.btn.sm{min-height:44px;height:44px}", css)


if __name__ == "__main__":
    unittest.main()
