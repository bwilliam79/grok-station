"""Item 16: one admin type and spacing scale. No raw font sizes outside the :root token block."""
import os, re, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "admin", "web")
CSS = open(os.path.join(WEB, "admin.css"), encoding="utf-8").read()
JS = open(os.path.join(WEB, "app.js"), encoding="utf-8").read()
SIZE = r"\d+(?:\.\d+)?(?:px|rem|em|pt|%)"

def without_root(css):
    return re.sub(r":root\{[^}]*\}", "", css, count=1)

class TypeScale(unittest.TestCase):
    def test_tokens_live_in_one_root_block(self):
        root = re.search(r":root\{([^}]*)\}", CSS).group(1)
        for tok in ("--fs-help", "--fs-body", "--fs-label", "--fs-h3", "--fs-h2", "--fs-h1", "--fs-mono",
                    "--sp-1", "--sp-2", "--sp-3", "--sp-4", "--sp-5", "--sp-6", "--panel-pad"):
            self.assertIn(tok + ":", root, tok)
        self.assertEqual(len(re.findall(r":root\{", CSS)), 1)

    def test_no_raw_font_size_in_css(self):
        body = without_root(CSS)
        bad = re.findall(r"font-size:\s*" + SIZE, body)
        self.assertEqual(bad, [])
        # font shorthand: weight? size/line family. A literal size is not allowed.
        bad = [m for m in re.findall(r"font:([^;}]*)", body) if re.search(r"(?<![\w-])" + SIZE, m)]
        self.assertEqual(bad, [])

    def test_no_inline_font_styles_in_js_or_html(self):
        self.assertEqual(re.findall(r'style="[^"]*font[^"]*"', JS), [])
        self.assertNotRegex(JS, r"\.style\.font")
        for f in os.listdir(WEB):
            if f.endswith(".html"):
                self.assertNotRegex(open(os.path.join(WEB, f), encoding="utf-8").read(), r'style="[^"]*font')

    def test_every_tab_panel_shares_one_class(self):
        panels = re.findall(r'role="tabpanel"[^>]*', JS)
        self.assertTrue(panels)
        for p in panels:
            self.assertIn('class="cpanel"', p)
        self.assertRegex(CSS, r"\.cpanel\{padding:var\(--panel-pad\)")

    def test_design_doc_documents_the_scale(self):
        doc = open(os.path.join(ROOT, "docs", "design.md"), encoding="utf-8").read()
        self.assertIn("Admin type and spacing scale", doc)
        for tok in ("--fs-help", "--fs-h1", "--sp-4", "--panel-pad"):
            self.assertIn(tok, doc)
