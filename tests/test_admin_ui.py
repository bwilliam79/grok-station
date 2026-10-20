"""Admin page structure for voice relay, updates, and the add-bot wizard.

Reads admin/web/app.js and admin/web/admin.css. No browser and no network.
"""
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _fn(js, name):
    mark = "function " + name
    start = js.find(mark)
    if start < 0:
        raise AssertionError("missing " + name)
    nxt = js.find("\nfunction ", start + len(mark))
    return js[start:nxt if nxt > 0 else start + 4000]


class AdminUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read(os.path.join("admin", "web", "app.js"))
        cls.css = _read(os.path.join("admin", "web", "admin.css"))

    def test_nav_labels(self):
        js = self.js
        self.assertIn('["Voice service", [["voice", "Voice relay"]]]', js)
        self.assertIn('["System", [["updates", "Updates"], ["system", "System"]]]', js)
        self.assertNotIn("xAI key and relay", js)
        self.assertIn('const STEPS = ["Code and password", "Wi-Fi", "Voice relay", "Time and location", "Wake words", "Audio test"]', js)
        self.assertNotIn("xAI key", js.split("const STEPS = ", 1)[1].split("];", 1)[0])

    def test_old_relay_and_update_endpoints_are_gone(self):
        self.assertNotIn("relay_token", self.js)
        self.assertNotIn("/api/system/update", self.js)
        self.assertNotIn("relayPanel", self.js)
        self.assertNotIn("xaiPanel", self.js)

    def test_fingerprint_css_is_at_least_28px_and_does_not_ellipsis(self):
        css = self.css
        block = re.search(r"\.fp\{([^}]*)\}", css)
        self.assertIsNotNone(block, "missing .fp rule")
        body = block.group(1)
        size = re.search(r"font-size:\s*max\(\s*(\d+(?:\.\d+)?)px", body)
        self.assertIsNotNone(size, body)
        self.assertGreaterEqual(float(size.group(1)), 28)
        self.assertNotIn("ellipsis", body)
        self.assertIn("text-overflow:clip", body)
        groups = re.search(r"\.fp \.g\{([^}]*)\}", css)
        self.assertIsNotNone(groups, "missing .fp .g rule")
        self.assertIn("white-space:nowrap", groups.group(1))
        self.assertNotIn("ellipsis", groups.group(1))
        fp = _fn(self.js, "fpHTML")
        self.assertIn("line < 4", fp)
        self.assertIn("g < 4", fp)
        self.assertIn("idx === 0 || idx === 15", fp)
        self.assertIn('class="g end"', fp)
        self.assertIn('class="fp"', fp)

    def test_match_buttons_are_not_autofocused(self):
        self.assertNotIn("autofocus", self.js)
        fn = _fn(self.js, "showFingerprint")
        self.assertIn("They don't match · cancel", fn)
        self.assertIn(">They match<", fn)
        self.assertIn("blurMatch", fn)
        self.assertNotIn("autofocus", fn)
        self.assertIn('type="button"', fn)

    def test_relay_and_updates_copy(self):
        js = self.js
        for needle in (
            "/api/relay",
            "/api/relay/pair/start",
            "/api/relay/pair/new-code",
            "/api/relay/pair/cancel",
            "/api/relay/pair/confirm",
            "/api/relay/unpair",
            "/api/updates",
            "/api/updates/check",
            "/api/updates/autocheck",
            "/api/updates/apply",
            "https://github.com/bwilliam79/grok-station/releases/latest/download/relay-install.sh",
            "https://github.com/bwilliam79/grok-station/releases/latest/download/relay-install.ps1",
            "Expires in ",
            "Tries left ",
            "Waiting for relay…",
            "Can't reach the relay. Voice is off.",
            "Retry now",
            "Show setup command",
            "Unpair relay…",
            "grok-relay rotate-xai-key",
            "Compare with the installer and the station screen. Nothing pairs until both ends confirm.",
            "Can't check. Update info is out of date. Updates may be hidden.",
            "Relay first, then station after a 10-minute healthy soak. Updates come from the GitHub release and are checked by image digest.",
            "Update both",
            "Update station only",
            "Daily check",
            'apply("both"',
            'apply("station"',
            "pairing.code",
            "d.installed",
        ):
            self.assertIn(needle, js, needle)
        apply = _fn(js, "paintUpdates")
        self.assertNotIn("target:", apply)
        self.assertIn("{version: avail}", apply)
        setup = _fn(js, "renderNotSetup")
        self.assertIn("askPassword", setup)
        self.assertNotIn("stepupOpen", setup)
        self.assertIn('"/api/relay/pair/start"', setup)
        unpair = _fn(js, "bindUnpair")
        self.assertIn("askPassword", unpair)
        self.assertNotIn("stepupOpen", unpair)
        watch = _fn(js, "watchPairing")
        self.assertIn("2000", watch)
        self.assertIn('"/api/relay"', watch)
        key = _fn(js, "xaiKeyHTML")
        self.assertNotIn("<input", key)
        self.assertNotIn("Replace", key)
        self.assertNotIn(">Test<", key)
        self.assertIn("key_last4", key)
        updates = _fn(js, "paintUpdates")
        self.assertNotIn("signature", updates.lower())

    def test_add_bot_wizard_and_connect_webhook(self):
        js = self.js
        self.assertIn("1 Bot · 2 Voice token · 3 Webhook · 4 Done", js)
        self.assertIn("Skip for now", js)
        self.assertIn(">Connect webhook<", js)
        self.assertIn("function connectWebhookWizard(bot, el)", js)
        self.assertIn("I've saved it", js)
        self.assertIn("It won't be shown again.", js)
        self.assertIn("Send test ping", js)
        self.assertIn(">Webhook URL<", js)
        self.assertIn(">Key<", js)
        self.assertIn("Authorization: Bearer <voice token>", js)
        self.assertIn("Not connected yet - use Connect webhook on the Bots page", js)
        self.assertIn("The station sends the key as the", js)
        self.assertIn("function dropToken", js)
        self.assertIn("node.textContent = token", js)
        self.assertNotIn("${esc(token)}", js)
        self.assertNotIn("${token}", js)
        self.assertNotIn("${key}", js)
        self.assertNotIn("${esc(key)}", js)
        self.assertIn("function httpsWebhook", js)
        hook = _fn(js, "mountWebhookStep")
        self.assertIn('type="password"', hook)
        self.assertIn("/webhook", hook)
        self.assertIn("/test", hook)

    def test_no_secret_is_logged(self):
        js = self.js
        self.assertNotIn("console.log", js)
        self.assertNotIn("console.debug", js)
        self.assertNotIn("console.info", js)
        self.assertIsNone(re.search(r"console\.(?:log|debug|info|warn|error)\s*\([^)]*(?:token|key|password|secret)", js, re.I))
        self.assertIsNone(re.search(r"toast\([^)]*voice_token", js))


if __name__ == "__main__":
    unittest.main()
