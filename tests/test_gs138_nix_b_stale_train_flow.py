#!/usr/bin/env python3
"""Nix (b): install removes the old grok-owned wake/train_flow.py and its .pyc, safely and idempotently."""
import importlib.util
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "rm-stale-train-flow.py")


def _tool():
    spec = importlib.util.spec_from_file_location("rm_stale_train_flow", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StaleTrainFlow(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.wake = os.path.join(self.base, "wake")
        os.makedirs(os.path.join(self.wake, "__pycache__"))
        self.outside = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.base)
        shutil.rmtree(self.outside)

    def _touch(self, *p):
        path = os.path.join(*p)
        open(path, "w").close()
        return path

    def test_removes_file_and_pyc_and_is_idempotent(self):
        self._touch(self.wake, "train_flow.py")
        self._touch(self.wake, "__pycache__", "train_flow.cpython-313.pyc")
        keep = [self._touch(self.wake, "grok_wake.py"), self._touch(self.wake, "__pycache__", "gain.cpython-313.pyc")]
        got = _tool().remove(self.base)
        self.assertEqual(sorted(got), ["wake/__pycache__/train_flow.cpython-313.pyc", "wake/train_flow.py"])
        self.assertFalse(os.path.lexists(os.path.join(self.wake, "train_flow.py")))
        for k in keep:
            self.assertTrue(os.path.exists(k))
        self.assertEqual(_tool().remove(self.base), [])

    def test_symlinks_are_never_followed_or_removed(self):
        target = self._touch(self.outside, "victim.py")
        os.symlink(target, os.path.join(self.wake, "train_flow.py"))
        self.assertEqual(_tool().remove(self.base), [])
        self.assertTrue(os.path.islink(os.path.join(self.wake, "train_flow.py")))
        self.assertTrue(os.path.exists(target))

    def test_symlinked_wake_dir_is_left_alone(self):
        shutil.rmtree(self.wake)
        self._touch(self.outside, "train_flow.py")
        os.symlink(self.outside, self.wake)
        self.assertEqual(_tool().remove(self.base), [])
        self.assertTrue(os.path.exists(os.path.join(self.outside, "train_flow.py")))

    def test_directory_named_train_flow_is_kept(self):
        os.makedirs(os.path.join(self.wake, "train_flow.py"))
        self.assertEqual(_tool().remove(self.base), [])

    def test_missing_base_is_fine(self):
        self.assertEqual(_tool().remove(os.path.join(self.base, "nope")), [])

    def test_both_installers_run_it(self):
        for rel in ("install.sh", "tools/install-files.sh"):
            src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
            self.assertIn("rm-stale-train-flow.py", src, rel)
            self.assertNotIn("rm -f /opt/grok-station/wake/train_flow", src, rel)


if __name__ == "__main__":
    unittest.main()
