"""Item 13: every copy button that hands out bot instructions has a View beside it (one shared helper)."""
import os, re, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()

class InstructionView(unittest.TestCase):
    def test_no_hand_rolled_copy_buttons(self):
        # A literal "Copy..." text button would skip the View. Only icon copies (data-copy) and the helper remain.
        raw = re.findall(r"<button[^>]*>\s*Copy[^<]*</button>", JS)
        self.assertEqual(raw, [], raw)
        self.assertNotIn("data-fincopy", JS)
        self.assertNotIn('id="aview"', JS)

    def test_every_helper_block_is_bound_with_the_same_text(self):
        made = set(re.findall(r'instructionsHtml\("([a-z0-9]+)"', JS))
        bound = set(re.findall(r'bindInstructions\([a-z]+, "([a-z0-9]+)"', JS))
        self.assertEqual(made, {"aroutine", "cpupd", "copyteamd", "chcopy"})
        self.assertEqual(made, bound)
        self.assertIn("instructionsHtml(finMsgId(b.slug)", JS)
        self.assertIn("bindInstructions(d, finMsgId(row.slug)", JS)

    def test_view_and_copy_share_one_source(self):
        body = re.search(r"function bindInstructions\(root, id, makeText, onCopy\) \{(.*?)\n\}", JS, re.S).group(1)
        self.assertIn("pre.textContent = makeText()", body)
        self.assertIn("const text = makeText()", body)
        self.assertIn('data-viewfor="${id}"', JS)
        self.assertIn('aria-controls="${id}"', JS)

    def test_copy_comes_before_view(self):
        body = re.search(r"function instructionsHtml\(id, copyLabel, opts = \{\}\) \{(.*?)\n\}", JS, re.S).group(1)
        c, st, v = body.index("data-copyfor="), body.index('class="copied"'), body.index("data-viewfor=")
        self.assertLess(c, st)
        self.assertLess(st, v)
