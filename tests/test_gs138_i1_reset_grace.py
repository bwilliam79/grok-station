#!/usr/bin/env python3
"""gs138 I1: reset-admin restores password_grace_min "session" to the factory 10.

The settings file sits beside auth.json. A planted symlink must not be followed,
and the password reset must not run while that symlink is in place.
"""
import json
import os
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _reset_py():
    src = open(os.path.join(ROOT, "tools", "grok-station"), encoding="utf-8").read()
    i = src.index("reset() {")
    a = src.index("<<'PY'\n", i) + len("<<'PY'\n")
    return src[a:src.index("\nPY\n", a) + 1]


class ResetGrace(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.adm = os.path.join(self.t, "admin")
        os.mkdir(self.adm)
        self.auth = os.path.join(self.adm, "auth.json")
        with open(self.auth, "w", encoding="utf-8") as f:
            json.dump({"password": "x", "keep": 1}, f)
        self.settings = os.path.join(self.adm, "update-settings.json")

    def tearDown(self):
        for root, dirs, files in os.walk(self.t, topdown=False):
            for name in files:
                os.unlink(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(self.t)

    def _run(self):
        return subprocess.run(
            ["python3", "-", self.auth], input=_reset_py(), capture_output=True, text=True)

    def test_session_grace_becomes_factory_10(self):
        with open(self.settings, "w", encoding="utf-8") as f:
            json.dump({"password_grace_min": "session", "autocheck": True}, f)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        auth = json.load(open(self.auth, encoding="utf-8"))
        self.assertNotIn("password", auth)
        self.assertEqual(auth["keep"], 1)
        doc = json.load(open(self.settings, encoding="utf-8"))
        self.assertEqual(doc.get("password_grace_min", 10), 10)
        self.assertNotEqual(doc.get("password_grace_min"), "session")
        self.assertIs(doc["autocheck"], True)
        st = os.stat(self.settings)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertFalse(os.path.islink(self.settings))

    def test_settings_symlink_is_not_followed(self):
        victim = os.path.join(self.t, "victim.json")
        with open(victim, "w", encoding="utf-8") as f:
            json.dump({"password_grace_min": "session", "secret": "keep"}, f)
        os.chmod(victim, 0o644)
        os.symlink(victim, self.settings)
        r = self._run()
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.islink(self.settings))
        self.assertEqual(json.load(open(victim, encoding="utf-8")),
                         {"password_grace_min": "session", "secret": "keep"})
        self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o644)
        auth = json.load(open(self.auth, encoding="utf-8"))
        self.assertEqual(auth.get("password"), "x")


if __name__ == "__main__":
    unittest.main()
