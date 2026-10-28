"""0.12.6: Finish update dialog cleanup and one Finish update button at a time."""
import json, os, shutil, subprocess, unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()


def _fn(name):
    i = JS.index("function " + name + "(")
    return JS[i:JS.index("\n}\n", i) + 3]


def _node(prog):
    if not shutil.which("node"):
        raise unittest.SkipTest("node not installed")
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


PRE = "const esc = s => String(s);\n" + "".join(_fn(n) for n in (
    "finishHelperChanges", "finishShared", "finishHelperLine", "finishLead", "finishRowHTML"))
PRE += "function finishPill(b){return '<span class=\"pill warn\">Needs update</span>';}\n"
PRE += "function finishRowButtons(){return '<button>Send</button>';}\nfunction finishCopyExtra(){return '';}\n"


def render(g):
    return _node(PRE + f"""
const g = {json.dumps(g)};
const rows = g.bots.map(b => finishRowHTML({{}}, g, b));
console.log(JSON.stringify({{line: finishHelperLine(g), lead: finishLead([g]), rows}}));""")


class FinishDialog(unittest.TestCase):
    def test_single_bot_same_helper(self):
        r = render({"home": True, "helper": "0.11.8", "want_helper": "0.11.8", "updater": "rhea", "bots": [{"slug": "rhea", "name": "Rhea"}]})
        self.assertEqual(r["line"], "")
        self.assertNotIn("updates the helper", r["rows"][0])
        self.assertEqual(r["lead"], "Send each bot its updated instructions.")
        self.assertEqual(r["rows"][0].count("pill"), 1)
        row = r["rows"][0]
        self.assertLess(row.index('class="finbtns"'), row.index("pill"))  # pill sits with the Send button

    def test_helper_change_with_two_bots(self):
        r = render({"home": True, "helper": "0.11.8", "want_helper": "0.12.0", "updater": "rhea",
                    "bots": [{"slug": "rhea", "name": "Rhea"}, {"slug": "kai", "name": "Kai"}]})
        self.assertEqual(r["line"], '<p class="finhelp">Includes a helper update</p>')
        self.assertIn("updates the helper", r["rows"][0])
        self.assertNotIn("updates the helper", r["rows"][1])
        self.assertIn("share one helper", r["lead"])

    def test_helper_change_single_bot(self):
        r = render({"home": True, "helper": "0.11.8", "want_helper": "0.12.0", "updater": "rhea", "bots": [{"slug": "rhea"}]})
        self.assertIn("Includes a helper update", r["line"])
        self.assertNotIn("0.11.8", r["line"])
        self.assertNotIn("updates the helper", r["rows"][0])
        self.assertEqual(r["lead"], "Send each bot its updated instructions.")

    def test_empty_want_helper_hides_line(self):
        self.assertEqual(render({"home": True, "helper": "0.11.8", "want_helper": "", "bots": []})["line"], "")

    def test_tag_is_quiet_gray(self):
        css = open(os.path.join(ROOT, "admin", "web", "admin.css")).read()
        rule = css[css.index(".fintag{"):].split("}", 1)[0]
        self.assertNotIn("amber", rule)


class FinishButtons(unittest.TestCase):
    """0.12.10: no Finish update button on Updates or in the header (see test_gs1210 for the Bots dot)."""

    def test_no_finish_button_anywhere(self):
        js = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        for gone in ("finhead", "finpage", "finishBtnHTML"):
            self.assertNotIn(gone, js)
        self.assertNotIn("Finish update", _fn("paintFinishButtons"))

    def test_behind_counts_only_listed_bots(self):
        """A stale Finish view must not count a bot that is no longer on the Bots list."""
        r = _node("""
const nodes = {};
const document = {getElementById: id => nodes[id] || null};
""" + _fn("finishComputers") + _fn("finishListedSlugs") + _fn("finishBehind") + """
const view = {behind: 2, computers: [{bots: [
  {slug: "rhea", state: "needs"},
  {slug: "ghost", state: "needs"}
]}]};
const off = finishBehind(view);
nodes.blist = {};
nodes.pg = {_bots: [{slug: "rhea"}, {slug: "kai", state: "done"}]};
view.computers[0].bots.push({slug: "kai", state: "done"});
const on = finishBehind(view);
nodes.pg._bots = [];
const empty = finishBehind(view);
console.log(JSON.stringify({off: off, on: on, empty: empty}));""")
        self.assertEqual(r, {"off": 2, "on": 1, "empty": 0})


if __name__ == "__main__":
    unittest.main()


class WaitingRow(unittest.TestCase):
    def test_no_resend_while_waiting_and_send_again_after_no_reply(self):
        pre = "const esc = s => String(s);\n" + _fn("finMsgId") + _fn("finishRowButtons") + _fn("finishSendable") + _fn("finishPill")
        r = _node(pre + """
const d = {};
console.log(JSON.stringify({
  wait: finishRowButtons(d, {slug: "rhea", state: "waiting"}),
  waitSend: finishSendable({slug: "rhea", instr: "a", state: "waiting"}),
  waitPill: finishPill({state: "waiting", wait_left: 290}),
  noreply: finishRowButtons(d, {slug: "rhea", state: "noreply"}),
  noreplySend: finishSendable({slug: "rhea", instr: "a", state: "noreply"}),
  noreplyPill: finishPill({state: "noreply"})}));""")
        self.assertEqual(r["wait"], "")
        self.assertFalse(r["waitSend"])
        self.assertIn("Waiting for reply, up to 5 min", r["waitPill"])
        self.assertIn("Send again", r["noreply"])
        self.assertTrue(r["noreplySend"])
        self.assertIn('pill warn', r["noreplyPill"]); self.assertIn("No reply", r["noreplyPill"])
