"""NIX-1313-09: created-file cleanup unlinks only the file it created (st_dev/st_ino of the open fd vs lstat path)."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "relay"))
import safe_io  # noqa: E402

REAL_REPLACE = os.replace


class SwapBeforeCleanup(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "f")
        safe_io.set_log(lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def swap(self, target):
        other = os.path.join(self.d, "other")
        with open(other, "w") as f:
            f.write("theirs")
        REAL_REPLACE(other, target)  # a different inode now sits at the path

    def fchmod_swaps(self, target):
        def bad(fd, mode):
            self.swap(target)
            raise PermissionError(1, "no")
        return mock.patch.object(safe_io.os, "fchmod", bad)

    def survives(self, target):
        with open(target) as f:
            self.assertEqual(f.read(), "theirs")

    def test_open_write_keeps_swapped_file(self):
        with self.fchmod_swaps(self.p), self.assertRaises(OSError):
            safe_io.open_write(self.p, "t")
        self.survives(self.p)

    def test_open_write_excl_keeps_swapped_file(self):
        with self.fchmod_swaps(self.p), self.assertRaises(OSError):
            safe_io.open_write(self.p, "t", excl=True)
        self.survives(self.p)

    def test_open_append_keeps_swapped_file(self):
        with self.fchmod_swaps(self.p), self.assertRaises(OSError):
            safe_io.open_append(self.p, "t")
        self.survives(self.p)

    def test_replace_file_tmp_cleanup_keeps_swapped_file(self):
        seen = {}

        def bad_replace(src, dst):
            seen["tmp"] = src
            self.swap(src)
            raise PermissionError(1, "no")
        with mock.patch.object(safe_io.os, "replace", bad_replace), self.assertRaises(OSError):
            safe_io.replace_file(self.p, "x", "t")
        self.survives(seen["tmp"])
        self.assertFalse(os.path.exists(self.p))

    def test_own_file_still_removed(self):
        with mock.patch.object(safe_io.os, "fchmod", side_effect=PermissionError(1, "no")), self.assertRaises(OSError):
            safe_io.open_write(self.p, "t")
        self.assertFalse(os.path.exists(self.p))


if __name__ == "__main__":
    unittest.main()
