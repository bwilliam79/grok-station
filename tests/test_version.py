#!/usr/bin/env python3
"""Semantic versions: comparison, the shell helper, and tools/check-version.py. No admin server."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_admin  # noqa: E402  (the one shared admin sandbox; see test_gs137_admin_import_guard)
sys.path.insert(0, os.path.join(ROOT, "admin"))
_spec = importlib.util.spec_from_file_location("check_version", os.path.join(ROOT, "tools", "check-version.py"))
check_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_version)
ga = test_admin.ga

SEMVER_SH = os.path.join(ROOT, "tools", "semver-order.sh")
CASES = (
    ("0.1.0", "0.1.1", "newer"),
    ("0.1.0", "0.1.0", "same"),
    ("0.1.2", "0.1.0", "older"),
    ("1.9.0", "1.10.0", "newer"),
    ("1.10.0", "1.9.0", "older"),
    ("0.2.0", "0.1.9", "older"),
    ("01.2.3", "1.2.3", "same"),
    ("4915427", "0.1.0", "newer"),
    ("dev", "0.1.1", "newer"),
    ("", "0.1.0", "newer"),
    ("0.1.0", "", "unknown"),
    ("0.1.0", "0.1", "unknown"),
    ("0.1.0", "v0.1.1", "unknown"),
    ("0.1.0", "0.1.0-rc1", "unknown"),
    ("0.1.2", None, "unknown"),
)


def sh_order(installed, available):
    proc = subprocess.run(
        ["sh", SEMVER_SH, "" if installed is None else str(installed), "" if available is None else str(available)],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc.stdout.strip()


def _block(path):
    text = open(os.path.join(ROOT, path), encoding="utf-8").read()
    start = text.index("# semver-order:start\n")
    end = text.index("# semver-order:end\n")
    return text[start:end]


class Semver(unittest.TestCase):
    def test_python_shell_and_admin_agree(self):
        for installed, available, expect in CASES:
            self.assertEqual(check_version.version_order(installed, available), expect, (installed, available))
            self.assertEqual(ga.version_order(installed, available), expect, (installed, available))
            self.assertEqual(sh_order(installed, available), expect, (installed, available))

    def test_shell_strips_whitespace(self):
        self.assertEqual(sh_order(" 0.1.0\n", "\t0.1.1 "), "newer")

    def test_copies_of_the_awk_match(self):
        block = _block("tools/semver-order.sh")
        self.assertEqual(block, _block("bootstrap.sh"))
        self.assertEqual(block, _block("tools/grok-station-update"))

    def test_updater_refuses_before_pull(self):
        boot = open(os.path.join(ROOT, "bootstrap.sh"), encoding="utf-8").read()
        upd = open(os.path.join(ROOT, "tools", "grok-station-update"), encoding="utf-8").read()
        self.assertLess(boot.index("semver_order"), boot.index("merge --quiet --ff-only"))
        self.assertIn("Refusing to install", boot)
        self.assertIn("Already up to date.", boot)
        self.assertIn("Refusing to install", upd)
        self.assertIn("Already up to date.", upd)
        self.assertIn('exec sh "$SRC/bootstrap.sh"', upd)
        self.assertNotRegex(boot, r"\bread\b")
        install = open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read()
        self.assertLess(install.index("[ -f VERSION ]"), install.index("rev-parse --short HEAD"))

    def test_admin_refuse_lower(self):
        self.assertIsNone(ga.update_block("0.1.0", "0.1.1", True))
        self.assertIsNone(ga.update_block("4915427", "0.1.0", True))
        self.assertEqual(ga.update_block("0.1.0", "0.2.0", False), "Check for updates first.")
        self.assertIn("Already up to date", ga.update_block("0.1.0", "0.1.0", True))
        msg = ga.update_block("0.1.2", "0.1.0", True)
        self.assertIn("Refusing to install 0.1.0 over 0.1.2", msg)
        self.assertIn("The main branch is older", msg)
        missing = ga.update_block("0.1.0", "", True)
        self.assertIn("Refusing to install nothing over 0.1.0", missing)
        self.assertIn("Refusing", ga.update_block("0.1.0", "nope", True))


def _git(repo, *args):
    subprocess.check_call(
        ["git", "-C", repo, "-c", "user.name=version-test", "-c", "user.email=version-test@localhost",
         "-c", "commit.gpgsign=false", *args],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-m", message)


class CheckVersion(unittest.TestCase):
    def test_first_version_passes_then_only_newer(self):
        with tempfile.TemporaryDirectory() as repo:
            _git(repo, "init", "-q")
            open(os.path.join(repo, "README"), "w").write("x\n")
            _commit(repo, "start")
            self.assertEqual(check_version.problem(repo), "HEAD has no VERSION file.")
            self.assertEqual(subprocess.run([sys.executable, os.path.join(ROOT, "tools", "check-version.py"), repo],
                                             capture_output=True).returncode, 1)
            open(os.path.join(repo, "VERSION"), "w").write("0.1.0\n")
            _commit(repo, "add version")
            self.assertIsNone(check_version.problem(repo))  # parent has no VERSION file
            open(os.path.join(repo, "VERSION"), "w").write("0.1.0\n")
            _commit(repo, "same")
            self.assertIn("not newer", check_version.problem(repo))
            open(os.path.join(repo, "VERSION"), "w").write("0.1.1\n")
            _commit(repo, "patch")
            self.assertIsNone(check_version.problem(repo))
            self.assertEqual(subprocess.run([sys.executable, os.path.join(ROOT, "tools", "check-version.py"), repo],
                                             capture_output=True).returncode, 0)
            open(os.path.join(repo, "VERSION"), "w").write("0.1.0\n")
            _commit(repo, "lower")
            self.assertIn("not newer", check_version.problem(repo))
            open(os.path.join(repo, "VERSION"), "w").write("dev\n")
            _commit(repo, "not semver")
            self.assertIn("MAJOR.MINOR.PATCH", check_version.problem(repo))

    def test_non_semver_parent_can_move_onto_semver(self):
        with tempfile.TemporaryDirectory() as repo:
            _git(repo, "init", "-q")
            open(os.path.join(repo, "VERSION"), "w").write("4915427\n")
            _commit(repo, "sha")
            open(os.path.join(repo, "VERSION"), "w").write("0.1.0\n")
            _commit(repo, "semver")
            self.assertIsNone(check_version.problem(repo))


class ReusedReleaseTest(unittest.TestCase):
    """Regression: 0.12.6 shipped at a54d0e6, then more code went out under the same 0.12.6 (re-released as 0.12.7)."""

    def test_released_version_not_reused(self):
        self.assertIsNone(check_version.reused_release(ROOT))


if __name__ == "__main__":
    unittest.main()

