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


class Overrides(Base):
    def _block(self):
        src = _src("install.sh")
        i = src.index('echo "+ prune retired starter wake words')
        i = src.rfind("\n", 0, i) + 1
        body = src[i:src.index("\n  fi\n", src.index("\nPY\n", i)) + 5]
        return body.replace("python3 -I tools/safe-mkdirs.py", 'python3 "%s"' % self.shim)

    def _setup(self):
        adm = os.path.join(self.V, "admin")
        os.mkdir(adm)
        os.makedirs(os.path.join(self.t, "opt", "wake", "models", "enrolled"))
        p = os.path.join(adm, "wake-overrides.json")
        with open(p, "w") as f:
            json.dump({"hey_ash": {"enabled": False}, "hey_finn": {"enabled": True}}, f)
        os.chmod(p, 0o640)
        return adm, p

    def test_prune_still_works(self):
        adm, p = self._setup()
        r = self._sh(self._block())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.load(open(p)), {"hey_finn": {"enabled": True}})
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o640)
        self.assertEqual(sorted(os.listdir(adm)), ["wake-overrides.json"])

    def test_planted_tmp_symlink_not_followed(self):
        adm, p = self._setup()
        target = os.path.join(self.victim, "secret")
        with open(target, "w") as f:
            f.write("keep")
        os.symlink(target, p + ".tmp")
        r = self._sh(self._block())
        self.assertEqual(open(target).read(), "keep")
        self.assertEqual(json.load(open(p)), {"hey_finn": {"enabled": True}}, r.stdout + r.stderr)

    def test_planted_target_symlink_not_followed(self):
        adm, p = self._setup()
        real = os.path.join(self.victim, "ov.json")
        os.rename(p, real)
        os.chmod(real, 0o600)
        os.symlink(real, p)
        self._sh(self._block())
        self.assertEqual(json.load(open(real)), {"hey_ash": {"enabled": False}, "hey_finn": {"enabled": True}})
        self.assertEqual(stat.S_IMODE(os.stat(real).st_mode), 0o600)

    def test_replace_refuses_symlink(self):
        m = _load()
        self.assertTrue(hasattr(m, "replace"))
        adm, p = self._setup()
        target = os.path.join(self.victim, "secret")
        with open(target, "w") as f:
            f.write("keep")
        os.unlink(p)
        os.symlink(target, p)
        with self.assertRaises(m.Refused):
            m.replace(self.V, "admin", "wake-overrides.json", b"{}", root_uid=os.getuid())
        self.assertEqual(open(target).read(), "keep")


class RandomTemp(unittest.TestCase):
    def test_put_temp_name_is_random(self):
        m = _load()
        t = tempfile.mkdtemp()
        try:
            os.chmod(t, 0o755)
            os.mkdir(os.path.join(t, "d"))
            src = os.path.join(t, "f.txt")
            with open(src, "w") as f:
                f.write("x")
            seen = []
            real = os.rename

            def rec(a, b, **kw):
                seen.append(a)
                return real(a, b, **kw)
            with mock.patch.object(m.os, "rename", rec):
                for _ in range(2):
                    m.put(t, "d", [src], os.getuid(), os.getgid(), root_uid=os.getuid())
            self.assertEqual(len(seen), 2)
            for n in seen:
                self.assertRegex(n, r"^\.f\.txt\.new\.[0-9a-f]{16}$")
            self.assertNotEqual(seen[0], seen[1])
        finally:
            shutil.rmtree(t, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
