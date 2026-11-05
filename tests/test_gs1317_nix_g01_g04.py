"""Nix G01–G04 regressions for 0.13.18 (fail before / pass after)."""
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_admin  # noqa: F401 — sets ADMIN_* sandbox before grok_admin import
sys.path.insert(0, os.path.join(ROOT, "admin"))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import gs_routine
import grok_admin as ga
import relay as ry
import handoff

PIN_BIND = ("When template_commit and template_sha256 are present, they must equal the install commit and "
            "install-home.sh hash in these instructions")
NEVER = "never reconstruct them from knowledge, reviews, or other messages"
PINS_LINE = re.compile(
    r"Install pins for this release: commit ([0-9a-f]{40}), install-home\.sh sha256 ([0-9a-f]{64})\."
)


def setup_pins():
    return ry.setup_install_pins()


class G01CloudMainHoldsInstallLiterals(unittest.TestCase):
    def test_cloud_template_contains_setup_install_pins(self):
        c, h = setup_pins()
        self.assertTrue(c and h)
        cloud = gs_routine.TEMPLATES["cloud"]
        self.assertIn(PIN_BIND, cloud)
        m = PINS_LINE.search(cloud)
        self.assertIsNotNone(m, "cloud template missing Install pins line")
        self.assertEqual(m.group(1), c)
        self.assertEqual(m.group(2), h)
        self.assertIn(NEVER, cloud.lower())
        rc = open(os.path.join(ROOT, "admin", "routine-cloud.txt"), encoding="utf-8").read()
        self.assertEqual(rc, cloud)
        js = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
        i = js.index("const BOT_ROUTINE = String.raw`") + len("const BOT_ROUTINE = String.raw`")
        j = js.index("`;", i)
        self.assertEqual(js[i:j], cloud)


class G02HomeMainPinBind(unittest.TestCase):
    def test_home_template_has_pin_bind_and_literals(self):
        c, h = setup_pins()
        home = gs_routine.TEMPLATES["home"]
        self.assertIn(PIN_BIND, home)
        m = PINS_LINE.search(home)
        self.assertIsNotNone(m, "home template missing Install pins line")
        self.assertEqual(m.group(1), c)
        self.assertEqual(m.group(2), h)
        self.assertIn(NEVER, home.lower())
        rh = open(os.path.join(ROOT, "admin", "routine-home.txt"), encoding="utf-8").read()
        self.assertEqual(rh, home)


class G03HoldsSetupWired(unittest.TestCase):
    def test_update_only_never_attests_target_install_procedure(self):
        # Actual Update render + signed status hash: only the main routine was adopted.
        bot = {"slug": "otto", "paired": True, "key_mode": "cloud"}
        station, fp, pub = "https://station-demo.local", ":".join(["AB"] * 32), "A" * 43 + "="
        from unittest.mock import patch
        with patch.object(ga, "VERSION_FILE", os.path.join(ROOT, "VERSION")):
            body = ga.render_bot_routine(bot, station, fp, pub)
            bot["update"] = {"release": ga.version(), "instr": gs_routine.instr_hash(body)}
            held = ga.bot_holds_current_setup(bot, station, fp, pub)
        from test_gs1317_nopaste_setup import _payload
        payload = _payload("held" if held else "first")
        self.assertFalse(held, "Update only attests forwarding, not target install setup")
        self.assertIn("template_commit", payload)
        self.assertIn("template_sha256", payload)

    def test_setup_public_exposes_holds_setup(self):
        self.assertTrue(callable(getattr(ga, "bot_holds_current_setup", None)))
        doc = {"rows": {"otto": {"name": "Otto", "wake": "Hey Otto", "computer": "", "status": "not_set_up",
                                 "sent_at": 0, "fails": 0, "message": "", "unknown": False}}}
        view = ga.setup_public(doc, 1000.0, relay=None, holds={"otto": True})
        self.assertIs(view["rows"]["otto"].get("holds_setup"), True)
        view2 = ga.setup_public(doc, 1000.0, relay=None, holds={"otto": False})
        self.assertNotIn("holds_setup", view2["rows"]["otto"])

    def test_held_path_omits_pin_fields_in_payload(self):
        from test_gs1317_nopaste_setup import _payload
        body = _payload("held")
        self.assertNotIn("template_commit", body)
        self.assertNotIn("template_sha256", body)
        body2 = _payload("first")
        self.assertIn("template_commit", body2)
        self.assertIn("template_sha256", body2)

    def test_bots_setup_send_reads_live_holds_not_row_field(self):
        src = open(os.path.join(ROOT, "admin", "grok_admin.py"), encoding="utf-8").read()
        self.assertIn("bot_holds_current_setup(b, station, fp, pub)", src)
        self.assertIn('path = "held" if holds else "first"', src)
        self.assertNotIn('path = "held" if row.get("holds_setup") is True else "first"', src)


class G04PanelPinsFromSetupParams(unittest.TestCase):
    def test_panel_values_use_setup_params_not_deploy_pins(self):
        js = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
        self.assertIn("function setupInstallPins(", js)
        block = js[js.index("function setupPanelValues("):js.index("function setupPanelValues(") + 900]
        self.assertIn("setupInstallPins(", block)
        self.assertNotIn("pins.commit", block)
        c, h = setup_pins()
        from test_gs1311_bots_merge import SETUP, _run, _fn, _icons, FAKE_DOM
        import subprocess
        # Inject SETUP_PARAMS with the real install pins so setupInstallPins resolves.
        snippet = (
            "T=$(mktemp) && curl -fsS --proto '=https' --max-redirs 0 -o \"$T\" "
            f"'https://raw.githubusercontent.com/bwilliam79/grok-station/{c}/scripts/install-home.sh' "
            f"&& printf '%s  %s\\n' '{h}' \"$T\" | sha256sum -c -"
        )
        params = "Grok Station setup parameters.\n4. " + snippet + "\n"
        names = list(SETUP)
        src = "\n".join(_fn(n) for n in names)
        opts = {
            "mainName": "Juniper",
            "stationAddress": "https://station-demo.local",
            "fingerprint": "ab" * 32,
            "release": "0.13.18",
            "deployPins": {"commit": "0" * 40,
                           "files": {"scripts/install-home.sh": "1" * 64}},
        }
        prog = ("const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;')"
                ".replace(/>/g,'&gt;').replace(/\"/g,'&quot;');\n" + _icons() + "\n"
                + "const SETUP_PARAMS = " + json.dumps(params) + ";\n"
                + src + "\n"
                + "const html = setupRowPanelHTML({slug:'otto',name:'Otto',status:'not_set_up'}, "
                + json.dumps(opts) + ");\n"
                + "process.stdout.write(JSON.stringify(html));\n")
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(prog)
            path = fh.name
        try:
            out = subprocess.run(["node", path], capture_output=True, text=True)
        finally:
            os.unlink(path)
        self.assertEqual(out.returncode, 0, out.stderr)
        html = json.loads(out.stdout)
        self.assertIn(c[:7], html)
        self.assertIn(h[:8] + "\u2026", html)
        self.assertNotIn("0000000", html)


if __name__ == "__main__":
    unittest.main()
