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
        self.assertIn('api("POST", path, {})', up)  # check and apply send no password
        self.assertNotIn("Enter the admin password. The station pulls", up)
        self.assertNotIn("Enter the admin password to look for a newer version", up)
        self.assertNotIn("withPassword", up.split('$("#auto"')[0])  # those two buttons, not the daily toggle
        self.assertIn("Enter the admin password to change the daily check", up)
        self.assertIn("Update available:", up)
        self.assertIn("ver-new", up)
        self.assertIn("uptodate", up)
        self.assertIn('class="spin"', up)
        self.assertIn("Reconnecting…", up)
        self.assertNotIn("job.log", up)
        self.assertNotIn("<pre", up)
        self.assertIn(".banner.up", self.css)
        self.assertIn(".uptodate", self.css)
        self.assertIn(".ver-new", self.css)
        self.assertIn(".sha", self.css)
        self.assertIn("localTime(", _fn(js, "historyHTML"))  # history in local time, not raw ISO Z

    def test_key_status_then_replace(self):
        f = _fn(self.js, "xaiKeyPanel")
        for t in ("Key set", "ending ••••", "Replace key", "Cancel", "st.set && !replacing ? status : form"):
            self.assertIn(t, f)

    def test_login_is_password_only(self):
        login = _fn(self.js, "loginScreen")
        self.assertNotIn("Username", login)
        self.assertNotIn('autocomplete="username"', login)
        self.assertIn('{password: $("#p").value}', login)

    def test_buttons_after_inputs_are_spaced(self):
        self.assertIn("form>.btn{margin-top:14px}", self.css)
        self.assertIn("form+.note{margin-top:12px}", self.css)
        self.assertIn('<div id="xke"></div><div class="inline" style="margin-top:14px"><button class="btn p" id="xks">Save key</button>', self.js)

    def test_add_bot_wizard_and_connect_webhook(self):
        js = self.js
        self.assertIn("1 Bot · 2 Pair keys · 3 Webhook · 4 Done", js)
        self.assertIn("Skip for now", js)
        self.assertIn(">Connect webhook<", js)
        self.assertIn("function connectWebhookWizard(bot, el)", js)
        self.assertNotIn("I've saved it", js)
        self.assertNotIn("It won't be shown again.", js)
        self.assertNotIn("Authorization: Bearer <voice token>", js)
        self.assertNotIn("function dropToken", js)
        self.assertNotIn("function showRoutine", js)
        self.assertNotIn("function routineText", js)
        self.assertNotIn("node.textContent = token", js)
        self.assertIn("Send test ping", js)
        self.assertIn(">Test relay<", js)
        self.assertIn(">Webhook URL<", js)
        self.assertIn(">Key<", js)
        self.assertIn("Not connected yet - use Connect webhook on the Bots page", js)
        self.assertIn("The station sends the key as the", js)
        self.assertNotIn("${esc(token)}", js)
        self.assertNotIn("${token}", js)
        self.assertNotIn("${key}", js)
        self.assertNotIn("${esc(key)}", js)
        self.assertIn("function httpsWebhook", js)
        self.assertIn("function pubKeyOk", js)
        self.assertIn("Station public key", js)
        self.assertIn("Bot public key", js)
        self.assertIn(">Pair key<", js)
        self.assertIn("Revoke all bots", js)
        self.assertIn("Avg round trip", js)
        self.assertIn("No round trips yet", js)
        self.assertIn("Overall · ", js)
        self.assertIn("Never port-forward", js)
        self.assertIn("All bots are revoked. Pair a key again to accept replies.", js)
        self.assertIn("Base64, 32 bytes", js)
        self.assertIn("From grok-station-reply keygen. The private key stays on the bot.", js)
        self.assertIn("Gives up after 30 seconds.", js)
        self.assertNotIn("No answer after 2 min", js)
        self.assertNotIn("Gives up after 2 min", js)
        hook = _fn(js, "mountWebhookStep")
        self.assertIn('type="password"', hook)
        self.assertIn("/webhook", hook)
        self.assertIn("/test", hook)

    def test_display_dimming_uses_amber_attention(self):
        js, css = self.js, self.css
        self.assertIn("auto-sensor", js)
        self.assertIn("Needs attention", js)
        self.assertIn("isn't reporting a reading", js)
        self.assertIn("dim-overlay", js)
        self.assertIn("GROK_STATION_DIM_MODE", js)
        self.assertNotIn("isn't adjustable", js)
        self.assertIn(".nightprev", css)
        self.assertIn(".dim-overlay", css)
        self.assertIn("#9A7752", css)
        self.assertIn("#5E4934", css)
        panel = js.split("async function displayPanel", 1)[1].split("async function xaiKeyPanel", 1)[0]
        self.assertNotIn("errBox", panel)

    def test_configure_drawer_can_change_voice(self):
        fn = _fn(self.js, "openBot")
        self.assertIn('<label for="bvoice">Voice</label>', fn)
        self.assertIn('id="vsave"', fn)
        self.assertIn(">Save<", fn)
        self.assertIn('voiceOptions(el._voices, bot.voice, "bvoice")', fn)
        self.assertIn("Voice saved.", fn)
        self.assertIn("refreshBots(el)", fn)
        self.assertIn('api("PATCH", `/api/bots/${encodeURIComponent(bot.slug)}`, body)', fn)
        add = self.js.split("function addBot", 1)[1].split("\nasync function pageBots", 1)[0]
        self.assertIn('voiceOptions(voices, "", "avoice", stationVoiceLabel(el))', add)
        self.assertIn("function voiceOptions", self.js)
        self.assertIn("Station default", _fn(self.js, "stationVoiceLabel"))
        x = _fn(self.js, "dialogX")
        self.assertIn('aria-label="Close"', x)
        self.assertIn('type="button"', x)
        self.assertIn("dialogX()", add)
        self.assertIn("bindSheet(d, shut)", add)
        self.assertNotIn(">Cancel<", add)
        self.assertIn("keyFingerprint(draft.pub)", add)
        self.assertIn(">Add bot<", add)
        self.assertIn('api("POST", "/api/bots", body)', add)
        self.assertIn("/key", add)
        self.assertIn("/webhook", add)
        step1 = add.split("const step1", 1)[1]
        self.assertIn(">Next<", step1)
        self.assertNotIn('type="password"', step1)
        self.assertNotIn('id="apw"', step1)
        self.assertIn('id="apw"', add.split("const step4", 1)[1].split("const step3", 1)[0])
        rm = _fn(self.js, "confirmRemove")
        self.assertIn("dialogX()", rm)
        self.assertIn("bindSheet(d, close)", rm)
        self.assertNotIn(">Cancel<", rm)
        self.assertIn("stepupOpen()", rm)
        self.assertIn('type="password"', rm)
        self.assertIn("DELETE", rm)
        sysp = _fn(self.js, "pageSystem")
        self.assertIn("Password grace period", sysp)
        self.assertIn("/api/system/password-grace", sysp)
        self.assertIn("askPassword(", sysp)
        self.assertNotIn("withPassword(", sysp)
        self.assertIn("data-vsel", _fn(self.js, "paintBots"))

    def test_no_secret_is_logged(self):
        js = self.js
        self.assertNotIn("console.log", js)
        self.assertNotIn("console.debug", js)
        self.assertNotIn("console.info", js)
        self.assertIsNone(re.search(r"console\.(?:log|debug|info|warn|error)\s*\([^)]*(?:token|key|password|secret)", js, re.I))
        self.assertIsNone(re.search(r"toast\([^)]*voice_token", js))


if __name__ == "__main__":
    unittest.main()
