"""A bot row shows at most one status message (0.13.1), now as the replies line (0.13.8 rev2).

'Helper out of date' and 'Update failed' must not stack. Failure wins. The message is one button
on the replies line, not a reserved empty row under it. A row with nothing to say has no button.
"""
import json, os, re, shutil, subprocess, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "admin", "web", "app.js")
CSS = os.path.join(ROOT, "admin", "web", "admin.css")

FAIL = "Update failed: The bot refused the update."
OLD = "Helper out of date: use Update"


def _fn(src, name):
    mark = "function " + name + "("
    i = src.index(mark)
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]
    raise AssertionError("unclosed " + name)


def _render(src, bot, view):
    """The one status line the Bots page paints, from the page's own functions."""
    names = ("helperBehind", "rowUpdating", "rowFailLine", "finishNotes", "botMessageLineHTML")
    parts = [_fn(src, name) for name in names]
    prog = "\n".join([
        "const esc = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;')"
        ".replace(/>/g,'&gt;').replace(/\"/g,'&quot;');",
        "const ICO_TRI = '';",
        "const FINISH = {view: %s};" % json.dumps(view),
        "const ROW_SENDING = {};",
        "function finishFind(v, slug) {",
        "  const computers = (v && v.computers) || [];",
        "  for (const g of computers) for (const b of (g.bots || [])) if (b && b.slug === slug) return b;",
        "  return null;",
        "}",
        *parts,
        "const bot = %s;" % json.dumps(bot),
        "const note = finishNotes(bot);",
        "process.stdout.write(JSON.stringify({note: note, html: botMessageLineHTML(note, true, bot.slug)}));",
    ])
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr or out.stdout or "node failed")
    return out.stdout


def _tags(html):
    """Start-tag names, in order, so two rows can be compared without their text."""
    return re.findall(r"<([a-z0-9]+)\b", html)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class BotRowStatus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = open(JS, encoding="utf-8").read()
        cls.css = open(CSS, encoding="utf-8").read()

    def _view(self, state, fail):
        return {"computers": [{"bots": [{"slug": "max", "state": state, "fail": fail, "instr": "a" * 16}]}]}

    def test_both_messages_are_one_line_with_the_failure(self):
        bot = {"slug": "max", "key_mode": "home", "paired": True,
               "helper": {"out_of_date": True, "state": "old"}}
        got = json.loads(_render(self.src, bot, self._view("needs", "The bot refused the update.")))
        html = got["html"]
        self.assertEqual(got["note"], FAIL)
        self.assertEqual(html.count("<button"), 1, html)
        self.assertNotIn(OLD, html)
        self.assertIn('aria-label="%s, show details"' % FAIL, html)
        self.assertIn('class="tx">%s</span>' % FAIL, html)
        self.assertIn('role="alert">%s</span>' % FAIL, html)
        self.assertNotIn("data-bst", html)

    def test_empty_row_has_no_message_button(self):
        bot = {"slug": "max", "key_mode": "home", "paired": True, "helper": {"out_of_date": False}}
        both = {"slug": "max", "key_mode": "home", "paired": True,
                "helper": {"out_of_date": True, "state": "old"}}
        empty = json.loads(_render(self.src, bot, self._view("done", "")))
        full = json.loads(_render(self.src, both, self._view("needs", "The bot refused the update.")))
        self.assertEqual(empty["note"], "")
        self.assertEqual(empty["html"], "")
        self.assertNotEqual(_tags(empty["html"]), _tags(full["html"]))
        self.assertNotIn(FAIL, empty["html"])
        self.assertNotIn(OLD, empty["html"])
        self.assertIn(FAIL, full["html"])

    def test_paint_uses_one_line_and_the_row_is_not_a_fixed_height(self):
        paint = _fn(self.src, "paintBots")
        self.assertIn("finishNotes(b)", paint)
        self.assertIn("botRowL2HTML(", paint)
        self.assertNotIn("botStatusSlot(", paint)
        css = self.css.replace(" ", "")
        self.assertIn(".bmsg.tx{min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}", css)
        self.assertNotIn(".botrow{height:104px", css)
        self.assertNotIn(".botrow{height:112px", css)


if __name__ == "__main__":
    unittest.main()
