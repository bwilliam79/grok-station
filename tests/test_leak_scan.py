"""tools/leak-scan.py: hashed private terms, personal email domains, real LAN IPs.

Uses a temp terms file with made-up terms, so no private term appears here.
"""
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


TERMS = {"zorbhost", "hyph-box", "fake.lan", "old town"}
_tmp = tempfile.NamedTemporaryFile("w", suffix=".terms", delete=False)
_tmp.close()


def setUpModule():
    # Written again whenever this module's tests start, so a shuffled run that leaves and re-enters works.
    with open(_tmp.name, "w") as f:
        f.write("# comment\nZorbHost\nhyph-box\nfake.lan\nold town\n")


def tearDownModule():
    try:
        os.unlink(_tmp.name)
    except FileNotFoundError:
        pass


def env(**kw):
    e = {k: v for k, v in os.environ.items() if k not in ("LEAK_TERMS", "CI")}
    e["LEAK_TERMS_FILE"] = _tmp.name
    e.update(kw)
    return e


def hit(line):
    return ls.term_hit(line, TERMS, words={"quux"})


def run(text, *args, **kw):
    return subprocess.run([sys.executable, TOOL, "t", *args], input=text,
                          capture_output=True, text=True, env=env(**kw))


class TermTests(unittest.TestCase):
    def test_no_term_list_in_repo(self):
        src = open(TOOL).read()
        self.assertNotIn("HASHES", src)
        self.assertIn("LEAK_TERMS", src)

    def test_loads_terms_file_and_env(self):
        self.assertEqual(run("ssh zorbhost\n").returncode, 1)
        self.assertEqual(run("ssh zorbhost\n", LEAK_TERMS="other\n").returncode, 0)
        self.assertEqual(run("ssh other\n", LEAK_TERMS="other\n").returncode, 1)

    def test_missing_list_warns_locally_fails_in_ci(self):
        r = run("clean\n", LEAK_TERMS_FILE="/nonexistent")
        self.assertEqual(r.returncode, 0)
        self.assertIn("warning", r.stderr)
        r = run("clean\n", LEAK_TERMS_FILE="/nonexistent", CI="1")
        self.assertEqual(r.returncode, 1)

    def test_terms_never_printed(self):
        r = run("x zorbhost y\n")
        self.assertNotIn("zorbhost", r.stderr.lower())
        self.assertIn("line 1", r.stderr)

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
        self.assertFalse(hit("the home machine relays replies to the station"))
        self.assertTrue(ls.term_hit("ssh mesa.local", set()))
        self.assertFalse(ls.term_hit("libgl1-mesa-dri", set()))


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
    def test_workflow(self):
        wf = open(next(p for p in (os.path.join(ROOT, ".github", "workflows", "pii-guard.yml"), os.path.join(ROOT, "tools", "ci", "pii-guard.yml")) if os.path.exists(p))).read()
        self.assertIn("contents: read", wf)
        self.assertIn("head.repo.full_name == github.repository", wf)
        self.assertRegex(wf, r"actions/checkout@[0-9a-f]{40}")
        self.assertIn("LEAK_TERMS", wf)

    def test_whole_tree_clean(self):
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                               text=True).stdout.split()
        r = subprocess.run([sys.executable, TOOL, "tree", *files], cwd=ROOT,
                           capture_output=True, text=True, env=env())
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_commit_msg_mode(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("fix: route via 10.9.9.9\n# 10.8.8.8 comment ignored\n")
        try:
            r = subprocess.run(["bash", os.path.join(ROOT, "tools", "pii-guard"), "msg", f.name],
                               cwd=ROOT, env=env(PII_GUARD_PATTERNS="/nonexistent"), capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("line 1", r.stderr)
            self.assertNotIn("line 2", r.stderr)
        finally:
            os.unlink(f.name)

    def test_push_with_unknown_remote_sha_scans_all_new_commits(self):
        # After a history rewrite the remote's old sha isn't in this clone. That must not skip the scan.
        with tempfile.TemporaryDirectory() as d:
            git = lambda *a: subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.hooksPath=/dev/null", *a],
                                            check=True, capture_output=True, text=True).stdout.strip()
            git("init", "-q")
            open(os.path.join(d, "a.txt"), "w").write("hello\n")
            git("add", "a.txt")
            git("commit", "-q", "-m", "route via 10.9.9.9")
            head = git("rev-parse", "HEAD")
            pat = os.path.join(d, ".git", "pat")
            with open(pat, "w") as f:
                f.write("zzqqxx-never-matches\n")
            r = subprocess.run(["bash", os.path.join(ROOT, "tools", "pii-guard"), "push"], cwd=d,
                               input=f"refs/heads/main {head} refs/heads/main {'1' * 40}\n",
                               env=env(PII_GUARD_PATTERNS=pat), capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_hooks_installed(self):
        src = open(os.path.join(ROOT, "tools", "install-pii-hooks")).read()
        for hook in ("pre-commit", "commit-msg", "pre-push"):
            self.assertIn(hook, src)


if __name__ == "__main__":
    unittest.main()
