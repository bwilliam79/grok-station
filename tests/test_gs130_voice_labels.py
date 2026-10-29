"""0.13.0: voice pickers show "Aurora", but the option value (what is saved and sent to xAI) stays "aurora"."""
import json, os, shutil, subprocess, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "admin", "web", "app.js")


def _fn(src, name):
    i = src.index("function " + name + "(")
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class VoiceLabels(unittest.TestCase):
    def run_js(self, body):
        src = open(JS).read()
        prog = "\n".join([
            "const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;');",
            _fn(src, "voiceLabel"), _fn(src, "voiceOptions"), _fn(src, "stationVoiceLabel"), body])
        return subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True).stdout

    def test_labels_capitalised_values_lowercase(self):
        out = self.run_js("""
const add = voiceOptions(["aurora", "castor", "lux"], "", "avoice", stationVoiceLabel({_defaultVoice: "luna"}));
const cfg = voiceOptions(["aurora", "castor", "lux"], "castor", "bvoice");
console.log(JSON.stringify({add, cfg}));""")
        doc = json.loads(out)
        for html in (doc["add"], doc["cfg"]):
            self.assertIn('<option value="aurora">Aurora</option>', html)
            self.assertIn('<option value="lux">Lux</option>', html)
            self.assertNotIn('value="Aurora"', html)
        self.assertIn('<option value="castor" selected>Castor</option>', doc["cfg"])
        self.assertIn(">Station default (Luna)</option>", doc["add"])
        self.assertIn('<option value="">Station default</option>', doc["cfg"])

    def test_relay_ids_stay_lowercase(self):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        import voices
        self.assertTrue(all(v == v.lower() for v in voices.XAI_VOICES))
        self.assertEqual(voices.map_reported_voice("Aurora"), "aurora")


if __name__ == "__main__":
    unittest.main()
