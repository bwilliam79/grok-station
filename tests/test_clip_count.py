"""Finished enroll sessions are the only recordings that count.

A full session is a YYYYMMDD-HHMMSS directory with a session.json object and at least
5 NN.wav clips (Train more is 5 too; an older session of 10 still counts). Empty dirs,
partial sessions, missing markers, and a later copy of the same clip bytes do not add
to the count. The admin list and the wake trainer share that rule.
"""
import json, os, sys, tempfile, unittest, wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "admin"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wake"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_admin  # noqa: E402  (sets the admin test env before grok_admin is first imported)
ga = test_admin.ga
import grok_wake as gw


def _wav(path, mark):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes([mark, 0]) * 80)


def _session(root, phrase, ses, n=10, mark=1, marker=True, marker_doc=None, extra=None):
    d = os.path.join(root, phrase, ses)
    os.makedirs(d, exist_ok=True)
    for i in range(n):
        _wav(os.path.join(d, f"{i:02d}.wav"), (mark + i) & 0xFF)
    if extra:
        open(os.path.join(d, extra), "wb").write(b"notes")
    if marker:
        doc = {"phrase": phrase, "count": n} if marker_doc is None else marker_doc
        with open(os.path.join(d, "session.json"), "w") as f:
            json.dump(doc, f)
    return d


class ClipCount(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gs-clips-")
        self.old_inbox = ga.ENROLL_INBOX
        self.old_enroll = gw.ENROLL_DIR
        ga.ENROLL_INBOX = self.tmp
        gw.ENROLL_DIR = self.tmp

    def tearDown(self):
        ga.ENROLL_INBOX = self.old_inbox
        gw.ENROLL_DIR = self.old_enroll
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_partial_empty_and_duplicate_sessions(self):
        _session(self.tmp, "hey_rhea", "20261026-120000", n=10, mark=1)          # full
        _session(self.tmp, "hey_rhea", "20261026-120100", n=4, mark=2)           # partial (under 5)
        _session(self.tmp, "hey_rhea", "20261026-120200", n=10, mark=3, marker=False)  # no marker
        os.makedirs(os.path.join(self.tmp, "hey_rhea", "20261026-120300"))       # empty
        _session(self.tmp, "hey_rhea", "20261026-120400", n=10, mark=1)          # duplicate of the first
        _session(self.tmp, "hey_rhea", "20261026-120500", n=11, mark=9)          # longer than a session, new audio
        _session(self.tmp, "hey_rhea", "not-a-session", n=10, mark=4)            # wrong directory name
        _session(self.tmp, "hey_rhea", "20261026-120600", n=10, mark=5, marker_doc=["nope"])
        d = _session(self.tmp, "hey_rhea", "20261026-120700", n=10, mark=6)
        open(os.path.join(d, "noise.wav"), "wb").write(b"noise")

        self.assertEqual(ga.clip_count("hey_rhea"), 10 + 11 + 10)
        files = ga.finished_clip_files(os.path.join(self.tmp, "hey_rhea"))
        self.assertTrue(all(os.path.basename(p) != "noise.wav" for p in files))
        self.assertTrue(any("20261026-120000" in p for p in files))
        self.assertFalse(any("20261026-120400" in p for p in files))  # later duplicate dropped
        self.assertFalse(any("20261026-120100" in p for p in files))
        self.assertFalse(any("20261026-120200" in p for p in files))

        sessions = gw.finished_sessions("hey_rhea")
        self.assertEqual([os.path.basename(d) for d, _names, _noise in sessions],
                         ["20261026-120000", "20261026-120500", "20261026-120700"])
        clips, noise = gw.phrase_clips("hey_rhea")
        self.assertEqual(len(clips), 10 + 11 + 10)
        self.assertEqual(noise, [])  # the noise file above is not a readable wav

        # a later full session of new audio still counts; identical bytes do not
        _session(self.tmp, "hey_kai", "20261026-130000", n=10, mark=20)
        _session(self.tmp, "hey_kai", "20261026-130100", n=10, mark=21)
        self.assertEqual(ga.clip_count("hey_kai"), 20)
        self.assertEqual(len(gw.phrase_clips("hey_kai")[0]), 20)

    def test_topup_session_of_five_counts(self):
        _session(self.tmp, "hey_finn", "20261023-080000", n=10, mark=1)
        _session(self.tmp, "hey_finn", "20261023-090000", n=6, mark=2, marker_doc={"phrase": "hey_finn", "count": 6, "prior": "20261023-080000"})
        _session(self.tmp, "hey_finn", "20261023-100000", n=4, mark=3)  # first session with only 4: not kept
        got = gw.finished_sessions("hey_finn")
        self.assertEqual([len(n) for _d, n, _z in got], [10, 6])
        self.assertEqual(ga.clip_count("hey_finn"), 16)

    def test_existing_ten_clip_session_still_counts(self):
        _session(self.tmp, "hey_ash", "20261020-080000", n=10, mark=1)
        _session(self.tmp, "hey_ash", "20261020-090000", n=5, mark=2,
                 marker_doc={"phrase": "hey_ash", "count": 5, "prior": "20261020-080000"})
        _session(self.tmp, "hey_ash", "20261020-100000", n=4, mark=3)  # short first session: not kept
        got = gw.finished_sessions("hey_ash")
        self.assertEqual([len(n) for _d, n, _z in got], [10, 5])
        self.assertEqual(ga.clip_count("hey_ash"), 15)
        self.assertEqual(len(gw.phrase_clips("hey_ash")[0]), 15)

    def test_five_clip_first_session_counts_four_does_not(self):
        _session(self.tmp, "hey_milo", "20261021-080000", n=5, mark=1)
        _session(self.tmp, "hey_milo", "20261021-090000", n=4, mark=2)
        self.assertEqual([len(n) for _d, n, _z in gw.finished_sessions("hey_milo")], [5])
        self.assertEqual(ga.clip_count("hey_milo"), 5)

    def test_outside_the_inbox_counts_as_nothing(self):
        other = tempfile.mkdtemp(prefix="gs-clips-out-")
        try:
            _session(other, "hey_rhea", "20261026-120000")
            self.assertEqual(ga.finished_clip_files(os.path.join(other, "hey_rhea")), [])
            self.assertEqual(ga.clip_count("../hey_rhea"), 0)
        finally:
            import shutil
            shutil.rmtree(other, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
