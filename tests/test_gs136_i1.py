#!/usr/bin/env python3
"""Nix 0.13.6 I1: safe-mkdirs --replace keeps only the permission bits (no setuid, setgid or sticky) and refuses a
hardlinked target."""
import os
import shutil
import stat
import tempfile
import unittest

from test_gs136_nix import _load


class ReplaceMode(unittest.TestCase):
    def setUp(self):
        self.m = _load()
        self.t = tempfile.mkdtemp()
        os.chmod(self.t, 0o755)
        self.d = os.path.join(self.t, "admin")
        os.mkdir(self.d)
        self.p = os.path.join(self.d, "f.json")
        with open(self.p, "w") as f:
            f.write("old")

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def _replace(self):
        self.m.replace(self.t, "admin", "f.json", b"new", root_uid=os.getuid())

    def test_special_bits_are_dropped(self):
        for mode in (0o6755, 0o1755):
            os.chmod(self.p, mode)
            self.assertEqual(stat.S_IMODE(os.stat(self.p).st_mode), mode)  # the setup really has the bit
            self._replace()
            self.assertEqual(stat.S_IMODE(os.stat(self.p).st_mode), 0o755, oct(mode))
            self.assertEqual(open(self.p).read(), "new")

    def test_hardlinked_target_is_refused(self):
        other = os.path.join(self.t, "elsewhere")
        os.link(self.p, other)
        with self.assertRaises(self.m.Refused):
            self._replace()
        self.assertEqual(open(other).read(), "old")
        self.assertEqual(open(self.p).read(), "old")
        self.assertEqual(sorted(os.listdir(self.d)), ["f.json"])


if __name__ == "__main__":
    unittest.main()
