"""0.12.7: Bots team tip. No main bot, before an import, and after an import."""
import json, os, re, shutil, subprocess, sys, unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "relay"))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()
PHRASE = re.search(r'const TEAM_PHRASE = "([^"]+)";', JS).group(1)
UPDATE = re.search(r'const TEAM_UPDATE_PHRASE = "([^"]+)";', JS).group(1)


def _fn(name):
    i = JS.index("function " + name + "(")
    j = JS.index("\n}\n", i) + 3
    return JS[i:j]


class TeamPhrase(unittest.TestCase):
    def test_phrases_are_station_voice_phrases(self):
        import relay
        self.assertEqual(PHRASE, "Hey Grok, import the rest of my team")  # 0.12.10
        self.assertEqual(UPDATE, "Hey Grok, update my team list")
        for said in (PHRASE, PHRASE + ".", UPDATE, UPDATE + ".", "import the rest of my team"):
            self.assertTrue(relay.import_team_request(said), said)

    def test_no_typed_chat_phrase_in_templates(self):
        for f in ("routine-home.txt", "routine-cloud.txt"):
            body = open(os.path.join(ROOT, "admin", f)).read()
            self.assertNotIn("Set up the rest of my team", body)
        self.assertNotIn("Set up the rest of my team", JS)

    def test_card_markup_and_css(self):
        bsum = JS.index('class="bsumrow')
        card_at = JS.index('class="card teamtip" id="teamcard"')
        sheet = JS.index('class="card botsheet"')
        self.assertLess(bsum, card_at)
        self.assertLess(card_at, sheet)
        card = JS[card_at:sheet]
        self.assertIn('id="teamphrase"', card)
        self.assertIn('class="ico teal"', card)
        self.assertNotIn("copyteam", card)
        self.assertNotIn("teamsay", JS)
        self.assertNotIn("teamnote", JS)
        self.assertNotIn("copyphrase", JS)
        self.assertIn("#teamcard.teamtip{display:flex;align-items:center;gap:10px;padding:12px 16px;"
                      "font-size:var(--fs-label);line-height:1.6;color:var(--gs-muted)}", CSS)
        # 0.13.1: the chip is gone; the wake word is an inline .wake run (tests/test_gs131_voice_examples.py).
        self.assertNotIn(".saychip{", CSS)
        self.assertNotIn(".teamsay{", CSS)
        for bad in ("mailbox", "seal"):
            self.assertNotIn(bad, _fn("teamPhraseHtml").lower())
        paint = _fn("paintTeam")
        self.assertIn('document.getElementById("add")', paint)
        self.assertIn("btn.click", paint)

    def _paint(self, bots, imported=None):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        if imported is None:
            imported = {"rows": []}
        prog = f"""
const TEAM_PHRASE = {json.dumps(PHRASE)};
const TEAM_UPDATE_PHRASE = {json.dumps(UPDATE)};
const esc = s => String(s);
function mk() {{ return {{hidden: true, innerHTML: "", textContent: ""}}; }}
const nodes = {{"#teamcard": mk(), "#teamphrase": mk()}};
const $ = sel => nodes[sel];
{_fn("teamState")}
{_fn("teamPhraseHtml")}
{_fn("paintTeam")}
paintTeam({{_bots: {json.dumps(bots)}, _imported: {json.dumps(imported)}}});
console.log(JSON.stringify({{card: nodes["#teamcard"].hidden, phrase: nodes["#teamphrase"].hidden,
  html: nodes["#teamphrase"].innerHTML}}));
"""
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_no_main_bot_offers_add(self):
        r = self._paint([], [])
        self.assertFalse(r["card"]); self.assertFalse(r["phrase"])
        self.assertEqual(r["html"], 'No bots yet. Use <button type="button" class="tipadd" id="teamadd">Add bot</button> to connect your main bot.')
        r = self._paint([{"name": "Kai"}], [])
        self.assertIn("No bots yet.", r["html"])

    def test_before_import(self):
        r = self._paint([{"name": "Rhea", "main": True}], {"rows": [], "updated_at": None})
        self.assertFalse(r["card"]); self.assertFalse(r["phrase"])
        self.assertEqual(r["html"], 'Say “<span class="wake">Hey Grok</span>, import the rest of my team.” to add your other bots.')

    def test_after_import(self):
        want = 'Added a bot? Say “<span class="wake">Hey Grok</span>, update my team list.”'
        # 0.12.10: an import that found nobody (only the main bot) still invites the import, not the update phrase
        r = self._paint([{"name": "Rhea", "main": True}], {"rows": [], "updated_at": 1710000000})
        self.assertIn("import the rest of my team", r["html"])
        r = self._paint([{"name": "Rhea", "main": True}, {"name": "Kai", "slug": "kai"}], {"rows": [], "updated_at": 1710000000})
        self.assertEqual(r["html"], want)
        r = self._paint([{"name": "Rhea", "main": True}], {"rows": [{"name": "Kai", "state": "new"}]})
        self.assertEqual(r["html"], want)


if __name__ == "__main__":
    unittest.main()
