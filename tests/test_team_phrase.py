"""0.12.6: Bots team card says the station phrase once a main bot is paired."""
import json, os, re, shutil, subprocess, sys, unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "relay"))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
PHRASE = re.search(r'const TEAM_PHRASE = "([^"]+)";', JS).group(1)


def _fn(name):
    i = JS.index("function " + name + "(")
    j = JS.index("\n}\n", i) + 3
    return JS[i:j]


class TeamPhrase(unittest.TestCase):
    def test_phrase_is_the_station_voice_phrase(self):
        import relay
        self.assertEqual(PHRASE, "Hey Grok, import the rest of my team")
        self.assertTrue(relay.import_team_request(PHRASE))
        self.assertTrue(relay.import_team_request(PHRASE + "."))

    def test_no_typed_chat_phrase_in_templates(self):
        for f in ("routine-home.txt", "routine-cloud.txt"):
            body = open(os.path.join(ROOT, "admin", f)).read()
            self.assertNotIn("Set up the rest of my team", body)
        self.assertNotIn("Set up the rest of my team", JS)

    def test_card_markup(self):
        i = JS.index('id="teamcard"')
        card = JS[i:JS.index('id="unusedwrap"', i)]
        self.assertNotIn("copyteam", card)
        self.assertNotIn("teamnote", JS)
        self.assertIn('class="note" id="teamphrase"', card)
        self.assertNotIn("copyphrase", JS)
        self.assertIn('.teamsay{color:var(--gs-text);font-weight:600}',
                      open(os.path.join(ROOT, "admin", "web", "admin.css")).read())
        for bad in ("mailbox", "seal"):
            self.assertNotIn(bad, _fn("teamPhraseHtml").lower())

    def _paint(self, bots, pending):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        prog = f"""
const TEAM_PHRASE = {json.dumps(PHRASE)};
const esc = s => String(s);
function mk() {{ return {{hidden: true, innerHTML: "", textContent: ""}}; }}
const nodes = {{"#teamcard": mk(), "#teamphrase": mk()}};
const $ = sel => nodes[sel];
{_fn("teamPhraseHtml")}
{_fn("paintTeam")}
paintTeam({{_bots: {json.dumps(bots)}}});
console.log(JSON.stringify({{card: nodes["#teamcard"].hidden, phrase: nodes["#teamphrase"].hidden,
  html: nodes["#teamphrase"].innerHTML}}));
"""
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_hidden_with_no_bots(self):
        r = self._paint([], [])
        self.assertTrue(r["card"]); self.assertTrue(r["phrase"])

    def test_shown_with_main_bot(self):
        r = self._paint([{"name": "Rhea", "main": True}], [])
        self.assertFalse(r["card"]); self.assertFalse(r["phrase"])
        self.assertEqual(r["html"], 'Say <strong class="teamsay">“Hey Grok, import the rest of my team.”</strong>'
                                    ' Grok Station sets up the rest of your team for you.')
        r = self._paint([{"name": "Kai"}], [])  # bots but no main bot yet
        self.assertTrue(r["card"])


if __name__ == "__main__":
    unittest.main()
