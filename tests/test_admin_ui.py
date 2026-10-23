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
        page = _read(os.path.join("admin", "web", "index.html"))
        self.assertLess(page.find('src="/deploy-pins.js"'), page.find('src="/app.js"'))
        self.assertIn("admin/web/deploy-pins.js", _read(os.path.join("tools", "install-files.sh")))
        html = self.js + page
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
        self.assertNotIn("<pre", up.split("const showLog")[0])  # the log only appears behind View update log (0.8.12)
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
        self.assertIn("1 Bot · 2 Connect · 3 Webhook · 4 Done", js)
        self.assertNotIn("Pair keys", js)
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
        self.assertNotIn("Never open the admin port", js)
        self.assertNotIn("Never port-forward", js)
        self.assertNotIn("The bot seals replies", js)
        self.assertIn("All bots are revoked. Pair a key again to accept replies.", js)
        self.assertIn("Base64, 32 bytes", js)
        self.assertNotIn("function keyNote", js)
        self.assertNotIn("From grok-station-reply keygen.", js)
        self.assertNotIn("From gs-seal-setup keygen", js)
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

    def test_main_bot_sorts_first_and_gold_star(self):
        import json, subprocess, shutil
        fn = _fn(self.js, "mainFirst")
        self.assertIn("mainFirst(", _fn(self.js, "paintBots"))
        css = _read(os.path.join("admin", "web", "admin.css"))
        self.assertIn("--gs-gold:", css)
        self.assertIn(".mainstar{color:var(--gs-gold)}", css)
        if not shutil.which("node"):
            self.skipTest("node not installed")
        bots = [{"slug": "a"}, {"slug": "b"}, {"slug": "c", "main": True}, {"slug": "d"}]
        out = subprocess.run(["node", "-e", fn + "\nconsole.log(JSON.stringify(mainFirst(" + json.dumps(bots) + ").map(b => b.slug)))"],
                             capture_output=True, text=True, timeout=20, check=True).stdout
        self.assertEqual(json.loads(out), ["c", "a", "b", "d"])
        out = subprocess.run(["node", "-e", fn + "\nconsole.log(JSON.stringify(mainFirst([{slug:'x'},{slug:'y'}]).map(b => b.slug)))"],
                             capture_output=True, text=True, timeout=20, check=True).stdout
        self.assertEqual(json.loads(out), ["x", "y"])

    def test_imported_list_card(self):
        js = self.js
        self.assertNotIn("List source", js)
        self.assertNotIn('id="lsrc"', js)
        self.assertIn("b.main", _fn(js, "paintBots"))
        self.assertIn("bot.main ?", js)  # no Set as Main Bot on the current main bot
        self.assertIn('id="imported"', js)
        self.assertIn("function paintImported", js)
        for text in ("Get list now", "Already set up", "Not on the last list", ">New<", "Set up", "Dismiss",
                     "Set as Main Bot", "Main bot", "/api/bots/import", "/api/bots/list-source", "/api/bots/imported/",
                     "Needs attention", "Nothing in the list is run"):
            self.assertIn(text, js, text)
        self.assertIn("banner warn", _fn(js, "attn"))
        self.assertIn("function paintImported", js)
        add = js.split("function addBot", 1)[1].split("\nasync function pageBots", 1)[0]
        self.assertIn("preset", add)
        self.assertIn("botIdFromName(preset.name)", add)

    def test_toggles_are_instant_and_text_fields_have_save(self):
        """Toggles apply on click. Text, number, and select groups keep a Save button that starts disabled."""
        js, css = self.js, self.css
        rule = css.split(".saved{", 1)[1].split("}", 1)[0]
        self.assertIn("font-size:13px", rule)
        self.assertIn("var(--gs-teal)", rule)
        self.assertNotIn("13.5px", rule)
        self.assertIn("function markSaved", js)
        self.assertIn("All changes saved", js)
        self.assertIn('class="saved"', js)
        self.assertIn('id="allsaved"', js)
        for tag in re.findall(r"<button\b[^>]*>", js):
            if re.search(r'class="tog', tag):
                self.assertIn('role="switch"', tag, tag)
                self.assertNotIn("Save", tag, tag)
        self.assertNotIn('$("#non", el).onclick = $("#fade", el).onclick', js)
        disp = js.split("async function displayPanel", 1)[1].split("async function xaiKeyPanel", 1)[0]
        pre, post = disp.split('$("#dsave", el).onclick', 1)
        self.assertIn("GROK_STATION_NIGHT", pre)
        self.assertIn("GROK_STATION_FADE", pre)
        self.assertNotIn("GROK_STATION_FADE", post)
        wake = js.split("async function wakePanel", 1)[1].split("\nfunction enrollProgressHTML", 1)[0]
        self.assertIn("data-sens-save", wake)
        self.assertIn(">Save<", wake)
        self.assertNotIn("s.onchange = async", wake)
        bots = _fn(js, "paintBots")
        head, tail = bots.split('querySelectorAll("[data-vsave]")', 1)
        self.assertIn("data-vsave", bots)
        self.assertNotIn("saveRowVoice", head)
        self.assertIn("saveRowVoice", tail)
        def chunk(start, end):
            return js.split(start, 1)[1].split(end, 1)[0]
        groups = {
            "wifi": chunk("async function wifiPanel", "\nfunction joinForm"),
            "time": chunk("async function timePanel", "\nfunction soundLabel"),
            "audio": chunk("async function audioPanel", "\nfunction wordLabel"),
            "wake": wake,
            "display": disp,
            "system": chunk("async function pageSystem", "\nfunction pageMore"),
            "xai": chunk("async function xaiKeyPanel", "\nasync function wizard"),
        }
        for name, body in groups.items():
            self.assertTrue(">Save<" in body or "Save key" in body, name)
            self.assertNotIn(".onblur", body, name)
        self.assertIn("dataset.saved", js.split("function watchSave", 1)[1].split("function boot", 1)[0])
        self.assertIn("btn.disabled", _fn(js, "watchSave"))
        priv = chunk("async function pagePrivacy", "\nasync function pageSystem")
        self.assertLess(priv.find("setTog"), priv.find("askPassword"))
        self.assertIn("Enter the admin password to change this.", priv)
        up = chunk("async function paintUpdates", "\nasync function pageUpdates")
        auto = up.split('$("#auto"', 1)[1]
        self.assertLess(auto.find("setTog"), auto.find("withPassword"))
        self.assertIn("Enter the admin password to change the daily check", up)

    def test_add_bot_routine_matches_docs(self):
        docs = _read(os.path.join("docs", "bots.md"))
        fences = [part.split("\n```", 1)[0] for part in docs.split("```text\n")[1:]]
        js = self.js
        cloud = js.split("const BOT_ROUTINE = `", 1)[1].split("`;", 1)[0]
        home = js.split("const BOT_ROUTINE_HOME = `", 1)[1].split("`;", 1)[0]
        self.assertIn(cloud, fences)
        self.assertIn(home, fences)
        self.assertIn("const HOME_MODE = true;", js)
        self.assertEqual(js.count('value="cloud"${home ? "" : " checked"}> Bot computer</label>'), 2)
        self.assertNotIn("Cloud computer (default)", js)
        self.assertIn('keyMode: "cloud"', js)
        self.assertEqual(js.count("Home computer will be available after it's been tested."), 2)
        self.assertIn("grok-station-reply seal", cloud)
        self.assertNotIn("gs-seal-reply", cloud)
        self.assertIn("gs-seal-reply reply", home)
        self.assertNotIn("<base64 seed>", js.split("function routineForBot", 1)[1].split("function addBot", 1)[0])
        for token in ("<slug>", "<station public key>", "<station address>", "<fingerprint>"):
            self.assertIn(token, cloud, token)
        fn = js.split("function routineForBot", 1)[1].split("function addBot", 1)[0]
        self.assertNotIn("Also run grok-station-reply keygen once", fn)
        self.assertNotIn("On the home machine, once, run gs-seal-setup keygen", fn)
        self.assertNotIn("Keep the key file out of git, sync, and backups.", fn)
        self.assertIn("Keep the key file out of git, sync, and backups.", docs)
        self.assertIn("Deploy bot key setup on system running Grok Bot (one time)", fn)
        self.assertIn("Deploy bot key setup on system running Grok Bot (one time)", docs)
        self.assertIn("deployScript(", fn)
        self.assertIn("DEPLOY_PINS", fn)
        self.assertIn("stationAddress()", fn)
        self.assertIn("your own computer", fn)
        self.assertIn("home computer running Grok Bot", fn)
        self.assertIn("~/.local/share/grok-station/bin/grok-station-reply", fn)
        self.assertIn("~/.local/share/grok-station/bin/vendor-lock.txt", fn)
        self.assertIn("~/.local/share/grok-station/bin/gs-seal-setup", fn)
        self.assertIn("~/.local/share/grok-station/bin/gs-seal-reply", fn)
        self.assertIn("~/.local/share/grok-station/bin/gs_seal_common.py", fn)
        self.assertIn("Ask the user before you run this.", fn)
        self.assertIn("It is one time.", fn)
        self.assertIn("never pushes code", fn)
        self.assertIn("does not ask you to download these files again", fn)
        self.assertIn("The script:\\n", fn)
        self.assertIn("public key it printed", fn)
        self.assertIn("Never send the private key or the seed.", fn)
        self.assertIn("Replace key", fn)
        self.assertGreater(fn.rindex("Reply with the webhook URL"), fn.index("your own computer"))
        self.assertGreater(fn.rindex("Reply with the webhook URL"), fn.index("gs-seal-setup"))
        self.assertIn("webhook URL", fn)
        self.assertIn("webhook key", fn)
        self.assertIn("Reply with the webhook URL, the webhook key, and your public key.", fn)
        self.assertNotIn("Reply with the webhook URL and the webhook key.", fn)
        # Keygen lines now live in the installer routineForBot pastes, not in the old tails.
        installer = js.split("function deployScript", 1)[1].split("\nboot();", 1)[0]
        self.assertIn('"$stage/grok-station-reply" keygen', installer)
        self.assertIn("GROK_STATION_BOT_KEY", installer)
        self.assertIn('"$stage/gs-seal-setup" keygen', installer)
        self.assertIn('"$stage/gs-seal-setup" init', installer)
        self.assertNotIn("mailbox", cloud.lower())
        self.assertNotIn("mailbox", home.lower())
        self.assertNotIn("mailbox", js.lower())
        self.assertNotIn("The bot needs Grok Bot", cloud)
        self.assertNotIn("The bot needs Grok Bot", home)
        self.assertTrue(cloud.startswith("You are this station persona."))
        self.assertTrue(home.startswith("You are this station persona."))
        self.assertNotIn("Home mode is faster and depends", fn)
        self.assertNotIn("old-hostname", js)
        self.assertIn("printf '%s' \"$ANSWER\" | gs-seal-reply reply --persona <slug> --request-id \"$REQUEST_ID\"", home)
        self.assertIn("https://", js.split("function stationAddress", 1)[1].split("function routineForBot", 1)[0])
        self.assertIn('id="aroutine"', js)
        self.assertIn("<pre ", js)
        self.assertIn('data-copy="aroutine"', js)
        self.assertNotIn("<textarea", js.split("const step3", 1)[1].split("const step2", 1)[0])
        self.assertNotIn("function showRoutine", js)
        self.assertNotIn("function routineText", js)

    def test_key_location_main_bot_and_no_mailbox(self):
        js, css = self.js, self.css
        add = js.split("function addBot", 1)[1].split("\nasync function pageBots", 1)[0]
        self.assertIn("Add your main bot first.", add)
        step2 = js.split("const step2 = ", 1)[1].split("const step1 = ", 1)[0]
        self.assertIn("Connect your bot", step2)
        self.assertIn("Where the bot's key lives", step2)
        self.assertIn('name="alives"', step2)
        self.assertIn('value="cloud"', step2)
        self.assertIn('value="home"', step2)
        self.assertIn("> Bot computer</label>", step2)
        self.assertIn("> Home computer</label>", step2)
        self.assertNotIn("Replies come back faster.</span>", step2)
        self.assertNotIn("(default)", step2)
        self.assertEqual(step2.count("esc(HOME_24_7)"), 1)
        self.assertEqual(js.count("Storing the key on your home computer allows replies to come back faster. Either way, a computer on your home network must be running the Grok Bot app, and be on and reachable 24/7, for replies to reach the station."), 1)
        self.assertNotIn("old-hostname", step2.lower())
        self.assertNotIn('id="apairskip"', step2)
        self.assertNotIn("keygen", step2)
        self.assertNotIn("seal", step2.lower())
        self.assertNotIn("mailbox", step2.lower())
        self.assertNotIn("Cloud computer", step2)
        self.assertNotIn("Home machine", step2)
        step3 = js.split("const step3 = ", 1)[1].split("const readLive = ", 1)[0]
        self.assertIn("Copy the instructions to your Grok Bot, then paste what it sends back below.", step3)
        self.assertIn(">Deploy bot key setup on system running Grok Bot</div>", step3)
        self.assertIn("These instructions have your Grok Bot set up its key on the computer running Grok Bot. It asks you first, and it only happens once.", step3)
        self.assertNotIn("seal", step3.lower())
        self.assertNotIn("cortex", step3.lower())
        self.assertNotIn("mailbox", step3.lower())
        self.assertNotIn("Optional", step3)
        self.assertNotIn("Skip", step3)
        self.assertNotIn("askip", step3)
        self.assertIn(">View instructions<", step3)
        self.assertIn('>Copy instructions<', step3)
        self.assertIn('data-copy="aroutine"', step3)
        self.assertIn('<pre class="static-val" id="aroutine" hidden>', step3)
        for fid in ('id="aurl"', 'id="akey"', 'id="apub"'):
            self.assertIn(fid, step3)
        self.assertIn("pubKeyOk(pub)", step3)
        self.assertIn("draft.pub = pub", step3)
        self.assertNotIn('class="note"', step3)
        add_l = add.lower()
        for text in ("secret store", "station address is this page", "never open the admin port", "station's lan", " seed"):
            self.assertNotIn(text, add_l, text)
        self.assertNotIn("mailbox", step3.lower())
        self.assertIn("key_mode", add)
        self.assertIn("Key lives", add)
        bots = _fn(js, "paintBots")
        self.assertIn("Key on home machine", bots)
        self.assertIn("Key on cloud computer", bots)
        mode = _fn(js, "wireKeyMode")
        self.assertIn("_kmLock", mode)
        self.assertIn("needs_key", mode)
        self.assertIn("keyModeFields(bot.key_mode)", _fn(js, "openBot"))
        self.assertIn('name="blives"', _fn(js, "keyModeFields"))
        self.assertIn("attn(", mode)
        self.assertIn("Needs attention", _fn(js, "attn"))
        self.assertIn("accent-color:var(--gs-teal)", css.replace(" ", ""))
        docs = _read(os.path.join("docs", "bots.md"))
        readme = _read("README.md")
        for text in ("Add your main bot first.", "gs-seal-reply",
                     "A key exists on only one machine"):
            self.assertIn(text, docs, text)
            self.assertIn(text, readme, text)
        self.assertIn("A computer on your home network must be running the Grok Bot app, and be on and reachable 24/7, for replies to reach the station.", readme)
        self.assertNotIn("mailbox", docs.lower())
        self.assertNotIn("mailbox", readme.lower())
        self.assertNotIn("emeet", readme.lower())
        self.assertIn("docs/display.md", readme)
        self.assertNotIn("mailbox", js.lower())

    def test_0_2_0_notes_cover_wake_pins_level_and_home_key(self):
        """Release notes name the wake pins, the level line, and where a bot key lives."""
        notes = _read(os.path.join("docs", "versioning.md"))
        section = notes.split("## 0.6.0", 1)[1].split("\n## ", 1)[0]
        blurb = _read("README.md").split("**0.6.0**", 1)[1].split("\n\n", 1)[0]
        for text in ("openwakeword 0.6.0", "onnxruntime 1.31.0", "numpy 2.5.4",
                     "peak sample", "clipped=0.0012",
                     "A key exists on only one machine",
                     "Add your main bot first"):
            self.assertIn(text, section, text)
            self.assertIn(text, blurb, text)

    def test_add_bot_shows_certificate_fingerprint(self):
        step2 = self.js.split("const step2 = ", 1)[1].split("const step1 = ", 1)[0]
        self.assertNotIn("Certificate fingerprint", step2)
        self.assertNotIn("Station public key", step2)
        self.assertNotIn('id="apub"', step2)
        self.assertIn("botKeyState", self.js)
        state = _fn(self.js, "botKeyState")
        self.assertIn("public_key", state)
        self.assertIn("pubKeyOk", state)
        fields = _fn(self.js, "pairFields")
        self.assertIn("botKeyState(bot)", fields)
        self.assertIn('staticValue("bpubshow", st.key, "bot public key")', fields)
        self.assertIn("Replace key", fields)
        self.assertIn('id="bpubrepl"', fields)
        self.assertIn('id="bpubedit"', fields)
        self.assertIn('id="bpub"', fields)
        self.assertIn("Pair key", fields)
        self.assertNotIn("<input", fields.split("Bot public key", 1)[1].split('id="bpubedit"', 1)[0])
        self.assertIn("pairFields(el._stationPub, bot)", _fn(self.js, "openBot"))
        wire = _fn(self.js, "wirePair")
        self.assertIn("bpubrepl", wire)
        self.assertIn("/key", wire)
        self.assertIn("public_key: pub", wire)
        self.assertNotIn('staticValue("afp"', self.js)
        self.assertNotIn('staticValue("astapub"', self.js)
        self.assertIn('data-copy', self.js.split("function staticValue", 1)[1].split("function clearTimers", 1)[0])
        admin = _read(os.path.join("admin", "grok_admin.py"))
        self.assertIn('"fingerprint": tls_info()["fingerprint"]', admin)

    def test_audio_gain_save_omits_untouched(self):
        """Sliders show the listener default. Save sends a gain only after that slider moves."""
        audio = self.js.split("async function audioPanel", 1)[1].split("\nconst BUILTIN_TIP", 1)[0]
        self.assertNotIn('|| "1"', audio)
        self.assertNotIn("|| '1'", audio)
        self.assertIn("gain_by_device", audio)
        self.assertIn(".defaults", audio)
        self.assertIn("wakeShown", audio)
        self.assertIn("voiceShown", audio)
        dev = audio.split('$("#dev", el).addEventListener("change"', 1)[1].split("every(", 1)[0]
        self.assertIn("dataset.saved", dev)
        self.assertIn("gainFor", dev)
        save = audio.split('$("#asave", el).onclick', 1)[1].split('const r = await api("PUT", "/api/settings/audio"', 1)[0]
        head, tail = save.split("if (", 1)
        self.assertNotIn("WAKE_GAIN", head)
        self.assertNotIn("VOICE_GAIN", head)
        self.assertIn("body.WAKE_GAIN", tail)
        self.assertIn("body.VOICE_GAIN", tail)
        self.assertEqual(tail.count("dataset.saved"), 2)

    def test_no_secret_is_logged(self):
        js = self.js
        self.assertNotIn("console.log", js)
        self.assertNotIn("console.debug", js)
        self.assertNotIn("console.info", js)
        self.assertIsNone(re.search(r"console\.(?:log|debug|info|warn|error)\s*\([^)]*(?:token|key|password|secret)", js, re.I))
        self.assertIsNone(re.search(r"toast\([^)]*voice_token", js))

    def test_offline_banner_save_state_and_skeletons(self):
        """Station offline banner, Saving/Unsaved, and skeleton placeholders (design.md 8.2)."""
        js, css = self.js, self.css
        self.assertEqual(js.count('id="offline"'), 1)
        self.assertEqual(js.count("Can't reach the station. Retrying…"), 1)
        banner = re.search(r"<div id=\"offline\"[^>]*>", js)
        self.assertIsNotNone(banner)
        self.assertNotIn("button", banner.group(0))
        show = _fn(js, "showOffline")
        self.assertIn("signed_in", show)
        self.assertIn('classList.add("offline")', show)
        self.assertIn("setInterval(retryStation, 5000)", show)
        self.assertIn("offlineText()", show)
        text = _fn(js, "offlineText")
        self.assertIn("as of ", text)
        self.assertIn("toLocaleTimeString()", text)
        retry = js.split("function retryStation", 1)[1].split("async function api(", 1)[0]
        self.assertIn('api("GET", "/api/state")', retry)
        api = js.split("async function api(", 1)[1].split("async function apiRaw", 1)[0]
        self.assertIn("showOffline()", api)
        self.assertIn("netDown(r.status)", api)
        self.assertIn('path === "/api/state"', api)
        self.assertIn("hideOffline()", api)
        down = _fn(js, "netDown")
        for code in ("status === 0", "status === 502", "status === 503", "status === 504"):
            self.assertIn(code, down, code)
        shell = js.split("function shell()", 1)[1].split("window.addEventListener", 1)[0]
        self.assertIn("if (stationOffline) showOffline();", shell)

        saving = _fn(js, "markSaving")
        self.assertIn('n.textContent = "Saving…"', saving)
        self.assertIn('classList.add("saving")', saving)
        unsaved = _fn(js, "markUnsaved")
        self.assertIn('"<i></i>Unsaved changes"', unsaved)
        self.assertIn('classList.add("unsaved")', unsaved)
        saved = _fn(js, "markSaved")
        self.assertIn("All changes saved", saved)
        self.assertIn('classList.remove("saving", "unsaved")', saved)
        watch = _fn(js, "watchSave")
        self.assertIn("markUnsaved()", watch)
        self.assertIn("All changes saved", watch)
        self.assertIn("btn.disabled", watch)
        self.assertIn("dataset.saved", watch)

        # A Save that POST/PUTs and already called markSaved also marks saving, then unsaved on failure.
        for call in (
            'api("POST", "/api/network/hostname"',
            'api("PUT", "/api/updates/autocheck"',
            'api("PUT", "/api/settings/time"',
            'api("PUT", "/api/settings/audio"',
            'api("PUT", "/api/settings/display"',
            'api("PUT", "/api/cards"',
            'api("POST", "/api/xai-key"',
            'api("PUT", "/api/privacy"',
            'api("PUT", "/api/bots/list-source"',
            'api("PUT", "/api/system/password-grace"',
            'api("POST", "/api/password"',
        ):
            i = js.find(call)
            self.assertGreater(i, 0, call)
            self.assertIn("markSaving()", js[max(0, i - 400):i], call)
            self.assertIn("markUnsaved()", js[i:i + 900], call)

        self.assertNotIn('<p class="help">Loading…</p>', js)
        skel = js.split("function skel(", 1)[1].split("async function pageUpdates", 1)[0]
        self.assertIn('class="skel"', skel)
        self.assertIn('class="sr"', skel)
        self.assertIn("Loading…", skel)
        updates = js.split("async function pageUpdates", 1)[1].split("async function timePanel", 1)[0]
        home = js.split("async function pageHome", 1)[1].split("async function pageVoice", 1)[0]
        self.assertIn("skel(3)", updates)
        self.assertIn('id="updatesroot"', updates)
        self.assertIn("skel(4)", home)
        self.assertIn('id="hero"', home)

        self.assertLess(css.find(".copied{"), css.find("/* station status:"))
        block = css.split("/* station status:", 1)[1]
        self.assertNotIn("#FF", block)
        self.assertNotIn("#ff", block)
        self.assertIn("--gs-amber", block)
        self.assertIn("--gs-amber-bg", block)
        self.assertIn("--gs-lavender", block)
        self.assertIn("--gs-surface-2", block)
        self.assertIn(".main.offline .body button, .main.offline .body input, .main.offline .body select {pointer-events:none;opacity:.5}", block)
        self.assertIn(".saved.saving", block)
        self.assertIn(".saved.unsaved", block)
        self.assertIn(".skel", block)
        self.assertIn("@keyframes gs-shimmer", block)
        self.assertIn("opacity", block.split("@keyframes gs-shimmer", 1)[1].split("}", 1)[0])
        self.assertIn(".sr", block)
        # The existing reduced-motion rule turns every animation off, including the skeleton shimmer.
        motion = css.split("prefers-reduced-motion", 1)[1].split("}", 1)[0]
        self.assertIn("animation:none!important", motion)


if __name__ == "__main__":
    unittest.main()


class Release092UI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read(os.path.join("admin", "web", "app.js"))

    def test_setup_prompt_teaches_display_cards(self):
        for routine in ("BOT_ROUTINE = `", "BOT_ROUTINE_HOME = `"):
            start = self.js.index(routine)
            body = self.js[start:self.js.index("`;", start)]
            for needle in ("Display cards", '"type":"recipe"', '"type":"sports"', '"type":"calendar"', '"type":"home"',
                           '"op":"clear"', "a.espncdn.com"):
                self.assertIn(needle, body, (routine, needle))
        cloud = self.js[self.js.index("BOT_ROUTINE = `"):self.js.index("BOT_ROUTINE_HOME")]
        self.assertIn("seal-show --bot <slug>", cloud)
        self.assertIn("post --show", cloud)

    def test_copy_updated_instructions_for_paired_bots(self):
        self.assertIn("Copy updated instructions", self.js)
        fn = _fn(self.js, "routineUpdate")
        self.assertIn("BOT_ROUTINE", fn)
        self.assertNotIn("deployScript", fn)

    def test_fixed_ten_reps_train_more_and_delete(self):
        self.assertNotIn('id="cnt"', self.js)
        self.assertIn("You say it 10 times", self.js)
        self.assertIn("Train more", self.js)
        self.assertIn("topup: true", self.js)
        self.assertIn('"DELETE", "/api/wakewords/"', self.js)
        self.assertIn("w.builtin ? \"\" : `<button class=\"btn d sm\" data-del", self.js)
        self.assertNotIn("coming later", _read(os.path.join("wake", "grok_wake.py")))

    def test_logo_hosts_are_advanced_and_prefilled(self):
        self.assertIn("Advanced: team logo sources", self.js)
        self.assertIn('placeholder="a.espncdn.com"', self.js)
