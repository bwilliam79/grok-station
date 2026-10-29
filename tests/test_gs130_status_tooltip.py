"""0.13.0: a bot's status ("Offline: No answer") is the avatar tooltip, replacing the bare "Offline", and is no
longer a line under the bot name. Runs the page's own functions in node."""
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
class StatusTooltip(unittest.TestCase):
    def run_js(self, body):
        src = open(JS).read()
        prog = "\n".join([
            "const BOT_CHECKS = {};",
            "const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;');",
            "const defaultAvatarSrc = s => '/d/' + s; const avatarSrc = s => '/a/' + s;",
            _fn(src, "serverCheck"), _fn(src, "botRing"), _fn(src, "botCheckText"), _fn(src, "avatar"), body])
        return subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True).stdout

    def test_offline_tooltip_carries_the_failure_mode(self):
        out = self.run_js("""
const b = {slug: "max", name: "Max", status: {last_error_at: 9, last_error: "No answer"}};
const ring = botRing(b), text = botCheckText(b);
console.log(JSON.stringify({text, html: avatar(b.slug, b.name, false, {cls: ring.cls, word: text || ring.word})}));""")
        doc = json.loads(out)
        self.assertEqual(doc["text"], "Offline: No answer")
        html = doc["html"]
        self.assertIn('title="Offline: No answer"', html)
        self.assertIn('aria-label="Max: Offline: No answer"', html)
        self.assertIn('aria-describedby="avtip-max"', html)
        self.assertIn('id="avtip-max">Offline: No answer</span>', html)
        self.assertIn(" st offl", html)  # the colored ring stays
        self.assertNotIn('title="Offline"', html)
        self.assertNotIn(">Offline</span>", html)  # one tooltip, the full text

    def test_rows_have_no_status_line_and_tap_opens_popover(self):
        src = open(JS).read()
        paint = _fn(src, "paintBots")
        self.assertNotIn("${check", paint)
        self.assertNotIn("Offline: ", paint)
        self.assertIn('closest(".tipwrap.avtip")', src)
        css = open(os.path.join(ROOT, "admin", "web", "admin.css")).read()
        self.assertIn(".tipwrap.open>.tip{display:block}", css)


if __name__ == "__main__":
    unittest.main()
