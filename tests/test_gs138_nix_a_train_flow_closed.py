#!/usr/bin/env python3
"""Nix (a): the wake listener fails closed when relay/train_flow.py is missing or untrusted.

It logs one line, keeps running with wake-word training disabled, and never crashes at import.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tree(with_flow):
    d = tempfile.mkdtemp()
    shutil.copytree(os.path.join(ROOT, "wake"), os.path.join(d, "wake"))
    os.makedirs(os.path.join(d, "relay"))
    # 0.13.13: safe_io.py is always installed in relay/ (install.sh, tools/install-files.sh); only train_flow varies.
    shutil.copy(os.path.join(ROOT, "relay", "safe_io.py"), os.path.join(d, "relay", "safe_io.py"))
    if with_flow == "symlink":
        os.symlink(os.path.join(ROOT, "relay", "train_flow.py"), os.path.join(d, "relay", "train_flow.py"))
    return d


def _import(d):
    code = ("import sys; sys.path.insert(0, %r); import grok_wake as g; "
            "print('LOADED', g.train_flow is None, g.TRAINING_OFF['error'])") % os.path.join(d, "wake")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)


class TrainFlowFailsClosed(unittest.TestCase):
    def test_missing_file_disables_training_without_crashing(self):
        d = _tree(False)
        try:
            out = _import(d)
            self.assertEqual(out.returncode, 0, out.stderr[-1500:])
            self.assertIn("LOADED True", out.stdout)
            self.assertEqual(out.stdout.count("wake-word training disabled"), 1)
        finally:
            shutil.rmtree(d)

    def test_symlink_is_refused(self):
        d = _tree("symlink")
        try:
            out = _import(d)
            self.assertEqual(out.returncode, 0, out.stderr[-1500:])
            self.assertIn("LOADED True", out.stdout)
        finally:
            shutil.rmtree(d)

    def test_untrusted_owner_fails_the_file_check(self):
        sys.path.insert(0, os.path.join(ROOT, "wake"))
        import grok_wake as gw
        with tempfile.NamedTemporaryFile(suffix=".py") as f:
            os.chmod(f.name, 0o644)
            # The test runner is not root, so the file is not root-owned.
            if os.getuid() != 0:
                self.assertFalse(gw._train_flow_file_ok(f.name))

    def test_enroll_endpoints_refuse_when_disabled(self):
        src = open(os.path.join(ROOT, "wake", "grok_wake.py"), encoding="utf-8").read()
        i = src.index('if self.path != "/enroll":')
        self.assertIn("if train_flow is None:\n                return self._r(503, TRAINING_OFF)", src[i:i + 300])
        j = src.index('if self.path == "/enroll/open-tile":')
        self.assertIn("return self._r(503, TRAINING_OFF)", src[j:j + 200])
        k = src.index("def enroll(req, read16, play):")
        self.assertIn("if train_flow is None:\n        return", src[k:k + 300])


if __name__ == "__main__":
    unittest.main()
