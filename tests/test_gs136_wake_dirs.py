#!/usr/bin/env python3
"""0.13.6: st_wake must not follow a symlink grok planted at $O/wake or below and hand grok its target.

Checked as root with coreutils 9.7: `install -d -o grok -g grok $O/wake $O/wake/models` follows a symlink at
either path and chowns (and chmods 0755) the target. These tests run the real st_wake directory step from
install.sh, with the owner swapped for the current user so they run unprivileged; the chmod of the target
(0700 -> 0755) shows the follow without root.
"""
import getpass
import importlib.util
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("safe_mkdirs", os.path.join(ROOT, "tools", "safe-mkdirs.py"))


SHIM = """import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("safe_mkdirs", %r)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.ROOT_UID = os.getuid()  # unprivileged test: trust a base this user owns instead of root
sys.exit(m.main(sys.argv[1:]))
""" % os.path.join(ROOT, "tools", "safe-mkdirs.py")


def _dir_step(shim):
    """st_wake's lines before the venv step, with the owner set to whoever runs the tests."""
    src = open(os.path.join(ROOT, "install.sh")).read()
    body = src[src.index("st_wake() {\n") + len("st_wake() {\n"):]
    body = body[:body.index('  sh_ "[ -x $O/wake/venv/bin/python ]')]
    me = getpass.getuser()
    return (body.replace("-o grok -g grok", "").replace(" - grok ", " - %s " % me)
            .replace("tools/safe-mkdirs.py", shim))


class WakeDirs(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.shim = os.path.join(self.t, "shim.py")
        open(self.shim, "w").write(SHIM)
        self.O = os.path.join(self.t, "opt")
        os.mkdir(self.O)
        self.victim = os.path.join(self.t, "victim")
        os.mkdir(self.victim, 0o700)
        os.chmod(self.victim, 0o700)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def _run(self):
        script = 'set -e\nDRY=0\nO="$1"\nrun() { "$@"; }\nf() {\n%s}\nf\n' % _dir_step(self.shim)
        return subprocess.run(["sh", "-c", script, "sh", self.O], capture_output=True, text=True, cwd=ROOT)

    def _victim_untouched(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.victim).st_mode), 0o700)
        self.assertEqual(os.listdir(self.victim), [])

    def test_plain_dirs_are_made(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        for rel in ("wake", "wake/models", "wake/models/enrolled", "portraits"):
            p = os.path.join(self.O, rel)
            self.assertTrue(os.path.isdir(p) and not os.path.islink(p), rel)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o755, rel)
        self.assertEqual(self._run().returncode, 0)  # re-run is fine

    def test_symlinked_wake_is_refused(self):
        os.symlink(self.victim, os.path.join(self.O, "wake"))
        r = self._run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("symlink", r.stdout + r.stderr)
        self._victim_untouched()

    def test_symlinked_models_is_refused(self):
        os.mkdir(os.path.join(self.O, "wake"))
        os.symlink(self.victim, os.path.join(self.O, "wake", "models"))
        r = self._run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("symlink", r.stdout + r.stderr)
        self._victim_untouched()

    def test_symlinked_portraits_is_refused(self):
        os.symlink(self.victim, os.path.join(self.O, "portraits"))
        self.assertNotEqual(self._run().returncode, 0)
        self._victim_untouched()

    def test_install_d_with_owner_is_gone_from_st_wake(self):
        src = open(os.path.join(ROOT, "install.sh")).read()
        self.assertNotIn("install -d -o grok -g grok $O/wake", src)


if __name__ == "__main__":
    unittest.main()
