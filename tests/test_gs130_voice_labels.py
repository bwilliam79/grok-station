"""Voice names on the admin page are display labels. The stored id stays lowercase."""
import os, shutil, subprocess, unittest

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
    def test_labels_capitalised_values_lowercase(self):
        with open(JS, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("function voiceOptions", src)
        self.assertNotIn("function stationVoiceLabel", src)
        prog = _fn(src, "voiceLabel") + "\nconsole.log(voiceLabel('aurora'));\nconsole.log(voiceLabel(''));"
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.splitlines(), ["Aurora", ""])

    def test_relay_ids_stay_lowercase(self):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        import voices
        self.assertTrue(all(v == v.lower() for v in voices.XAI_VOICES))
        self.assertEqual(voices.map_reported_voice("Aurora"), "aurora")


if __name__ == "__main__":
    unittest.main()
