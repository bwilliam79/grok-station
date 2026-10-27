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
        self.assertEqual(r["line"], '<p class="finhelp">Helper 0.11.8 → 0.12.0</p>')
        self.assertIn("updates the helper", r["rows"][0])
        self.assertNotIn("updates the helper", r["rows"][1])
        self.assertIn("share one helper", r["lead"])

    def test_helper_change_single_bot(self):
        r = render({"home": True, "helper": "0.11.8", "want_helper": "0.12.0", "updater": "rhea", "bots": [{"slug": "rhea"}]})
        self.assertIn("0.11.8 → 0.12.0", r["line"])
        self.assertNotIn("updates the helper", r["rows"][0])
        self.assertEqual(r["lead"], "Send each bot its updated instructions.")

    def test_empty_want_helper_hides_line(self):
        self.assertEqual(render({"home": True, "helper": "0.11.8", "want_helper": "", "bots": []})["line"], "")

    def test_tag_is_quiet_gray(self):
        css = open(os.path.join(ROOT, "admin", "web", "admin.css")).read()
        rule = css[css.index(".fintag{"):].split("}", 1)[0]
        self.assertNotIn("amber", rule)


class FinishButtons(unittest.TestCase):
    def _paint(self, on_updates):
        return _node(f"""
function mk(){{return {{innerHTML:"", hidden:false, querySelector(){{return null;}}}};}}
const nodes = {{finhead: mk()}};
if ({json.dumps(on_updates)}) nodes.finpage = mk();
const document = {{getElementById: id => nodes[id] || null}};
const FINISH = {{view: {{behind: 1}}}};
function finishBehind(v){{return v.behind;}}
{_fn("finishBtnHTML")}
{_fn("paintFinishButtons")}
paintFinishButtons();
console.log(JSON.stringify({{head: nodes.finhead.innerHTML, headHidden: nodes.finhead.hidden,
  page: nodes.finpage ? nodes.finpage.innerHTML : null}}));""")

    def test_updates_page_shows_only_the_in_page_button(self):
        r = self._paint(True)
        self.assertEqual(r["head"], "")
        self.assertTrue(r["headHidden"])
        self.assertIn("Finish update", r["page"])

    def test_other_pages_keep_the_header_button(self):
        r = self._paint(False)
        self.assertIn("Finish update", r["head"])
        self.assertFalse(r["headHidden"])


if __name__ == "__main__":
    unittest.main()
