#!/usr/bin/env python3
"""NI1: a train_flow.py swapped for a folder between stat and unlink does not abort install."""
import importlib.util
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tool():
    spec = importlib.util.spec_from_file_location("rm_stale_ni1", os.path.join(ROOT, "tools", "rm-stale-train-flow.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class UnlinkRace(unittest.TestCase):
    def test_swap_to_folder_is_logged_and_skipped(self):
        base = tempfile.mkdtemp()
        try:
            wake = os.path.join(base, "wake")
            os.makedirs(wake)
            open(os.path.join(wake, "train_flow.py"), "w").close()
            mod = _tool()
            real_unlink = os.unlink

            def racing_unlink(name, *a, **kw):
                if name == "train_flow.py":
                    real_unlink(os.path.join(wake, name))
                    os.mkdir(os.path.join(wake, name))
                return real_unlink(name, *a, **kw)

            mod.os.unlink = racing_unlink
            err = io.StringIO()
            try:
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    got = mod.remove(base)
            finally:
                mod.os.unlink = real_unlink
            self.assertEqual(got, [])
            self.assertIn("train_flow.py", err.getvalue())
            self.assertTrue(os.path.isdir(os.path.join(wake, "train_flow.py")))
        finally:
            shutil.rmtree(base)


if __name__ == "__main__":
    unittest.main()
