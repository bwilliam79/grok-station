#!/usr/bin/env python3
"""0.13.35: tools/release-check.sh is enforced on push, not only documented.

tools/install-release-hook writes the clone's local pre-push hook, which the global hooks dispatcher chains to
after pii-guard. tools/release-check-pre-push blocks the push unless every pushed commit is the checked-out commit
and tools/release-check.sh ends with "release-check: PASS". These run in throwaway repos with a stand-in
release-check.sh; the real gate run is in runs/ evidence.
"""
import os, shutil, subprocess, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZERO = "0" * 40


def _git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env, check=True).stdout.strip()


class _Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gs-hook-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.env = {"HOME": self.home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": self.tmp,
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "tools"))
        for f in ("release-check-pre-push", "install-release-hook"):
            shutil.copy2(os.path.join(ROOT, "tools", f), os.path.join(self.repo, "tools", f))
        self.verdict("PASS")
        _git(self.repo, "init", "-q", env=self.env)
        _git(self.repo, "add", "-A", env=self.env)
        _git(self.repo, "commit", "-qm", "one", env=self.env)
        self.head = _git(self.repo, "rev-parse", "HEAD", env=self.env)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def verdict(self, word, rc=None):
        rc = (0 if word == "PASS" else 1) if rc is None else rc
        with open(os.path.join(self.repo, "tools", "release-check.sh"), "w") as fh:
            fh.write("echo 'FAIL: stand-in' >/dev/null\necho 'release-check: %s'\nexit %d\n" % (word, rc))

    def hook(self, lines):
        p = subprocess.run(["sh", os.path.join(self.repo, "tools", "release-check-pre-push"), "origin", "x"],
                           cwd=self.repo, input="".join(l + "\n" for l in lines), capture_output=True, text=True,
                           env=self.env)
        return p.returncode, p.stderr


class PrePushBody(_Repo):
    def line(self, sha):
        return "refs/heads/main %s refs/heads/main %s" % (sha, "1" * 40)

    def test_pass_on_the_exact_tip_allows_the_push(self):
        self.assertEqual(self.hook([self.line(self.head)])[0], 0)

    def test_fail_blocks_the_push(self):
        self.verdict("FAIL")
        rc, err = self.hook([self.line(self.head)])
        self.assertEqual(rc, 1)
        self.assertIn("push blocked", err)

    def test_zero_exit_without_pass_line_blocks(self):
        self.verdict("maybe", rc=0)
        self.assertEqual(self.hook([self.line(self.head)])[0], 1)

    def test_pushing_another_commit_than_the_checkout_blocks(self):
        rc, err = self.hook([self.line("2" * 40)])
        self.assertEqual(rc, 1)
        self.assertIn("not the checked-out commit", err)

    def test_missing_release_check_blocks(self):
        os.unlink(os.path.join(self.repo, "tools", "release-check.sh"))
        self.assertEqual(self.hook([self.line(self.head)])[0], 1)

    def test_branch_delete_pushes_no_commit(self):
        self.verdict("FAIL")
        self.assertEqual(self.hook(["(delete) %s refs/heads/old %s" % (ZERO, "1" * 40)])[0], 0)


class Installer(_Repo):
    def setUp(self):
        super().setUp()
        self.global_hooks = os.path.join(self.home, "hooks")
        os.makedirs(self.global_hooks)
        with open(os.path.join(self.global_hooks, "pre-push"), "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(self.global_hooks, "pre-push"), 0o755)
        self.local = os.path.join(self.repo, ".git", "hooks", "pre-push")

    def install(self):
        return subprocess.run(["sh", "tools/install-release-hook"], cwd=self.repo, capture_output=True, text=True,
                              env=self.env)

    def test_needs_the_global_dispatcher(self):
        self.assertNotEqual(self.install().returncode, 0)
        self.assertFalse(os.path.exists(self.local))

    def test_installs_a_chained_hook_that_blocks_a_failing_check(self):
        _git(self.repo, "config", "--global", "core.hooksPath", self.global_hooks, env=self.env)
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.install().returncode, 0)  # rerun replaces its own hook
        self.assertTrue(os.access(self.local, os.X_OK))
        line = "refs/heads/main %s refs/heads/main %s\n" % (self.head, "1" * 40)
        run = lambda: subprocess.run([self.local, "origin", "x"], cwd=self.repo, input=line, capture_output=True,
                                     text=True, env=self.env).returncode
        self.assertEqual(run(), 0)
        self.verdict("FAIL")
        self.assertEqual(run(), 1)

    def test_refuses_to_replace_a_foreign_hook(self):
        _git(self.repo, "config", "--global", "core.hooksPath", self.global_hooks, env=self.env)
        os.makedirs(os.path.dirname(self.local), exist_ok=True)
        with open(self.local, "w") as fh:
            fh.write("#!/bin/sh\necho mine\n")
        self.assertNotEqual(self.install().returncode, 0)
        with open(self.local) as fh:
            self.assertEqual(fh.read(), "#!/bin/sh\necho mine\n")


if __name__ == "__main__":
    unittest.main()
