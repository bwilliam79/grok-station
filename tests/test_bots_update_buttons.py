"""0.12.8: Bots-page Update (per row) and Update all, shown only when a listed bot needs Finish update."""
import json, os, re, shutil, subprocess, unittest

JS = open(os.path.join(os.path.dirname(__file__), "..", "admin", "web", "app.js"), encoding="utf-8").read()


def _fn(name):
    m = re.search(r"\nfunction %s\(.*?\n}\n" % re.escape(name), JS, re.S)
    if not m:
        raise AssertionError("missing " + name)
    return m.group(0)


PRELUDE = """
const esc = s => String(s);
let LISTED = null;
function finishListedSlugs() { return LISTED; }
const FINISH = {view: null};
"""


class UpdateButtons(unittest.TestCase):
    def run_js(self, body):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        prog = PRELUDE + "".join(_fn(n) for n in ("finishComputers", "finishFind", "finishBehind", "finishNeedsUpdate",
                                                   "updateRowBtnHTML", "updateAllBtnHTML")) \
            + "const ROW_SENDING = {};" + _fn("rowUpdating") + body
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_buttons_only_when_needed(self):
        got = self.run_js("""
const view = s => ({behind: 9, computers: [{bots: s}]});
const res = {};
LISTED = {fresh: true, old: true};
FINISH.view = view([{slug: "fresh", state: "done"}]);
res.none = [updateRowBtnHTML({slug: "fresh"}), updateAllBtnHTML()];
FINISH.view = view([{slug: "fresh", state: "done"}, {slug: "old", state: "needs"}, {slug: "gone", state: "needs"}]);
res.some = [updateRowBtnHTML({slug: "fresh"}), updateRowBtnHTML({slug: "old"}), updateAllBtnHTML(), finishBehind(FINISH.view)];
FINISH.view = view([{slug: "fresh", state: "done"}, {slug: "gone", state: "needs"}]);
res.removed = [updateAllBtnHTML(), finishBehind(FINISH.view)];
console.log(JSON.stringify(res));""")
        self.assertEqual(got["none"], ["", ""])
        self.assertEqual(got["some"][0], "")
        self.assertIn('data-upd="old"', got["some"][1])
        self.assertIn("Update all", got["some"][2])
        self.assertEqual(got["some"][3], 1)  # the removed bot is not counted
        self.assertEqual(got["removed"], ["", 0])

    def test_reuses_finish_update_flow(self):
        self.assertIn('openFinishDialog({slug: btn.dataset.upd})', JS)
        self.assertIn('openFinishDialog({all: true})', JS)
        body = _fn("openFinishDialog").replace("async ", "") if False else JS.split("async function openFinishDialog", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("finishSendRow(d, finishFind(d._view, opts.slug))", body)
        self.assertIn("finishSendAll(d)", body)
        self.assertIn('id="updallbtn"', JS)
        self.assertIn("Update all (", _fn("updateAllBtnHTML"))
        self.assertIn("Update all (", _fn("botsCalloutHTML"))


if __name__ == "__main__":
    unittest.main()
