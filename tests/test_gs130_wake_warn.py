"""0.13.0: a wake word that sounds like another bot's is a warning with "Use it anyway" and suggestions that keep
the bot's name; an exact duplicate is still a hard block."""
import json, os, shutil, subprocess, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_admin  # noqa: E402  (sets the admin's test environment before importing it)
ga = test_admin.ga

KNOWN = ["hey_grok", "hey_miles"]


class ServerRules(unittest.TestCase):
    def test_close_is_a_warning_not_a_block(self):
        self.assertEqual(ga.phrase_problem("Hey Jules", KNOWN), ga.too_close_msg("Hey Miles"))  # old strict rule
        self.assertIsNone(ga.phrase_problem("Hey Jules", KNOWN, allow_close=True))
        self.assertEqual(ga.phrase_close("Hey Jules", KNOWN), "Hey Miles")
        self.assertEqual(ga.close_warning("Hey Miles"), "Sounds a lot like Hey Miles and might wake the wrong bot.")

    def test_exact_duplicate_stays_blocked(self):
        self.assertEqual(ga.phrase_problem("Hey Miles", KNOWN, allow_close=True), ga.duplicate_msg("Hey Miles"))

    def test_suggestions_keep_the_name_and_pass(self):
        sug = ga.wake_suggestions("Jules", KNOWN)
        self.assertTrue(2 <= len(sug) <= 3, sug)
        for p in sug:
            self.assertIn("Jules", p)
            self.assertIsNone(ga.phrase_problem(p, KNOWN), p)
        self.assertNotIn("Hey Jules", sug)
        self.assertEqual(ga.wake_suggestions("", KNOWN), [])


def _fn(src, name):
    i = src.index("function " + name + "(")
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class DialogHTML(unittest.TestCase):
    def test_warning_markup(self):
        src = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        prog = "\n".join(["const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;');",
                          _fn(src, "wakeWarnHTML"),
                          'console.log(wakeWarnHTML({warning: "Sounds a lot like Hey Miles and might wake the wrong bot.", suggestions: ["Okay Jules", "Hi Jules", "Hello Jules", "Yo Jules"]}));'])
        html = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True).stdout
        self.assertIn("Sounds a lot like Hey Miles and might wake the wrong bot.", html)
        self.assertIn('id="wwok"', html)
        self.assertIn("Use it anyway", html)
        self.assertEqual(html.count('class="btn sm wwsug"'), 3)
        self.assertIn('data-phrase="Okay Jules"', html)
        self.assertNotIn("Try another", html)

    def test_dialog_reserves_height_and_keeps_start_enabled(self):
        src = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        css = open(os.path.join(ROOT, "admin", "web", "admin.css")).read()
        self.assertIn('<div id="wwe" class="wwmsg"', src)
        self.assertIn(".wwmsg{min-height:", css)
        self.assertIn("payload.close_ok = true", src)


if __name__ == "__main__":
    unittest.main()
