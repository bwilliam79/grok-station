import re
"""Wake client: persona events keep the listening phase, 16 kHz capture upsampling, patience. No network, no audio."""
import json, os, sys, types, unittest
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wake"))
import grok_wake as gw


class FakeWS:
    def __init__(self, msgs): self.msgs = msgs
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return iter(self.msgs)
    def send(self, b): pass


def run_session(msgs, persona="kai"):
    posted = []
    mod = types.ModuleType("websockets.sync.client")
    mod.connect = lambda url, **kw: (posted.append(("url", url)), FakeWS([json.dumps(m) for m in msgs]))[1]
    sys.modules.update({"websockets": types.ModuleType("websockets"), "websockets.sync": types.ModuleType("websockets.sync"),
                        "websockets.sync.client": mod})
    old = gw.post_state, gw.RELAY_URL
    gw.post_state = lambda state, **kw: posted.append((state, kw.get("who")))
    gw.RELAY_URL = "ws://relay.test/ws"
    try:
        s = gw.Session("default", persona=persona)
        while s.active:
            pass
    finally:
        gw.post_state, gw.RELAY_URL = old
    return posted


class TestWake(unittest.TestCase):
    def test_persona_event_keeps_listening(self):
        p = run_session([{"type": "ready"}, {"type": "persona", "slug": "kai", "name": "Kai"}, {"type": "timeout"}])
        self.assertEqual(re.sub(r"[?&]t1=[^&]*$", "", p[0][1]), "ws://relay.test/ws?persona=kai")
        self.assertIn(("listening", "Kai"), p)
        self.assertNotIn("thinking", [x[0] for x in p])

    def test_persona_event_while_responding_is_thinking(self):
        p = run_session([{"type": "response_started"}, {"type": "persona", "slug": "rhea", "name": "Rhea"}, {"type": "done"}],
                        persona="grok")
        self.assertEqual(re.sub(r"[?&]t1=[^&]*$", "", p[0][1]), "ws://relay.test/ws")
        self.assertIn(("thinking", "Rhea"), p)

    def test_up24(self):
        x = np.sin(2 * np.pi * 440 * np.arange(1280) / 16000)
        y = gw.up24(x, 0.0)
        self.assertEqual(len(y), 1920)
        self.assertAlmostEqual(y[3], x[1], places=6)  # 24 kHz sample 3 == 16 kHz sample 1 (one-sample delay)

    def test_patience(self):
        gw.WORDS.clear()
        gw.WORDS.update(hey_rhea=gw._word({"name": "hey_rhea", "persona": "rhea", "threshold": 0.8, "patience": 2}))
        d = gw.Detector()
        self.assertIsNone(d.feed({"hey_rhea": 0.9}))
        self.assertEqual(d.feed({"hey_rhea": 0.85}), "hey_rhea")
        d.reset(); d.feed({"hey_rhea": 0.9})
        self.assertIsNone(d.feed({"hey_rhea": 0.5}))

    def test_repo_config(self):
        cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "wake", "wakewords.json")))
        names = {w["name"]: w for w in cfg["words"]}
        self.assertNotIn("hey_finn", names)
        self.assertTrue(names["hey_rhea"]["provisional"])
        for w in cfg["words"]:
            self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(__file__), "..", "wake", "models", w["name"] + ".onnx")))



class GainClip(unittest.TestCase):
    def test_default_gain(self):
        self.assertEqual(gw.default_gain("EMEET"), "2")
        self.assertEqual(gw.default_gain("Razer"), "4")

    def test_clip_on_raw(self):
        quiet = np.full(4000, 12000, np.int16)  # would clip after x4 gain, but the raw mic signal is fine
        self.assertFalse(gw.is_clipped(quiet))
        loud = quiet.copy(); loud[:100] = 32767
        self.assertTrue(gw.is_clipped(loud))

if __name__ == "__main__":
    unittest.main()


class TestEnroll(unittest.TestCase):
    def test_trim_clip(self):
        sr = 16000
        x = np.zeros(int(2.4 * sr), np.int16)
        self.assertIsNone(gw.trim_clip(x))
        x[sr // 2: sr // 2 + 12000] = (np.sin(np.arange(12000) / 5) * 8000).astype(np.int16)
        y = gw.trim_clip(x)
        self.assertTrue(12000 <= len(y) <= 12000 + 8000 + 640)

    def test_enroll_phrases(self):
        old = gw.WAKE_CONFIG
        gw.WAKE_CONFIG = os.path.join(os.path.dirname(__file__), "..", "wake", "wakewords.json")
        try:
            ph = gw.enroll_phrases()
        finally:
            gw.WAKE_CONFIG = old
        self.assertEqual(ph["hey_finn"], "finn"); self.assertEqual(ph["hey_hank"], "hank"); self.assertEqual(ph["hey_rhea"], "rhea")


class EnrollFlow(unittest.TestCase):
    def run_enroll(self, n=10, loud_at=(), topup=False):
        import tempfile, threading
        posted, uploads, reads = [], [], {"n": 0}
        clip = {"k": 0}

        def read16(raw=False):
            reads["n"] += 1
            return np.zeros(1280, np.int16)

        def fake_concat(parts):  # recording window: speech (or a clipped clip) instead of silence
            clip["k"] += 1
            t = np.arange(len(parts) * 1280) / 16000.0
            amp = 32767 if clip["k"] in loud_at else 6000
            y = (amp * np.sin(2 * np.pi * 220 * t)).astype(np.int16)
            y[: 16000 // 2] = 0; y[-16000 // 2:] = 0
            return y
        old = (gw.post_state, gw.relay_post, gw.subprocess.Popen, gw.ENROLL_DIR, gw.np.concatenate, gw.follow_training, gw.time.sleep)
        gw.post_state = lambda state, **kw: posted.append(kw.get("heard", "").split("|")[1] if "|" in kw.get("heard", "") else state)
        gw.relay_post = lambda path, body, ctype="": uploads.append((path, body if ctype == "application/json" else b""))
        gw.subprocess.Popen = lambda *a, **k: None
        gw.ENROLL_DIR = tempfile.mkdtemp()
        gw.np.concatenate = fake_concat
        gw.follow_training = lambda *a, **k: None
        gw.time.sleep = lambda s: None
        try:
            if topup:
                os.makedirs(os.path.join(gw.ENROLL_DIR, "hey_finn", "20261019-150740"))
            gw.enroll({"phrase": "hey_finn", "persona": "finn", "count": n, "topup": topup}, read16, "default")
        finally:
            (gw.post_state, gw.relay_post, gw.subprocess.Popen, gw.ENROLL_DIR, gw.np.concatenate, gw.follow_training, gw.time.sleep) = old
        return posted, uploads

    def test_stages_and_default_count(self):
        self.assertEqual(gw.ENROLL_COUNT, 10); self.assertEqual(gw.TOPUP_COUNT, 10)
        self.assertEqual((gw.LEAD_S, gw.CLIP_S, gw.RETRY_S), (0.9, 2.4, 1.5))
        posted, uploads = self.run_enroll(loud_at=(2,))
        self.assertEqual(posted[0], "prompt")
        self.assertEqual(posted[1:4], ["ready", "recording", "saved"])
        self.assertIn("loud", posted)
        self.assertEqual(posted[-1], "uploaded")
        self.assertEqual(sum(1 for p, _ in uploads if p.endswith(".wav")), 10)
        done = json.loads([b for p, b in uploads if p.endswith("/done")][0])
        self.assertEqual(done["count"], 10)
        self.assertNotIn("prior", done)

    def test_topup_names_prior_session(self):
        _, uploads = self.run_enroll(topup=True)
        done = json.loads([b for p, b in uploads if p.endswith("/done")][0])
        self.assertEqual(done["prior"], "20261019-150740")
