"""Item 12: every password failure says exactly "Password incorrect." with no prefix."""
import os, re, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def read(*p): return open(os.path.join(ROOT, *p), encoding="utf-8").read()

class PasswordMessage(unittest.TestCase):
    def test_server_uses_one_message(self):
        src = read("admin", "grok_admin.py")
        self.assertIn('PASSWORD_BAD = "Password incorrect."', src)
        for old in ("Wrong password", "Password is wrong"):
            self.assertNotIn(old, src)
        self.assertRegex(src, r'send\(429 if AUTH\.locked\(\) else 401, \{"error": PASSWORD_BAD')
        self.assertRegex(src, r'send\(403, \{"error": PASSWORD_BAD\}\)')

    def test_ui_has_no_prefix_for_password_errors(self):
        js = read("admin", "web", "app.js")
        self.assertIn('const PASSWORD_BAD = "Password incorrect.";', js)
        for old in ("Wrong password", "Password is wrong"):
            self.assertNotIn(old, js)
        # every error box goes through the password check before adding "Needs attention"
        for fn in ("errBox", "attn", "attnBox"):
            body = re.search(r"function %s\(el, msg\) \{(.*?)\n\}" % fn, js, re.S)
            self.assertIsNotNone(body, fn)
            self.assertTrue("withAttn(msg)" in body.group(1) or "PASSWORD_BAD" in body.group(1), fn)

    def test_docs_have_no_old_wording(self):
        for root, _, files in os.walk(ROOT):
            if ".git" in root or "tests" in root:
                continue
            for f in files:
                if f.endswith((".md", ".py", ".js", ".html", ".sh")):
                    t = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
                    self.assertNotIn("Wrong password.", t, f)
                    self.assertNotIn("Password is wrong.", t, f)
