"""0.13.1: 'Hey Grok, ...' is one inline text run, and Integrations back is a button.

The wake word and the rest of the example were separate flex items, so a gap
appeared before the comma. The comma sits in the source immediately after the
wake span's closing tag. The span is inline text: no flex, no gap, no padding,
no margin-right.

The add-flow back control is the same secondary button other admin sub-views
use (class "btn g back"), not an underlined accent link. It is at least 44px
tall and has no underline.
"""
import json, os, re, shutil, subprocess, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_PATH = os.path.join(ROOT, "admin", "web", "app.js")
HTML_PATH = os.path.join(ROOT, "admin", "web", "index.html")
CSS_PATH = os.path.join(ROOT, "admin", "web", "admin.css")

WAKE = re.compile(r'<span\b([^>]*)>Hey Grok</span>')


def _src(path):
    return open(path, encoding="utf-8").read()


def _fn(src, name):
    mark = "function " + name + "("
    i = src.index(mark)
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]
    raise AssertionError("unclosed " + name)


class VoiceExamples(unittest.TestCase):
    def test_comma_follows_the_wake_span_in_templates(self):
        js = _src(JS_PATH)
        html = _src(HTML_PATH)
        found = 0
        for path, text in ((JS_PATH, js), (HTML_PATH, html)):
            for m in WAKE.finditer(text):
                found += 1
                self.assertIn('class="wake"', m.group(1), "%s: %s" % (path, m.group(0)))
                nxt = text[m.end():m.end() + 1]
                self.assertEqual(nxt, ",", "%s: %r then %r" % (path, m.group(0), text[m.end():m.end() + 12]))
        self.assertGreaterEqual(found, 3, "integrations and the bots page each render a wake span")
        self.assertIn('<span class="wake">Hey Grok</span>, turn on ', js)
        self.assertNotIn('Hey Grok</span><span>', js)
        self.assertNotIn('class="saychip">“Hey Grok,', js)
        self.assertNotIn('class="w">Hey Grok</span>', js)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_bots_page_examples_are_one_text_run(self):
        js = _src(JS_PATH)
        phrase = re.search(r'const TEAM_PHRASE = "([^"]+)";', js).group(1)
        update = re.search(r'const TEAM_UPDATE_PHRASE = "([^"]+)";', js).group(1)
        prog = (
            "const TEAM_PHRASE = %s; const TEAM_UPDATE_PHRASE = %s;\n"
            "const esc = s => String(s);\n%s\n%s\n"
            "const before = teamPhraseHtml(teamState({_bots: [{main: true, slug: 'rhea'}], _imported: {rows: []}}));\n"
            "const after = teamPhraseHtml(teamState({_bots: [{main: true, slug: 'rhea'}, {slug: 'kai'}], _imported: {rows: []}}));\n"
            "process.stdout.write(JSON.stringify({before: before, after: after}));\n"
        ) % (json.dumps(phrase), json.dumps(update), _fn(js, "teamState"), _fn(js, "teamPhraseHtml"))
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(
            got["before"],
            'Say “<span class="wake">Hey Grok</span>, import the rest of my team.” to add your other bots.')
        self.assertEqual(
            got["after"],
            'Added a bot? Say “<span class="wake">Hey Grok</span>, update my team list.”')
        for html in got.values():
            close = html.index("</span>") + len("</span>")
            self.assertEqual(html[close:close + 1], ",")

    def test_wake_css_is_inline_text(self):
        css = re.sub(r"\s+", "", _src(CSS_PATH))
        m = re.search(r"\.wake\{([^}]*)\}", css)
        self.assertIsNotNone(m, "missing .wake rule")
        body = m.group(1)
        for bad in ("display:inline-block", "display:flex", "display:inline-flex", "gap:", "padding", "margin-right"):
            self.assertNotIn(bad, body, bad)


class IntegrationsBack(unittest.TestCase):
    def test_back_control_is_the_secondary_button(self):
        js = _src(JS_PATH)
        page = js[js.index("async function pageIntegrations"):]
        self.assertNotRegex(page, r"<a\b[^>]*\bid=\"iback\"")
        buttons = re.findall(
            r'<button type="button" class="btn g back" id="iback">([^<]*)</button>', page)
        self.assertGreaterEqual(len(buttons), 4, buttons)  # WLED add + manage, Homebridge add + manage
        for label in buttons:
            self.assertIn("‹", label)
            # Insecure-mode stop goes back to Homebridge. Every other control says Integrations.
            self.assertTrue("Integrations" in label or label == "‹ Homebridge", label)
        # Both flows use that control, not a private underlined link.
        self.assertIn("WLED", page)
        self.assertIn("Homebridge", page)
        self.assertGreaterEqual(page.count('id="iback"'), len(buttons))
        css = re.sub(r"\s+", "", _src(CSS_PATH))
        rule = re.search(r"button#iback\{([^}]*)\}", css)
        self.assertIsNotNone(rule, "missing button#iback rule")
        body = rule.group(1)
        self.assertIn("min-height:44px", body)
        self.assertIn("text-decoration:none", body)
        self.assertNotIn("underline", body.replace("text-decoration:none", ""))


if __name__ == "__main__":
    unittest.main()


class HomebridgeStopScreen(unittest.TestCase):
    """hbHead escapes its title, so callers pass plain text (no double escape: "can't" stays readable)."""

    def test_stop_title_is_escaped_once_and_always_given(self):
        js = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
        self.assertIn("const hbHead = (n, title) => ", js)
        self.assertIn("<h2>${esc(title)}</h2>", js.split("const hbHead = (n, title) => ", 1)[1].split("\n", 1)[0])
        self.assertNotIn("hbHead(4, esc(", js)
        for call in re.findall(r"hbStopped\(([^\n]*)\);", js):
            self.assertRegex(call, r'"We (can\'t use this login|couldn\'t finish the check)"\)?$', call)


class HomebridgeHttpWarning(unittest.TestCase):
    """Nix I1: an http:// Homebridge address is allowed but warned about in the add flow and on the card."""

    def test_http_warning_in_flow_and_card(self):
        js = open(JS_PATH, encoding="utf-8").read()
        self.assertIn("function hbHttpWarn(base)", js)
        fn = js.split("function hbHttpWarn(base)", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn('/^http:\\/\\//i.test', fn)
        # 0.13.4, Noor SPEC rev3 09b: informational box, not the amber warning.
        self.assertIn('<div class="hbinfo" role="note"><span class="ii" aria-hidden="true">i</span>', fn)
        self.assertIn("<b>Not encrypted.</b> This address uses http, so the connection isn't encrypted. That's fine on your home network. To encrypt it, turn on https in Homebridge's settings and use the https address.", fn)
        self.assertNotIn("hbwarn", fn)
        css = open(os.path.join(os.path.dirname(JS_PATH), "admin.css"), encoding="utf-8").read()
        rules = "".join(line for line in css.splitlines() if line.startswith(".hbinfo"))
        self.assertIn("background:var(--gs-surface-2)", rules)
        self.assertNotIn("amber", rules)
        self.assertNotIn(".hbwarn{", css)
        for where in ("function hbAddress(", "function hbEnter(", "async function list("):
            body = js.split(where, 1)[1].split("\n  }\n", 1)[0]
            with self.subTest(where):
                self.assertIn("hbHttpWarn(", body)
        readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
        self.assertIn("unencrypted", readme.split("**0.13.1**", 1)[1].split("**0.13.0**", 1)[0])

    def test_pick_step_notes_plugins_as_switches(self):
        # gs135: the warning is the Switches category count, not a page-level sentence.
        js = open(JS_PATH, encoding="utf-8").read()
        fn = _fn(js, "hbCountText")
        self.assertIn('categoryKey === "switch"', fn)
        self.assertIn("\\u26a0 check for locks or garage doors", fn)
        self.assertNotIn("Some plugins show locks or garage doors as plain switches", js)
