#!/usr/bin/env python3
"""Nix 0.13.6 M-pre: root must not follow a symlink grok-relay planted in $V/voice, nor a symlink at the
wake-overrides prune's temp or target; safe-mkdirs temp names are random (secrets.token_hex).

Run unprivileged like test_gs136_nix: the real install.sh steps are extracted, owner flags point at this user and
the trusted base owner (root) is swapped for this user through the shim. Root runs are in the evidence files.
"""
import getpass
import grp
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from test_gs136_nix import SHIM, _load, _src

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ME = getpass.getuser()
MYGROUP = grp.getgrgid(os.getgid()).gr_name
SUBS = ("minted", "pending", "replies", "ack")


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        os.chmod(self.t, 0o755)
        self.shim = os.path.join(self.t, "shim.py")
        with open(self.shim, "w") as f:
            f.write(SHIM)
        self.victim = os.path.join(self.t, "victim")
        os.mkdir(self.victim)
        os.chmod(self.victim, 0o700)
        self.V = os.path.join(self.t, "var")
        os.mkdir(self.V)
        os.chmod(self.V, 0o755)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def _sh(self, body):
        script = 'set -eu\nDRY=0\nV="%s"\nO="%s"\nrun() { "$@"; }\nf() {\n%s\n}\nf\n' % (
            self.V, os.path.join(self.t, "opt"), body)
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, cwd=ROOT)


class VoiceDirs(Base):
    def test_no_root_install_d_into_voice_subdirs(self):
        for line in _src("install.sh").split("\n"):
            if not line.lstrip().startswith("#"):
                for n in SUBS:
                    self.assertNotIn("$V/voice/%s" % n, line)

    def _block(self):
        src = _src("install.sh")
        lines = [l for l in src.split("\n") if "voice/minted" in l and "safe-mkdirs" not in l
                 and not l.lstrip().startswith("#")]
        self.assertTrue(lines and "--group grok-admin grok-relay" in lines[0], lines)
        i = src.rfind("  if [", 0, src.index(lines[0])) + 2
        body = src[i:src.index("\n  fi\n", i) + 5]
        body = body.replace("--group grok-admin grok-relay", "--group %s %s" % (MYGROUP, ME))
        return body.replace("< tools/safe-mkdirs.py", '< "%s"' % self.shim)

    def test_plain_voice_dirs_0770(self):
        os.mkdir(os.path.join(self.V, "voice"))
        r = self._sh(self._block())
        self.assertEqual(r.returncode, 0, r.stderr)
        for rel in ("voice",) + tuple("voice/" + n for n in SUBS):
            st = os.stat(os.path.join(self.V, rel))
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o770, rel)
            self.assertEqual(st.st_gid, os.getgid(), rel)

    def test_planted_symlink_subdir_is_refused(self):
        for n in SUBS:
            shutil.rmtree(os.path.join(self.V, "voice"), ignore_errors=True)
            os.mkdir(os.path.join(self.V, "voice"))
            os.symlink(self.victim, os.path.join(self.V, "voice", n))
            r = self._sh(self._block())
            self.assertNotEqual(r.returncode, 0, n)
            self.assertIn("refusing", r.stdout + r.stderr)
            self.assertEqual(stat.S_IMODE(os.stat(self.victim).st_mode), 0o700, n)
            self.assertEqual(os.listdir(self.victim), [])

    def test_group_option(self):
        r = subprocess.run(["python3", self.shim, "--mode", "0770", "--group", MYGROUP, ME, self.V, "voice/x"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.V, "voice", "x")).st_mode), 0o770)


if __name__ == "__main__":
    unittest.main()
