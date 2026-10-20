"""Admin page structure for voice, updates, and the add-bot wizard.

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
        self.assertIn('["Voice service", [["voice", "Voice"]]]', js)
        self.assertIn('["System", [["updates", "Updates"], ["system", "System"]]]', js)
        self.assertNotIn("xAI key and relay", js)
        self.assertIn('const STEPS = ["Code and password", "Wi-Fi", "xAI key", "Time and location", "Wake words", "Audio test"]', js)
        self.assertIn("xAI key", js.split("const STEPS = ", 1)[1].split("];", 1)[0])

    def test_every_input_and_select_uses_the_dark_theme(self):
        """Text, password and number inputs carry .inp and selects carry .sel (dark box, teal focus ring)."""
        import re
        html = self.js + _read(os.path.join("admin", "web", "index.html"))
        for tag in re.findall(r"<input[^>]*>", html):
            if re.search(r'type="(range|checkbox|radio|file|hidden)"', tag):
                continue
            self.assertRegex(tag, r'class="[^"]*\binp\b', tag)
        for tag in re.findall(r"<select[^>]*>", html):
            self.assertRegex(tag, r'class="[^"]*\bsel\b', tag)
        self.assertIn('id="xkv"', self.js)
        self.assertIn("--gs-teal", self.css.split(".inp:focus", 1)[1].split("}", 1)[0])

    def test_old_relay_and_update_endpoints_are_gone(self):
        self.assertNotIn("relay_token", self.js)
        self.assertNotIn("/api/system/update", self.js)
        self.assertNotIn("relayPanel", self.js)
        self.assertNotIn("xaiPanel", self.js)

    def test_no_pairing_or_relay_cards(self):
        js, css = self.js, self.css
        for gone in ("/api/relay/pair", "/api/relay/unpair", "Set up relay", "Pair the relay", "Voice stays off until a relay",
                     "This station only keeps a certificate", "fpHTML", "showFingerprint", "relay-install.sh", "Unpair relay",
                     "image digest", "10-minute healthy soak", "Update both", "Update station only", "Can't check. Update info"):
            self.assertNotIn(gone, js, gone)
        self.assertNotRegex(css, r"\.fp\{")
        relay = _fn(js, "paintRelay")
        self.assertIn("Voice: ready (local)", relay)
        self.assertIn("xAI key not set", relay)
        self.assertIn('h.xai_key ? "Ready (local)" : "xAI key not set"', js)

    def test_updates_page_single_station_card(self):
        js = self.js
        up = _fn(js, "paintUpdates")
        self.assertIn("<h2>Station</h2>", up)
        self.assertNotIn("<h2>Relay</h2>", up)
        self.assertIn('"/api/updates/apply"', up)
        self.assertIn('"/api/updates/check"', up)
        self.assertIn('"/api/updates/autocheck"', up)
        self.assertIn("Daily check", up)
        self.assertIn(">Update</button>", up)
        self.assertIn("localTime(", _fn(js, "historyHTML"))  # history in local time, not raw ISO Z

    def test_buttons_after_inputs_are_spaced(self):
        self.assertIn("form>.btn{margin-top:14px}", self.css)
        self.assertIn("form+.note{margin-top:12px}", self.css)
        self.assertIn('<div id="xke"></div><button class="btn p" id="xks">Save key</button></form>', self.js)

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

    def test_configure_drawer_can_change_voice(self):
        fn = _fn(self.js, "openBot")
        self.assertIn('<label for="bvoice">Voice</label>', fn)
        self.assertIn('id="vsave"', fn)
        self.assertIn(">Save<", fn)
        self.assertIn('voiceOptions(el._voices, bot.voice, "bvoice")', fn)
        self.assertIn("Voice saved.", fn)
        self.assertIn("refreshBots(el)", fn)
        self.assertIn('api("PATCH", `/api/bots/${encodeURIComponent(bot.slug)}`, body)', fn)
        add = _fn(self.js, "addBot")
        self.assertIn('voiceOptions(voices, "", "avoice")', add)
        self.assertIn("function voiceOptions", self.js)

    def test_no_secret_is_logged(self):
        js = self.js
        self.assertNotIn("console.log", js)
        self.assertNotIn("console.debug", js)
        self.assertNotIn("console.info", js)
        self.assertIsNone(re.search(r"console\.(?:log|debug|info|warn|error)\s*\([^)]*(?:token|key|password|secret)", js, re.I))
        self.assertIsNone(re.search(r"toast\([^)]*voice_token", js))


if __name__ == "__main__":
    unittest.main()
