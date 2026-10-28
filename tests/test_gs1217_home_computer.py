"""0.12.17: the 'Which home computer' paragraph reaches home bots on setup AND on update, from one source.

Real flow: gs-update.sh downloads admin/routine-home.txt and renders it with its embedded gs_routine copy; the
Bots page pairing text is routineForBot() in app.js. Both must carry the paragraph and the same body.
"""
import json, os, re, shutil, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARA = ("Which home computer: if you can run commands on more than one, pick the one that stays on all the time. "
        "Replies stop when that computer sleeps or is off. Use that same computer for the setup below and for every reply.")
SLUG, COMMIT, SHA = "ash", "a" * 40, "b" * 64
ST, FP, PUB = "https://station.example:8443", ":".join(["AB"] * 32), "A" * 43 + "="


def read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


def update_output(mode):
    """Render the routine exactly as gs-update.sh does, with the gs_routine copy embedded in gs-update.sh."""
    sh = read("scripts", "gs-update.sh")
    mod = sh.split("<<'GS_ROUTINE_PY'\n", 1)[1].split("\nGS_ROUTINE_PY\n", 1)[0]
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "gs_routine.py"), "w") as fh:
            fh.write(mod)
        args = [sys.executable, "-I", os.path.join(d, "gs_routine.py"), "render",
                os.path.join(ROOT, "admin", "routine-%s.txt" % mode), mode, SLUG, COMMIT, SHA]
        if mode == "cloud":
            args += [ST, FP, PUB]
        r = subprocess.run(args, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def setup_output(mode):
    """Run routineForBot from app.js in node, as the Bots page does when pairing."""
    js = read("admin", "web", "app.js")
    const = "BOT_ROUTINE_HOME" if mode == "home" else "BOT_ROUTINE"
    tmpl = js.split("const %s = `" % const, 1)[1].split("`;", 1)[0]
    fn = js[js.index("function routineForBot("):]
    fn = fn[:fn.index("\nfunction ", 10)]
    prog = ("const BOT_ROUTINE = `x`, BOT_ROUTINE_HOME = `x`;\n"
            "function deployScript(){return 'SCRIPT';}\nfunction stationAddress(){return %s;}\n"
            "function fillUpdateCommand(b){return b;}\n" % json.dumps(ST))
    prog = prog.replace(const + " = `x`", const + " = " + json.dumps(tmpl)) + fn
    prog += "\nprocess.stdout.write(routineForBot(%s,%s,%s,%s));" % tuple(json.dumps(v) for v in (SLUG, PUB, FP, mode))
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout, tmpl


def setup_body(text):
    return text.split("use the text below as that routine.\n\n", 1)[1].split("\n\nDeploy bot key setup", 1)[0]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class HomeComputerParagraph(unittest.TestCase):
    def test_update_output_home_has_paragraph(self):
        self.assertIn(PARA, update_output("home"))

    def test_setup_output_home_has_paragraph_once(self):
        out, _ = setup_output("home")
        self.assertEqual(out.count(PARA), 1)

    def test_setup_and_update_carry_same_body_home(self):
        out, _ = setup_output("home")
        upd = update_output("home")
        cmd = re.search(r"^d=\$\(mktemp -d\).*$", upd, re.M).group(0)
        self.assertEqual(setup_body(out), upd.replace(cmd, "<update command>"))

    def test_cloud_keeps_its_wording(self):
        out, _ = setup_output("cloud")
        upd = update_output("cloud")
        cmd = re.search(r"^d=\$\(mktemp -d\).*$", upd, re.M).group(0)
        body = setup_body(out)
        self.assertEqual(body, upd.replace(cmd, "<update command>"))
        self.assertIn("On the home machine", body)
        self.assertNotIn("Run exactly this command, unchanged", body)
        self.assertEqual(out.count(PARA), 1)
        self.assertIn(PARA, upd)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(shutil.which("node"), "node not installed")
class SetupReplyFieldLinks(unittest.TestCase):
    """0.12.17: the setup reply links the routine's own Webhook URL and key fields, never the Routines panel."""

    def test_setup_text_spells_out_field_links(self):
        for mode in ("home", "cloud"):
            out, _ = setup_output(mode)
            tail = out.split("Reply in this order:", 1)[1]
            self.assertIn("grokbot://app/v1/sidebar?target=webhook-url&automation=<routine folder id>", tail)
            self.assertIn("grokbot://app/v1/sidebar?target=webhook-key&automation=<routine folder id>", tail)
            self.assertIn("'Webhook URL'", tail)
            self.assertIn("'Webhook key'", tail)
            self.assertNotIn("tab=routines", out)
            self.assertNotRegex(tail, r"(?i)(link|open|go) to (the )?Routines")
            self.assertNotIn("a general link to Routines", tail)
