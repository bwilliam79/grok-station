#!/usr/bin/env python3
"""tools/vosk-unpack.py: only plain entries under the model folder, and no model folder after a failure."""
import hashlib
import importlib.util
import os
import stat
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("vosk_unpack", os.path.join(ROOT, "tools", "vosk-unpack.py"))
vu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vu)
MODEL = "vosk-model-small-en-us-0.15"


def _zip(entries):
    """entries: (name, data, mode or None). Returns (path, sha)."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "m.zip")
    with zipfile.ZipFile(path, "w") as z:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            z.writestr(info, data)
    return path, hashlib.sha256(open(path, "rb").read()).hexdigest()


GOOD = [(MODEL + "/README", b"r", stat.S_IFREG | 0o644), (MODEL + "/am/final.mdl", b"m", stat.S_IFREG | 0o644)]


class Unpack(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp()

    def _left(self):
        return sorted(os.listdir(self.dest))

    def test_good_zip_lands_with_marker(self):
        z, sha = _zip(GOOD)
        self.assertTrue(vu.unpack(z, self.dest, MODEL, sha))
        self.assertEqual(self._left(), [MODEL])
        self.assertEqual(open(os.path.join(self.dest, MODEL, ".sha256")).read().strip(), sha)

    def test_wrong_sha_leaves_nothing(self):
        z, _ = _zip(GOOD)
        self.assertFalse(vu.unpack(z, self.dest, MODEL, "0" * 64))
        self.assertEqual(self._left(), [])

    def test_symlink_entry_is_rejected(self):
        z, sha = _zip(GOOD + [(MODEL + "/link", b"/etc/shadow", stat.S_IFLNK | 0o777)])
        self.assertFalse(vu.unpack(z, self.dest, MODEL, sha))
        self.assertEqual(self._left(), [])

    def test_absolute_path_is_rejected(self):
        for bad in ("/etc/evil", "/" + MODEL + "/x", "C:/evil", "\\evil"):
            z, sha = _zip(GOOD + [(bad, b"x", stat.S_IFREG | 0o644)])
            self.assertFalse(vu.unpack(z, self.dest, MODEL, sha), bad)
            self.assertEqual(self._left(), [], bad)

    def test_parent_and_outside_entries_are_rejected(self):
        for bad in (MODEL + "/../evil", "other/file", "../x"):
            z, sha = _zip(GOOD + [(bad, b"x", stat.S_IFREG | 0o644)])
            self.assertFalse(vu.unpack(z, self.dest, MODEL, sha), bad)
            self.assertEqual(self._left(), [], bad)

    def test_failure_partway_through_extract_leaves_no_model_folder(self):
        z, sha = _zip(GOOD)
        real = {"n": 0}

        def flaky(info, path):
            real["n"] += 1
            if real["n"] == 2:
                raise OSError("disk full")
            with zipfile.ZipFile(z) as zz:
                zz.extract(info, path)

        self.assertFalse(vu.unpack(z, self.dest, MODEL, sha, extract=flaky))
        self.assertEqual(real["n"], 2)
        self.assertEqual(self._left(), [])

    def test_failed_update_keeps_the_previous_verified_model(self):
        z, sha = _zip(GOOD)
        self.assertTrue(vu.unpack(z, self.dest, MODEL, sha))

        def boom(info, path):
            raise OSError("disk full")

        self.assertFalse(vu.unpack(z, self.dest, MODEL, sha, extract=boom))
        self.assertEqual(self._left(), [MODEL])
        self.assertTrue(os.path.isfile(os.path.join(self.dest, MODEL, "am", "final.mdl")))

    def test_install_uses_the_unpacker_after_the_checksum(self):
        sh = open(os.path.join(ROOT, "install.sh")).read()
        self.assertLess(sh.index("sha256sum \"$tmp\""), sh.index("tools/vosk-unpack.py"))
        self.assertNotIn("extractall", sh)


if __name__ == "__main__":
    unittest.main()
