#!/usr/bin/env python3
"""Training clips stay on the station: private files, stale partials, stop, prompt tile."""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wake"))
import grok_wake as gw


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class ClipModes(unittest.TestCase):
    def test_session_dir_and_clip_are_private_and_umask_is_restored(self):
        d = tempfile.mkdtemp()
        os.umask(0)
        try:
            session = os.path.join(d, "hey_juniper", "20261029-120000")
            gw._mkdir_0700(os.path.dirname(session))
            gw._mkdir_0700(session)
            wav = os.path.join(session, "00.wav")
            gw._write_wav(wav, b"\x00\x00" * 160)
            meta = os.path.join(session, "session.json")
            gw._write_json_0600(meta, {"phrase": "hey_juniper", "count": 1})
            self.assertEqual(_mode(session), 0o700)
            self.assertEqual(_mode(os.path.dirname(session)), 0o700)
            self.assertEqual(_mode(wav), 0o600)
            self.assertEqual(_mode(meta), 0o600)
            self.assertEqual(os.umask(0), 0)  # the write helpers put the previous mask back
        finally:
            os.umask(0o022)


class StaleAndError(unittest.TestCase):
    def setUp(self):
        self.old = gw.ENROLL_DIR
        self.d = tempfile.mkdtemp()
        gw.ENROLL_DIR = self.d
        self.state = dict(gw.ENROLL_STATE)

    def tearDown(self):
        gw.ENROLL_DIR = self.old
        gw.ENROLL_STATE.clear()
        gw.ENROLL_STATE.update(self.state)

    def test_sweep_drops_partials_and_keeps_a_finished_session(self):
        partial = os.path.join(self.d, "hey_juniper", "20261029-120000")
        done = os.path.join(self.d, "hey_juniper", "20261029-130000")
        os.makedirs(partial)
        open(os.path.join(partial, "00.wav"), "wb").close()
        os.makedirs(done)
        for i in range(5):
            open(os.path.join(done, f"{i:02d}.wav"), "wb").close()
        json.dump({"phrase": "hey_juniper", "count": 5}, open(os.path.join(done, "session.json"), "w"))
        # Five clips but no session.json is still unfinished.
        naked = os.path.join(self.d, "hey_otto", "20261029-140000")
        os.makedirs(naked)
        for i in range(5):
            open(os.path.join(naked, f"{i:02d}.wav"), "wb").close()
        gw.ENROLL_STATE["busy"] = False
        removed = gw.sweep_stale_sessions()
        self.assertFalse(os.path.isdir(partial))
        self.assertFalse(os.path.isdir(naked))
        self.assertTrue(os.path.isfile(os.path.join(done, "session.json")))
        self.assertEqual(sorted(removed), ["20261029-120000", "20261029-140000"])

    def test_a_mic_error_removes_the_partial_session(self):
        n = {"i": 0}

        def read16(raw=False):
            n["i"] += 1
            if n["i"] > 20:
                raise EOFError
            import numpy as np
            return np.zeros(1280, np.int16)

        posted = []
        old = (gw.post_state, gw.time.sleep, gw.subprocess.Popen)
        gw.post_state = lambda state=None, **kw: posted.append(state)
        gw.time.sleep = lambda s: None
        gw.subprocess.Popen = lambda *a, **k: None
        try:
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(EOFError):
                    gw.enroll({"phrase": "hey_juniper", "persona": "juniper", "count": 5}, read16, "default")
        finally:
            gw.post_state, gw.time.sleep, gw.subprocess.Popen = old
        phrase = os.path.join(self.d, "hey_juniper")
        left = os.listdir(phrase) if os.path.isdir(phrase) else []
        self.assertEqual(left, [])  # the partial session directory is gone
        self.assertEqual(posted[-1], "idle")
        self.assertFalse(gw.ENROLL_STATE["busy"])


class StopAndPrompt(unittest.TestCase):
    def setUp(self):
        self.state = dict(gw.ENROLL_STATE)
        gw.ENROLL_STATE.update(busy=False, pause=False, discard=False, prompt=None, heard_text="")

    def tearDown(self):
        gw.ENROLL_STATE.clear()
        gw.ENROLL_STATE.update(self.state)

    def test_stop_discards_and_other_speech_is_ignored(self):
        gw.ENROLL_STATE["busy"] = True
        gw.ENROLL_STATE["pause"] = True
        self.assertIsNone(gw.note_training_speech("louder"))
        self.assertFalse(gw.ENROLL_STATE["discard"])
        self.assertTrue(gw.ENROLL_STATE["pause"])
        self.assertIsNone(gw.note_training_speech("stop the music"))
        self.assertFalse(gw.ENROLL_STATE["discard"])
        self.assertEqual(gw.note_training_speech("Hey Grok, stop."), "discard")
        self.assertTrue(gw.ENROLL_STATE["discard"])
        self.assertFalse(gw.ENROLL_STATE["pause"])
        gw.ENROLL_STATE["discard"] = False
        gw.ENROLL_STATE["heard_text"] = "STOP"
        self.assertEqual(gw._poll_training_speech(), "discard")
        self.assertEqual(gw.ENROLL_STATE["heard_text"], "")

    def test_open_tile_does_not_create_a_dir_or_log_the_phrase(self):
        d = tempfile.mkdtemp()
        old_dir, old_post = gw.ENROLL_DIR, gw.post_state
        posted = []
        gw.ENROLL_DIR = d
        gw.post_state = lambda state=None, **kw: posted.append((state, kw.get("heard"), kw.get("text"), kw.get("who")))
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code, body = gw.open_enroll_tile({"phrase": "hey_juniper", "persona": "juniper", "name": "Juniper"})
            self.assertEqual(code, 200)
            self.assertEqual(body, {"ok": True})
            self.assertEqual(os.listdir(d), [])
            self.assertFalse(gw.ENROLL_STATE["busy"])
            self.assertEqual(posted[0][0], "enroll")
            self.assertEqual(posted[0][1], "1/5|prompt|")
            self.assertIn("Hey Juniper", posted[0][2])
            self.assertNotIn("hey_juniper", buf.getvalue())
            gw.ENROLL_STATE["busy"] = True
            before = os.listdir(d)
            code, body = gw.open_enroll_tile({"persona": "juniper", "phrase": "hey_juniper"})
            self.assertEqual(code, 409)
            self.assertEqual(os.listdir(d), before)
            code, _ = gw.open_enroll_tile({"persona": "juniper", "extra": 1})
            self.assertEqual(code, 400)
        finally:
            gw.ENROLL_DIR, gw.post_state = old_dir, old_post

    def test_discard_of_the_prompt_posts_idle_and_a_stray_discard_stays_409(self):
        posted = []
        old = gw.post_state
        gw.post_state = lambda state=None, **kw: posted.append(state)
        try:
            gw.ENROLL_STATE["prompt"] = {"phrase": "hey_juniper", "persona": "juniper"}
            gw.ENROLL_STATE["busy"] = False
            code, body = gw.enroll_control({"op": "discard"})
            self.assertEqual(code, 200)
            self.assertIsNone(gw.ENROLL_STATE["prompt"])
            self.assertEqual(posted, ["idle"])
            code, body = gw.enroll_control({"op": "discard"})
            self.assertEqual(code, 409)
            code, body = gw.enroll_control({"op": "discard", "phrase": "hey_juniper"})
            self.assertEqual(code, 400)
            code, body = gw.enroll_control({"op": "pause"})
            self.assertEqual(code, 409)
        finally:
            gw.post_state = old


if __name__ == "__main__":
    unittest.main()
