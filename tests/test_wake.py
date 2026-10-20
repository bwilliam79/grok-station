import re
"""Wake client: persona events keep the listening phase, 16 kHz capture upsampling, patience. No network, no audio."""
import io, json, os, sys, tempfile, types, unittest
from contextlib import redirect_stdout
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
        old = gw.WAKE_CONFIG, gw.OVERRIDES
        gw.WAKE_CONFIG = os.path.join(os.path.dirname(__file__), "..", "wake", "wakewords.json")
        gw.OVERRIDES = os.path.join(tempfile.mkdtemp(), "missing-overrides.json")
        try:
            ph = gw.enroll_phrases()
        finally:
            gw.WAKE_CONFIG, gw.OVERRIDES = old
        self.assertEqual(ph["hey_finn"], "finn"); self.assertEqual(ph["hey_hank"], "hank"); self.assertEqual(ph["hey_rhea"], "rhea")

    def test_override_persona_and_parse_enroll(self):
        d = tempfile.mkdtemp()
        cfg, ov = os.path.join(d, "wakewords.json"), os.path.join(d, "wake-overrides.json")
        with open(cfg, "w") as f:
            json.dump({"words": [{"name": "hey_finn", "persona": "finn"}], "enroll": [{"name": "hey_hank", "persona": "hank"}]}, f)
        with open(ov, "w") as f:
            json.dump({"hey_kitchen": {"persona": "kitchen-bot"}, "hey_finn": {"enabled": False, "threshold": 0.3, "persona": "grok"}}, f)
        old = gw.WAKE_CONFIG, gw.OVERRIDES
        gw.WAKE_CONFIG, gw.OVERRIDES = cfg, ov
        try:
            ph = gw.enroll_phrases()
            self.assertEqual(ph["hey_kitchen"], "kitchen-bot")
            self.assertEqual(ph["hey_finn"], "grok")  # override persona wins over the config
            self.assertEqual(ph["hey_hank"], "hank")
            self.assertEqual(gw.parse_enroll({"phrase": "hey_hank", "count": 10}),
                             {"phrase": "hey_hank", "persona": "hank", "count": 10, "topup": False})
            self.assertEqual(gw.parse_enroll({"phrase": "hey_hank", "persona": "grok"})["persona"], "hank")
            self.assertEqual(gw.parse_enroll({"phrase": "hey_kitchen"})["persona"], "kitchen-bot")
            self.assertEqual(gw.parse_enroll({"phrase": "okay_finn", "persona": "finn", "count": 12, "topup": True}),
                             {"phrase": "okay_finn", "persona": "finn", "count": 12, "topup": True})
            self.assertIsNone(gw.parse_enroll({"phrase": "okay_finn"}))
            self.assertIsNone(gw.parse_enroll({"phrase": "okay_finn", "persona": "Not A Slug"}))
            self.assertEqual(gw.parse_enroll({"phrase": "hey_hank", "count": 5}), {"error": "count"})
            self.assertIsNone(gw.parse_enroll({"phrase": "hey_hank", "count": "ten"}))
        finally:
            gw.WAKE_CONFIG, gw.OVERRIDES = old
        self.assertTrue(gw.phrase_ok("hey_kitchen")); self.assertTrue(gw.phrase_ok("okay_finn")); self.assertTrue(gw.phrase_ok("hey_finn"))
        self.assertFalse(gw.phrase_ok("hey")); self.assertFalse(gw.phrase_ok("a_b_c_d_e"))
        self.assertFalse(gw.phrase_ok("extraordinarily_supercalifragilistic"))
        self.assertEqual(gw.relay_persona("kitchen-bot"), "grok")
        self.assertEqual(gw.relay_persona("finn"), "finn")
        self.assertTrue(gw.persona_ok("kitchen-bot")); self.assertTrue(gw.persona_ok("grok"))

    def test_apply_overrides_persona_wins_and_off_still_removes(self):
        entries = {"hey_kitchen": {"name": "hey_kitchen", "persona": "grok", "threshold": 0.5},
                   "hey_finn": {"name": "hey_finn", "persona": "finn", "threshold": 0.5}}
        ov = {"hey_kitchen": {"persona": "kitchen-bot", "threshold": 0.3},
              "hey_finn": {"enabled": False, "persona": "grok"},
              "okay_finn": {"persona": "finn"}}
        out = gw.apply_wake_overrides(entries, ov)
        self.assertEqual(out["hey_kitchen"]["persona"], "kitchen-bot")
        self.assertEqual(out["hey_kitchen"]["threshold"], 0.3)
        self.assertNotIn("hey_finn", out)
        self.assertNotIn("okay_finn", out)  # a mapping with no model yet is not inserted here

    def test_enroll_stage_ignores_a_stale_session(self):
        old = gw.ENROLL_STATE.get("last")
        try:
            gw.ENROLL_STATE["last"] = None
            gw.enroll_stage("training", "Training.", "hey_finn", "s1")
            self.assertIsNone(gw.ENROLL_STATE["last"])
            gw.ENROLL_STATE["last"] = {"phrase": "hey_finn", "session": "s1", "stage": "recording",
                                       "message": "Say it on the station.", "persona": "finn"}
            gw.enroll_stage("uploading", "Sending the clips.", "hey_finn", "other")
            gw.enroll_stage("uploading", "Sending the clips.", "hey_kitchen", "s1")
            self.assertEqual(gw.ENROLL_STATE["last"]["stage"], "recording")
            gw.enroll_stage("training", "Training.", "hey_finn", "s1")
            self.assertEqual(gw.ENROLL_STATE["last"]["stage"], "training")
            self.assertEqual(gw.ENROLL_STATE["last"]["message"], "Training.")
        finally:
            gw.ENROLL_STATE["last"] = old


class EnrollFlow(unittest.TestCase):
    def run_enroll(self, n=10, loud_at=(), topup=False, phrase="hey_finn", persona="finn"):
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
                os.makedirs(os.path.join(gw.ENROLL_DIR, phrase, "20261019-150740"))
            gw.enroll({"phrase": phrase, "persona": persona, "count": n, "topup": topup}, read16, "default")
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

    def test_hyphenated_bot_is_grok_on_the_relay_only(self):
        _, uploads = self.run_enroll(phrase="hey_kitchen", persona="kitchen-bot")
        done = json.loads([b for p, b in uploads if p.endswith("/done")][0])
        self.assertEqual(done["persona"], "grok")
        self.assertEqual(gw.ENROLL_STATE["last"]["persona"], "kitchen-bot")
        self.assertEqual(gw.ENROLL_STATE["last"]["stage"], "training")
        self.assertEqual(gw.ENROLL_STATE["last"]["message"], "Training.")


class _FakeDisplay:
    """Stand-in for the station's POST /state. Records the JSON body and returns an empty 200."""

    def __init__(self):
        self.bodies = []

    def urlopen(self, req, timeout=2):
        data = getattr(req, "data", None) or b"{}"
        self.bodies.append(json.loads(data.decode()))

        class Resp:
            def read(self):
                return b"{}"
        return Resp()


class ToolCardForward(unittest.TestCase):
    """Cards and timer lists are posted to the display; the log names types, never values."""

    def setUp(self):
        import tempfile
        self.display = _FakeDisplay()
        self.old = (gw.urllib.request.urlopen, gw.TIMERS_FILE, gw.LIVE["session"], gw.LIVE["play"], dict(gw.TIMERS))
        gw.urllib.request.urlopen = self.display.urlopen
        gw.TIMERS_FILE = os.path.join(tempfile.mkdtemp(), "timers.json")
        gw.TIMERS.clear()
        gw.LIVE["session"] = False
        gw.LIVE["play"] = None

    def tearDown(self):
        gw.urllib.request.urlopen, gw.TIMERS_FILE, gw.LIVE["session"], gw.LIVE["play"], saved = self.old
        gw.TIMERS.clear()
        gw.TIMERS.update(saved)

    def test_card_and_timer_list_and_logs(self):
        import tempfile
        gw.TIMERS_FILE = os.path.join(tempfile.mkdtemp(), "timers.json")
        posted_url = []
        mod = types.ModuleType("websockets.sync.client")
        msgs = [
            {"type": "ready"},
            {"type": "heard", "text": "swordfish time"},
            {"type": "card", "kind": "time", "zone_label": "London", "hhmm": "11:05 PM", "day": "Mon",
             "offset": "+6 h", "place": "Paris", "lat": 51.5, "lon": -0.1},
            {"type": "card", "kind": "nope", "hhmm": "secret"},
            {"type": "timer_set", "id": "ab12cd", "seconds": 45, "label": "pasta"},
            {"type": "timeout"},
        ]
        mod.connect = lambda url, **kw: (posted_url.append(url), FakeWS([json.dumps(m) for m in msgs]))[1]
        keys = ("websockets", "websockets.sync", "websockets.sync.client")
        saved = {k: sys.modules.get(k) for k in keys}
        sys.modules.update({"websockets": types.ModuleType("websockets"), "websockets.sync": types.ModuleType("websockets.sync"),
                            "websockets.sync.client": mod})
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                s = gw.Session("default", persona="grok")
                while s.active:
                    pass
                gw.timer_event({"type": "timer_cancel", "id": "ab12cd"})
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        log = buf.getvalue()
        for secret in ("London", "11:05", "swordfish", "pasta", "+6 h", "ab12cd", "Paris", "51.5"):
            self.assertNotIn(secret, log, secret)
        self.assertIn("relay: card time", log)
        self.assertIn("relay: card ignored", log)
        self.assertIn("timer set", log)
        self.assertIn("timer cancelled", log)
        self.assertNotIn("state post failed", log)
        cards = [b["card"] for b in self.display.bodies if isinstance(b.get("card"), dict)]
        self.assertTrue(cards)
        self.assertEqual(cards[0]["kind"], "time")
        self.assertEqual(cards[0]["zone_label"], "London")
        self.assertNotIn("place", cards[0])
        self.assertNotIn("lat", cards[0])
        self.assertNotIn("lon", cards[0])
        lists = [b["timers"] for b in self.display.bodies if "timers" in b]
        self.assertTrue(lists)
        self.assertEqual(lists[0][0]["id"], "ab12cd")
        self.assertEqual(lists[0][0]["label"], "pasta")
        self.assertFalse(lists[0][0]["ringing"])
        self.assertGreater(lists[0][0]["ends_at_epoch"], 1_000_000_000)
        self.assertEqual(lists[-1], [])

    def test_ring_posts_the_list_without_logging_the_label(self):
        import tempfile
        gw.TIMERS_FILE = os.path.join(tempfile.mkdtemp(), "timers.json")
        gw.LIVE["play"] = "dummy"
        gw.TIMERS["ee33ff"] = {"label": "eggs", "due": __import__("time").time() - 2}
        old_run, old_sleep = gw.subprocess.run, gw.time.sleep
        gw.subprocess.run = lambda *a, **k: None
        gw.time.sleep = lambda s: None
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                gw.timers_tick()
                with gw.TIMERS_LOCK:
                    gw.TIMERS["ee33ff"]["ring_until"] = gw.time.time() - 1
                gw.timers_tick()
        finally:
            gw.subprocess.run, gw.time.sleep = old_run, old_sleep
        log = buf.getvalue()
        self.assertNotIn("eggs", log)
        self.assertIn("timer ringing", log)
        self.assertIn("timer clear", log)
        self.assertNotIn("state post failed", log)
        ringing = [b for b in self.display.bodies if b.get("timers") and b["timers"][0].get("ringing")]
        self.assertTrue(ringing)
        self.assertEqual(ringing[0]["timers"][0]["label"], "eggs")
        spoken = [b for b in self.display.bodies if "eggs" in str(b.get("text", ""))]
        self.assertTrue(spoken)
        self.assertTrue(spoken[0].get("done"))
        cleared = [b for b in self.display.bodies if b.get("timers") == []]
        self.assertTrue(cleared)
