#!/usr/bin/env python3
"""0.13.35: tools/release-check.sh is enforced on push, not only documented.

tools/install-release-hook writes the clone's local pre-push hook, which the global hooks dispatcher chains to
after pii-guard. tools/release-check-pre-push gates only pushes to refs/heads/main: the pushed commit must be the
checked-out commit and tools/release-check.sh must end with "release-check: PASS"; deleting main is refused.
Other refs pass. These run in throwaway repos with a stand-in
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

    def hook(self, lines, raw=None):
        """Run the body. raw is the exact stdin (no newline added), for malformed input."""
        data = raw if raw is not None else "".join(l + "\n" for l in lines)
        p = subprocess.run(["sh", os.path.join(self.repo, "tools", "release-check-pre-push"), "origin", "x"],
                           cwd=self.repo, input=data, capture_output=True, text=True, env=self.env)
        return p.returncode, p.stderr


class PrePushBody(_Repo):
    """Only a push to refs/heads/main is gated; there the pushed sha must be HEAD and release-check must PASS."""

    OLD = "1" * 40

    def main(self, sha, remote=None):
        return "refs/heads/main %s refs/heads/main %s" % (sha, remote or self.OLD)

    def branch(self, sha, name="pr-42", remote=None):
        return "refs/heads/%s %s refs/heads/%s %s" % (name, sha, name, remote or self.OLD)

    def test_pr_branch_push_with_failing_check_passes(self):
        self.verdict("FAIL")
        self.assertEqual(self.hook([self.branch(self.head)])[0], 0)
        self.assertEqual(self.hook([self.branch("2" * 40)])[0], 0)  # not HEAD either: not gated

    def test_new_branch_passes(self):
        self.verdict("FAIL")
        self.assertEqual(self.hook([self.branch(self.head, "feature", remote=ZERO)])[0], 0)

    def test_new_main_is_gated(self):
        self.verdict("FAIL")
        self.assertEqual(self.hook([self.main(self.head, remote=ZERO)])[0], 1)

    def test_tag_push_passes(self):
        self.verdict("FAIL")
        self.assertEqual(self.hook(["refs/tags/v1 %s refs/tags/v1 %s" % (self.head, ZERO)])[0], 0)

    def test_non_main_delete_passes(self):
        self.verdict("FAIL")
        self.assertEqual(self.hook(["(delete) %s refs/heads/old %s" % (ZERO, self.OLD)])[0], 0)

    def test_main_push_of_a_non_head_sha_is_refused(self):
        rc, err = self.hook([self.main("2" * 40)])
        self.assertEqual(rc, 1)
        self.assertIn("not the checked-out commit", err)

    def test_main_with_failing_check_is_refused(self):
        self.verdict("FAIL")
        rc, err = self.hook([self.main(self.head)])
        self.assertEqual(rc, 1)
        self.assertIn("push blocked", err)

    def test_main_zero_exit_without_pass_line_is_refused(self):
        self.verdict("maybe", rc=0)
        self.assertEqual(self.hook([self.main(self.head)])[0], 1)

    def test_main_with_missing_release_check_is_refused(self):
        os.unlink(os.path.join(self.repo, "tools", "release-check.sh"))
        self.assertEqual(self.hook([self.main(self.head)])[0], 1)

    def test_main_with_exact_tip_and_pass_passes(self):
        self.assertEqual(self.hook([self.main(self.head)])[0], 0)

    def test_mixed_push_is_gated_by_the_main_line(self):
        lines = [self.branch("2" * 40), self.main(self.head)]
        self.assertEqual(self.hook(lines)[0], 0)  # main is HEAD and PASS
        self.assertEqual(self.hook(list(reversed(lines)))[0], 0)
        self.assertEqual(self.hook([self.branch(self.head), self.main("2" * 40)])[0], 1)  # main not HEAD
        self.verdict("FAIL")
        self.assertEqual(self.hook(lines)[0], 1)  # main check fails

    def test_deleting_main_is_refused(self):
        rc, err = self.hook(["(delete) %s refs/heads/main %s" % (ZERO, self.OLD)])
        self.assertEqual(rc, 1)
        self.assertIn("deleting refs/heads/main is refused", err)
        self.assertEqual(self.hook([self.branch(self.head), "(delete) %s refs/heads/main %s" % (ZERO, self.OLD)])[0], 1)


class PrePushBodyInput(_Repo):
    """A last line with no newline and a CRLF line are read like any other; malformed is refused."""

    main = PrePushBody.main
    branch = PrePushBody.branch
    OLD = PrePushBody.OLD

    def test_main_line_without_trailing_newline_is_gated(self):
        self.assertEqual(self.hook(None, raw=self.main("2" * 40))[0], 1)  # non-HEAD
        self.verdict("FAIL")
        self.assertEqual(self.hook(None, raw=self.main(self.head))[0], 1)  # HEAD, failing check
        self.verdict("PASS")
        self.assertEqual(self.hook(None, raw=self.branch(self.head) + "\n" + self.main(self.head))[0], 0)

    def test_crlf_main_line_is_gated(self):
        self.assertEqual(self.hook(None, raw=self.main("2" * 40) + "\r\n")[0], 1)
        self.verdict("FAIL")
        self.assertEqual(self.hook(None, raw=self.main(self.head) + "\r\n")[0], 1)
        self.verdict("PASS")
        self.assertEqual(self.hook(None, raw=self.main(self.head) + "\r\n")[0], 0)
        self.assertEqual(self.hook(None, raw=self.branch("2" * 40) + "\r\n")[0], 0)

    def test_malformed_line_is_refused(self):
        for bad in ("refs/heads/main %s refs/heads/main\n" % self.head, "garbage\n",
                    self.main(self.head) + " extra\n"):
            rc, err = self.hook(None, raw=bad)
            self.assertEqual(rc, 1, bad)
            self.assertIn("malformed", err)

    def test_blank_lines_pass(self):
        self.verdict("FAIL")
        self.assertEqual(self.hook(None, raw="\n\n")[0], 0)
        self.assertEqual(self.hook(None, raw="")[0], 0)

    def test_sha256_delete_of_main_is_refused(self):
        rc, err = self.hook(["(delete) %s refs/heads/main %s" % ("0" * 64, "1" * 64)])
        self.assertEqual(rc, 1)
        self.assertIn("deleting refs/heads/main is refused", err)


class _Hooked(_Repo):
    """A throwaway HOME whose global hooks dir has an executable pre-push (the dispatcher's place)."""

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


class Installer(_Hooked):
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


class InstalledStub(_Hooked):
    """The installed stub decides scope before it looks for this checkout's body."""

    def setUp(self):
        super().setUp()
        _git(self.repo, "config", "--global", "core.hooksPath", self.global_hooks, env=self.env)
        self.assertEqual(self.install().returncode, 0)
        self.body = os.path.join(self.repo, "tools", "release-check-pre-push")

    def stub(self, data):
        p = subprocess.run([self.local, "origin", "x"], cwd=self.repo, input=data, capture_output=True, text=True,
                           env=self.env)
        return p.returncode, p.stderr

    def no_body(self):
        os.unlink(self.body)  # a checkout from before 0.13.35: no body in its tree

    def main(self, sha):
        return "refs/heads/main %s refs/heads/main %s" % (sha, "1" * 40)

    def branch(self, sha, name="pr-42", remote="1" * 40):
        return "refs/heads/%s %s refs/heads/%s %s" % (name, sha, name, remote)

    def test_checkout_without_body_pushes_a_pr_branch(self):
        self.no_body()
        self.verdict("FAIL")
        self.assertEqual(self.stub(self.branch(self.head) + "\n")[0], 0)
        self.assertEqual(self.stub(self.branch(self.head, "feature", ZERO) + "\n")[0], 0)  # new branch
        self.assertEqual(self.stub("(delete) %s refs/heads/old %s\n" % (ZERO, "1" * 40))[0], 0)
        self.assertEqual(self.stub("refs/tags/v1 %s refs/tags/v1 %s\n" % (self.head, ZERO))[0], 0)
        self.assertEqual(self.stub("")[0], 0)

    def test_checkout_without_body_cannot_push_main(self):
        self.no_body()
        rc, err = self.stub(self.main(self.head) + "\n")
        self.assertEqual(rc, 1)
        self.assertIn("lacks", err)
        self.assertEqual(self.stub(self.branch(self.head) + "\n" + self.main(self.head) + "\n")[0], 1)  # mixed
        self.assertEqual(self.stub("(delete) %s refs/heads/main %s\n" % (ZERO, "1" * 40))[0], 1)

    def test_checkout_without_body_main_line_without_newline_or_crlf_is_blocked(self):
        self.no_body()
        self.assertEqual(self.stub(self.main(self.head))[0], 1)
        self.assertEqual(self.stub(self.main(self.head) + "\r\n")[0], 1)
        self.assertEqual(self.stub("garbage\n")[0], 1)  # malformed: not decided without the body

    def test_with_body_main_is_gated_and_stdin_reaches_the_body(self):
        self.assertEqual(self.stub(self.main(self.head) + "\n")[0], 0)
        self.assertEqual(self.stub(self.branch("2" * 40) + "\n" + self.main(self.head))[0], 0)  # no final newline
        rc, err = self.stub(self.branch(self.head) + "\n" + self.main("2" * 40) + "\r\n")
        self.assertEqual(rc, 1)
        self.assertIn("not the checked-out commit", err)  # the body saw the main line
        self.verdict("FAIL")
        self.assertEqual(self.stub(self.main(self.head) + "\n")[0], 1)
        self.assertEqual(self.stub(self.branch(self.head) + "\n")[0], 0)  # scope: the body is not even run
        rc, err = self.stub("garbage\n")
        self.assertEqual(rc, 1)
        self.assertIn("malformed", err)

    def test_replaces_the_0_13_35_stub(self):
        with open(self.local, "w") as fh:
            fh.write("#!/bin/sh\n# grok-station release-check pre-push (tools/install-release-hook)\nexit 1\n")
        self.assertEqual(self.install().returncode, 0)
        with open(self.local) as fh:
            text = fh.read()
        self.assertIn("# grok-station release-check pre-push v2 (tools/install-release-hook)", text)
        self.no_body()
        self.assertEqual(self.stub(self.branch(self.head) + "\n")[0], 0)


if __name__ == "__main__":
    unittest.main()
