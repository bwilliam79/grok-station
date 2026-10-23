"""tools/leak-scan.py: hashed private terms, personal email domains, real LAN IPs.

Uses made-up terms with a test salt, so no private term appears here.
"""
import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "leak-scan.py")
spec = importlib.util.spec_from_file_location("leak_scan", TOOL)
ls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ls)


def h(t):
    return hashlib.sha256(("testsalt" + t).encode()).hexdigest()


PARTS = {h("zorbhost"), h("hyph-box"), h("fake.lan"), h("old town")}
WORDS = {h("quux")}


def hit(line):
    return ls.term_hit(line, salt_hash=h, parts_set=PARTS, words_set=WORDS)


def run(text, *args):
    return subprocess.run([sys.executable, TOOL, "t", *args], input=text,
                          capture_output=True, text=True)


class TermTests(unittest.TestCase):
    def test_hashes_only(self):
        src = open(TOOL).read()
        self.assertIn("PART_HASHES", src)
        for v in ls.PART_HASHES | ls.WORD_HASHES:
            self.assertRegex(v, r"^[0-9a-f]{64}$")

    def test_part_terms(self):
        for line in ("ssh zorbhost", "ZorbHost.local", "user@zorbhost.fake.lan",
                     "a-zorbhost-b", "on hyph-box.", "x.fake.lan", "in Old Town today"):
            self.assertTrue(hit(line), line)

    def test_word_terms_skip_package_names(self):
        self.assertTrue(hit("host quux"))
        self.assertTrue(hit("quux.local"))
        self.assertFalse(hit("libgl1-quux-dri"))
        self.assertFalse(hit("quuxly zorbhosts"))

    def test_real_terms_clean_text(self):
        self.assertFalse(ls.term_hit("the home machine relays replies to the station"))


class RuleTests(unittest.TestCase):
    def test_email(self):
        self.assertEqual(run("mail a.b@Gmail.com\n").returncode, 1)
        self.assertEqual(run("x@aol.com\n").returncode, 1)
        self.assertEqual(run("owner@example.invalid\n").returncode, 0)

    def test_ips(self):
        for ip in ("192.168.200.20", "10.1.2.3", "172.20.0.1", "172.31.255.1"):
            self.assertEqual(run("ip %s\n" % ip).returncode, 1, ip)
        for ip in ("10.42.0.1", "192.168.4.20", "192.168.77.20", "172.17.0.2",
                   "192.0.2.5", "203.0.113.9", "8.8.8.8", "172.32.0.1", "1.2.3.4.5"):
            self.assertEqual(run("ip %s\n" % ip).returncode, 0, ip)

    def test_output_never_echoes_match(self):
        r = run("a@gmail.com\n")
        self.assertNotIn("gmail", r.stderr)
        self.assertIn("line 1", r.stderr)

    def test_allow_lines(self):
        self.assertEqual(run("leak-guard: allow a@gmail.com\n").returncode, 1)

    def test_files_and_allowlist(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("ok\nip 192.168.200.9\n")
        try:
            r = run("", f.name)
            self.assertEqual(r.returncode, 1)
            self.assertIn("line 2", r.stderr)
        finally:
            os.unlink(f.name)


class GuardTests(unittest.TestCase):
    def test_whole_tree_clean(self):
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                               text=True).stdout.split()
        r = subprocess.run([sys.executable, TOOL, "tree", *files], cwd=ROOT,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_commit_msg_mode(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("fix: route via 10.9.9.9\n# 10.8.8.8 comment ignored\n")
        try:
            env = dict(os.environ, CI="", PII_GUARD_PATTERNS="/nonexistent")
            r = subprocess.run(["bash", os.path.join(ROOT, "tools", "pii-guard"), "msg", f.name],
                               cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("line 1", r.stderr)
            self.assertNotIn("line 2", r.stderr)
        finally:
            os.unlink(f.name)

    def test_hooks_installed(self):
        src = open(os.path.join(ROOT, "tools", "install-pii-hooks")).read()
        for hook in ("pre-commit", "commit-msg", "pre-push"):
            self.assertIn(hook, src)


if __name__ == "__main__":
    unittest.main()
