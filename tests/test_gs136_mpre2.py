#!/usr/bin/env python3
"""Nix 0.13.6 M-pre2: `grok-station reset-admin` rewrites $V/admin/auth.json (a grok-admin-owned folder) without
following a planted auth.json.tmp or auth.json symlink: random temp through the folder's descriptor
(O_CREAT|O_EXCL|O_NOFOLLOW), fchown/fchmod to the folder's owner and 0600, rename in that folder."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _reset_py():
    src = open(os.path.join(ROOT, "tools", "grok-station")).read()
    i = src.index("reset() {")
    a = src.index("<<'PY'\n", i) + len("<<'PY'\n")
    return src[a:src.index("\nPY\n", a) + 1]


class ResetAdmin(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.adm = os.path.join(self.t, "admin")
        os.mkdir(self.adm)
        self.auth = os.path.join(self.adm, "auth.json")
        with open(self.auth, "w") as f:
            json.dump({"password": "x", "keep": 1}, f)
        self.victim = os.path.join(self.t, "victim.json")
        with open(self.victim, "w") as f:
            json.dump({"password": "victim"}, f)
        os.chmod(self.victim, 0o644)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def _run(self):
        return subprocess.run(["python3", "-", self.auth], input=_reset_py(), capture_output=True, text=True)

    def _victim_untouched(self):
        self.assertEqual(json.load(open(self.victim)), {"password": "victim"})
        self.assertEqual(stat.S_IMODE(os.stat(self.victim).st_mode), 0o644)

    def test_plain_reset(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.load(open(self.auth))
        self.assertNotIn("password", d)
        self.assertEqual(d["keep"], 1)
        st = os.stat(self.auth)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual((st.st_uid, st.st_gid), (os.stat(self.adm).st_uid, os.stat(self.adm).st_gid))
        self.assertEqual(os.listdir(self.adm), ["auth.json"])

    def test_planted_tmp_symlink_not_followed(self):
        os.symlink(self.victim, self.auth + ".tmp")
        self._run()
        self._victim_untouched()

    def test_planted_auth_symlink_not_followed(self):
        os.unlink(self.auth)
        os.symlink(self.victim, self.auth)
        r = self._run()
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(os.path.islink(self.auth))
        self._victim_untouched()

    def test_temp_name_is_random_not_fixed(self):
        src = _reset_py()
        self.assertNotIn('f + ".tmp"', src)
        self.assertIn("token_hex", src)
        self.assertIn("O_EXCL", src)


if __name__ == "__main__":
    unittest.main()
