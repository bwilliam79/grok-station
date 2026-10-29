"""0.13.1: a bot row shows at most one status line, and every row is the same height.

'Helper out of date: use Update' and 'Update failed: ...' used to stack as two extra lines, so that
row grew taller than the others. Failure wins over out of date. The line is one nowrap ellipsis;
the full text is in title and in a visually hidden element named by aria-describedby. A row with
no message still has the empty status element, so the structure (and the height) does not change.
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
    """The row status the Bots page paints, from the page's own functions."""
    names = ("helperBehind", "rowUpdating", "rowFailLine", "finishNotes", "botStatusSlot")
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
        "process.stdout.write(botStatusSlot(%s));" % json.dumps(bot),
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
        html = _render(self.src, bot, self._view("needs", "The bot refused the update."))
        self.assertEqual(html.count("data-bst"), 1, html)
        self.assertEqual(html.count('class="need"') + html.count("class='need'"), 0, html)
        self.assertNotIn(OLD, html.split('class="sr"', 1)[0].split("title=", 1)[0])
        self.assertIn('title="%s"' % FAIL, html)
        self.assertIn('aria-describedby="bst-max"', html)
        self.assertIn('id="bst-max">%s</span>' % FAIL, html)
        self.assertIn('class="sr"', html)
        self.assertEqual(html.count(FAIL), 3)  # title, the visible line, and the hidden copy
        self.assertNotIn(OLD, html)

    def test_empty_row_has_the_same_status_structure(self):
        bot = {"slug": "max", "key_mode": "home", "paired": True, "helper": {"out_of_date": False}}
        both = {"slug": "max", "key_mode": "home", "paired": True,
                "helper": {"out_of_date": True, "state": "old"}}
        empty = _render(self.src, bot, self._view("done", ""))
        full = _render(self.src, both, self._view("needs", "The bot refused the update."))
        self.assertEqual(_tags(empty), _tags(full), (empty, full))
        self.assertEqual(empty.count("data-bst"), 1, empty)
        self.assertIn('aria-describedby="bst-max"', empty)
        self.assertIn('id="bst-max">', empty)
        self.assertNotIn(FAIL, empty)
        self.assertNotIn(OLD, empty)

    def test_paint_reserves_a_slot_on_every_row_and_css_fixes_the_height(self):
        paint = _fn(self.src, "paintBots")
        self.assertIn("botStatusSlot(", paint)
        self.assertGreaterEqual(paint.count("botStatusSlot("), 3)  # Grok, each bot, each pending
        css = self.css
        self.assertIn(".bst{height:18px;line-height:18px;", css.replace(" ", ""))
        self.assertIn("white-space:nowrap", css)
        self.assertIn("text-overflow:ellipsis", css)
        # Desktop: a top-level rule (column 0). Phone: indented inside the max-width:767px block.
        self.assertRegex(css, r"(?m)^\.botrow\{height:104px[;}]")
        self.assertRegex(css, r"(?m)^  \.botrow\{height:112px;")


if __name__ == "__main__":
    unittest.main()
