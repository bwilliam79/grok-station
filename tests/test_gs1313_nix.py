"""NIX-1313-01 / NIX-1313-02: state/ readers refuse symlinks and oversized files, cap the offered voices,
atomic_write sets the mode only by fchmod, and a failed avatar-auto save is logged."""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_cards as tc  # noqa: E402,F401  (same env as the other relay tests)
import relay  # noqa: E402

BIG = 70 * 1024


class StateReaders(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp()
        self.state = os.path.join(self.top, "state")
        os.mkdir(self.state, 0o770)
        self.outside = os.path.join(self.top, "outside.json")
        self.saved = (relay.STATE_DIR, relay.AVATAR_AUTO_FILE, relay.log, set(relay.VOICES))
        relay.STATE_DIR = self.top
        relay.AVATAR_AUTO_FILE = os.path.join(self.state, "avatar-auto.json")
        self.logs = []
        relay.log = lambda *a: self.logs.append(" ".join(map(str, a)))
        relay.VOICES.clear()

    def tearDown(self):
        relay.STATE_DIR, relay.AVATAR_AUTO_FILE, relay.log, v = self.saved
        relay.VOICES.clear()
        relay.VOICES.update(v)
        shutil.rmtree(self.top, ignore_errors=True)

    def _write(self, path, doc):
        with open(path, "w") as f:
            json.dump(doc, f)

    def _link(self, path, doc):
        self._write(self.outside, doc)
        os.symlink(self.outside, path)

    # seed_offered_voices
    def test_voices_symlink_refused(self):
        self._link(relay.offered_path(), {"voices": ["eve", "rex"]})
        self.assertFalse(relay.seed_offered_voices())
        self.assertEqual(relay.VOICES, set())
        self.assertTrue(any("refused" in l for l in self.logs), self.logs)

    def test_voices_oversized_refused(self):
        self._write(relay.offered_path(), {"voices": ["eve"], "pad": "x" * BIG})
        self.assertFalse(relay.seed_offered_voices())
        self.assertEqual(relay.VOICES, set())
        self.assertTrue(any("oversized" in l for l in self.logs), self.logs)

    def test_voices_over_long_list_capped(self):
        self._write(relay.offered_path(), {"voices": [f"v{i}" for i in range(600)]})
        relay.seed_offered_voices()
        self.assertLessEqual(len(relay.VOICES), 512)
        self.assertTrue(any("capped" in l for l in self.logs), self.logs)

    # _updates_doc
    def test_updates_symlink_refused(self):
        self._link(relay.updates_path(), {"bots": {"jules": True}})
        self.assertEqual(relay._updates_doc(), {})
        self.assertTrue(any("refused" in l for l in self.logs), self.logs)

    def test_updates_oversized_refused(self):
        self._write(relay.updates_path(), {"bots": {"jules": True}, "pad": "x" * BIG})
        self.assertEqual(relay._updates_doc(), {})
        self.assertTrue(any("oversized" in l for l in self.logs), self.logs)

    def test_updates_over_long_list_refused(self):
        self._write(relay.updates_path(), {"bots": {f"bot{i}": True for i in range(8000)}})
        self.assertEqual(relay._updates_doc(), {})

    # _avatar_auto_disk
    def test_avatar_symlink_refused(self):
        self._link(relay.AVATAR_AUTO_FILE, {"jules": 1.0})
        self.assertEqual(relay._avatar_auto_disk(), {})
        self.assertTrue(any("refused" in l for l in self.logs), self.logs)

    def test_avatar_oversized_refused(self):
        self._write(relay.AVATAR_AUTO_FILE, {"jules": 1.0, "pad": "x" * BIG})
        self.assertEqual(relay._avatar_auto_disk(), {})
        self.assertTrue(any("oversized" in l for l in self.logs), self.logs)

    def test_avatar_over_long_list_refused(self):
        self._write(relay.AVATAR_AUTO_FILE, {f"bot{i}": 1.0 for i in range(8000)})
        self.assertEqual(relay._avatar_auto_disk(), {})

    def test_missing_files_fall_back_quietly(self):
        self.assertFalse(relay.seed_offered_voices())
        self.assertEqual(relay._updates_doc(), {})
        self.assertEqual(relay._avatar_auto_disk(), {})
        self.assertEqual(self.logs, [])

    def test_good_files_still_read(self):
        self._write(relay.offered_path(), {"voices": ["eve", "rex"]})
        self._write(relay.updates_path(), {"bots": {"jules": True}})
        self._write(relay.AVATAR_AUTO_FILE, {"jules": 1.0})
        self.assertTrue(relay.seed_offered_voices())
        self.assertEqual(relay._updates_doc(), {"bots": {"jules": True}})
        self.assertEqual(relay._avatar_auto_disk(), {"jules": 1.0})

    # NIX-1313-02
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory modes")
    def test_avatar_save_failure_logged(self):
        os.chmod(self.state, 0o500)
        try:
            relay._avatar_auto_save({"jules": 1.0})
        finally:
            os.chmod(self.state, 0o770)
        hit = [l for l in self.logs if l.startswith("avatar-auto save failed")]
        self.assertTrue(hit and relay.AVATAR_AUTO_FILE in hit[0], self.logs)
        self.assertNotIn("jules", hit[0])


class AtomicWriteMode(unittest.TestCase):
    def test_mode_set_without_chmod_by_path(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "f.json")
        calls = []
        real = relay.os.chmod
        relay.os.chmod = lambda *a, **k: (calls.append(a), real(*a, **k))[1]
        try:
            relay.atomic_write(p, b"{}", mode=0o640)
        finally:
            relay.os.chmod = real
        self.assertEqual(calls, [])
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o640)


if __name__ == "__main__":
    unittest.main()
