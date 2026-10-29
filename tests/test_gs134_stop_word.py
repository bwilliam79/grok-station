#!/usr/bin/env python3
"""Saying stop during wake-word training discards on-device. No cloud speech, no transcript logs."""
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wake"))
import grok_wake as gw
import stopword

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFF = "stop word: off (on-device recogniser not installed); touch still works"
NOTE = ("vosk: offline speech recognition so saying stop works during wake-word training; "
        "grammar limited to stop, never leaves the station.")
MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
MODEL_SHA = "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498"


def _zeros(raw=False):
    return np.zeros(1280, np.int16)


class _Rec:
    def __init__(self, speak):
        self.speak = speak
        self.resets = 0
        self.feeds = 0

    def reset(self):
        self.resets += 1

    def feed(self, samples):
        self.feeds += 1
        return self.speak(samples, self.feeds)


class StopWord(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old_dir = gw.ENROLL_DIR
        self.state = dict(gw.ENROLL_STATE)
        gw.ENROLL_DIR = self.d
        gw.ENROLL_STATE.update(busy=False, pause=False, discard=False, prompt=None, heard_text="")
        self.stop = dict(gw._STOP)
        gw._STOP["tried"] = False
        gw._STOP["rec"] = None
        self.factory = stopword.recogniser
        self.warned = stopword._warned
        self.post, self.sleep, self.popen = gw.post_state, gw.time.sleep, gw.subprocess.Popen
        gw.post_state = lambda state=None, **kw: None
        gw.time.sleep = lambda s: None
        gw.subprocess.Popen = lambda *a, **k: None

    def tearDown(self):
        gw.ENROLL_DIR = self.old_dir
        gw.ENROLL_STATE.clear()
        gw.ENROLL_STATE.update(self.state)
        gw._STOP.clear()
        gw._STOP.update(self.stop)
        stopword.recogniser = self.factory
        stopword._warned = self.warned
        gw.post_state, gw.time.sleep, gw.subprocess.Popen = self.post, self.sleep, self.popen

    def _use(self, speak):
        rec = _Rec(speak)
        stopword.recogniser = lambda model_dir=None: rec
        gw._STOP["tried"] = False
        gw._STOP["rec"] = None
        return rec

    def test_stop_during_a_clip_deletes_the_session(self):
        raw_n = {"n": 0}
        flag = {"raw": False}

        def read16(raw=False):
            flag["raw"] = bool(raw)
            if raw:
                raw_n["n"] += 1
            return _zeros()

        def speak(samples, n):
            if flag["raw"] and raw_n["n"] >= 3:
                return "STOP"
            if not flag["raw"]:
                return "zztranscript"
            return ""

        rec = self._use(speak)
        buf = io.StringIO()
        with redirect_stdout(buf):
            gw.enroll({"phrase": "hey_juniper", "persona": "juniper", "count": 1}, read16, "default")
        text = buf.getvalue()
        phrase = os.path.join(self.d, "hey_juniper")
        self.assertEqual(os.listdir(phrase), [])  # the session directory is gone
        last = gw.ENROLL_STATE["last"]
        self.assertEqual(last["stage"], "cancelled")
        self.assertEqual(last["message"], "Stopped.")
        self.assertFalse(gw.ENROLL_STATE["busy"])
        self.assertEqual(rec.resets, 1)
        self.assertGreaterEqual(raw_n["n"], 3)
        self.assertNotIn("zztranscript", text)
        self.assertNotIn("STOP", text)

    def test_stop_during_the_prompt_hold_closes_the_tile(self):
        feeds = {"n": 0}

        def speak(samples, n):
            feeds["n"] = n
            if n >= 3:
                return "stop"
            return "zztranscript"

        self._use(speak)
        posted = []
        gw.post_state = lambda state=None, **kw: posted.append(state)
        reads = {"n": 0}

        def read16(raw=False):
            reads["n"] += 1
            if reads["n"] > 50:
                raise AssertionError("prompt did not close")
            return _zeros()

        buf = io.StringIO()
        with redirect_stdout(buf):
            code, body = gw.open_enroll_tile({"phrase": "hey_juniper", "persona": "juniper", "name": "Juniper"})
            self.assertEqual((code, body), (200, {"ok": True}))
            gw._enroll_hold(read16)
        self.assertIsNone(gw.ENROLL_STATE["prompt"])
        self.assertEqual(posted, ["enroll", "idle"])
        self.assertGreaterEqual(feeds["n"], 3)
        self.assertLess(reads["n"], 50)
        self.assertNotIn("zztranscript", buf.getvalue())
        self.assertNotIn("hey_juniper", buf.getvalue())

    def test_other_words_are_ignored(self):
        words = ["top", "go", "[unk]", ""]
        seen = {"i": 0}

        def speak(samples, n):
            word = words[seen["i"] % len(words)]
            seen["i"] += 1
            return word

        self._use(speak)
        gw.ENROLL_STATE["prompt"] = {"phrase": "hey_juniper", "persona": "juniper", "text": "“Hey Juniper”"}
        gw.ENROLL_STATE["busy"] = False
        for _ in range(4):
            gw._feed_training_audio(_zeros())
        self.assertEqual(gw.ENROLL_STATE["prompt"]["phrase"], "hey_juniper")
        self.assertFalse(gw.ENROLL_STATE["discard"])
        gw.ENROLL_STATE["prompt"] = None
        buf = io.StringIO()
        with redirect_stdout(buf):
            gw.enroll({"phrase": "hey_juniper", "persona": "juniper", "count": 1}, _zeros, "default")
        text = buf.getvalue()
        self.assertFalse(gw.ENROLL_STATE["discard"])
        self.assertNotEqual((gw.ENROLL_STATE.get("last") or {}).get("stage"), "cancelled")
        self.assertFalse(gw.ENROLL_STATE["busy"])
        self.assertNotIn("[unk]", text)
        self.assertNotIn("top", text)
        self.assertNotIn("zztranscript", text)

    def test_missing_recogniser_still_discards_by_touch_and_warns_once(self):
        stopword._warned = False
        gw._STOP["tried"] = False
        gw._STOP["rec"] = None
        stopword.recogniser = self.factory
        seen, release = threading.Event(), threading.Event()
        n = {"i": 0}

        def read16(raw=False):
            n["i"] += 1
            if n["i"] == 8:
                seen.set()
                if not release.wait(5):
                    raise RuntimeError("not released")
            return _zeros()

        buf = io.StringIO()
        err = []

        def run():
            try:
                gw.enroll({"phrase": "hey_juniper", "persona": "juniper", "count": 1}, read16, "default")
            except Exception as exc:
                err.append(exc)

        th = threading.Thread(target=run)
        try:
            with redirect_stdout(buf):
                th.start()
                self.assertTrue(seen.wait(5))
                code, body = gw.enroll_control({"op": "discard"})
                release.set()
                th.join(5)
                self.assertIsNone(stopword.recogniser())
            self.assertFalse(th.is_alive())
            self.assertEqual(err, [])
            self.assertEqual((code, body.get("op")), (200, "discard"))
            phrase = os.path.join(self.d, "hey_juniper")
            self.assertEqual(os.listdir(phrase), [])
            last = gw.ENROLL_STATE["last"]
            self.assertEqual(last["stage"], "cancelled")
            self.assertEqual(last["message"], "Stopped.")
            self.assertFalse(gw.ENROLL_STATE["busy"])
            lines = [ln for ln in buf.getvalue().splitlines() if ln == OFF]
            self.assertEqual(lines, [OFF])
        finally:
            release.set()
            th.join(2)


class RecogniserContract(unittest.TestCase):
    def test_feed_uses_result_then_partial_and_reset(self):
        class Kaldi:
            def __init__(self):
                self.calls = 0
                self.blob = b""
                self.reset = False

            def AcceptWaveform(self, data):
                self.calls += 1
                self.blob = data
                return self.calls >= 2

            def Result(self):
                return '{"text": " stop "}'

            def PartialResult(self):
                return '{"partial": "sto"}'

            def Reset(self):
                self.reset = True

        kaldi = Kaldi()
        rec = stopword._Recognizer(kaldi)
        samples = np.array([1, -2, 3], np.int16)
        self.assertEqual(rec.feed(samples), "sto")
        self.assertEqual(kaldi.blob, samples.tobytes())
        self.assertEqual(rec.feed(samples), "stop")
        rec.reset()
        self.assertTrue(kaldi.reset)

    def test_vosk_is_imported_only_inside_the_factory(self):
        src = open(os.path.join(ROOT, "wake", "stopword.py")).read()
        self.assertLess(src.find("def recogniser"), src.find("import vosk"))
        self.assertNotIn("import vosk", src.split("def recogniser", 1)[0])
        self.assertIn('json.dumps(["stop", "[unk]"])', src)
        self.assertIn("SetLogLevel(-1)", src)
        self.assertIn("16000", src)
        self.assertEqual(stopword._OFF, OFF)


class InstallStopWord(unittest.TestCase):
    def test_model_is_verified_before_unzip_and_vosk_is_pinned(self):
        sh = open(os.path.join(ROOT, "install.sh")).read()
        req = open(os.path.join(ROOT, "wake", "requirements-vosk.txt")).read()
        self.assertIn(MODEL_URL, sh)
        self.assertIn(MODEL_SHA, sh)
        self.assertLess(sh.index("sha256sum"), sh.index("tools/vosk-unpack.py"))
        self.assertLess(sh.index('rm -f "$tmp"'), sh.index("tools/vosk-unpack.py"))
        self.assertIn("requirements-vosk.txt || echo", sh)
        self.assertIn(NOTE, sh)
        self.assertIn("vosk==0.3.44", req)
        self.assertIn(NOTE, req)
        files = open(os.path.join(ROOT, "tools", "install-files.sh")).read()
        self.assertIn('"$S/wake/stopword.py"', files)
        r = subprocess.run(["sh", "-n", os.path.join(ROOT, "install.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        dry = subprocess.run(["sh", os.path.join(ROOT, "install.sh"), "--dry-run", "--verbose"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(MODEL_URL, dry.stdout)
        self.assertNotIn(MODEL_SHA, dry.stdout)
        self.assertNotRegex(dry.stdout, r"\b[0-9a-f]{48,}\b")


if __name__ == "__main__":
    unittest.main()
