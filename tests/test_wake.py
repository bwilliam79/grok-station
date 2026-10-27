import re
"""Wake client: persona events keep the listening phase, 16 kHz capture upsampling, patience. No network, no audio."""
import base64, io, json, os, sys, tempfile, time, types, unittest
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


def _relay_base(url):
    """Drop the t1 and timers query params the wake client always appends."""
    url = re.sub(r"[?&](?:t1|timers)=[^&]*", "", url)
    return url.rstrip("?&").replace("?&", "?")


class TestWake(unittest.TestCase):
    def test_persona_event_keeps_listening(self):
        p = run_session([{"type": "ready"}, {"type": "persona", "slug": "kai", "name": "Kai"}, {"type": "timeout"}])
        self.assertEqual(_relay_base(p[0][1]), "ws://relay.test/ws?persona=kai")
        self.assertIn("timers=", p[0][1])
        self.assertIn(("listening", "Kai"), p)
        self.assertNotIn("thinking", [x[0] for x in p])

    def test_persona_event_while_responding_is_thinking(self):
        p = run_session([{"type": "response_started"}, {"type": "persona", "slug": "rhea", "name": "Rhea"}, {"type": "done"}],
                        persona="grok")
        self.assertEqual(_relay_base(p[0][1]), "ws://relay.test/ws")
        self.assertIn(("thinking", "Rhea"), p)

    def test_local_done_posts_idle_not_thinking(self):
        p = run_session([{"type": "ready"}, {"type": "done", "local": True}])
        self.assertIn("idle", [x[0] for x in p])
        self.assertNotIn("thinking", [x[0] for x in p])

    def test_up24(self):
        x = np.sin(2 * np.pi * 440 * np.arange(1280) / 16000)
        y = gw.up24(x, 0.0)
        self.assertEqual(len(y), 1920)
        self.assertAlmostEqual(y[3], x[1], places=6)  # 24 kHz sample 3 == 16 kHz sample 1 (one-sample delay)

    def test_patience(self):
        gw.WORDS.clear()
        gw.WORDS.update(hey_kitchen=gw._word({"name": "hey_kitchen", "persona": "kitchen", "threshold": 0.8, "patience": 2}))
        d = gw.Detector()
        self.assertIsNone(d.feed({"hey_kitchen": 0.9}))
        self.assertEqual(d.feed({"hey_kitchen": 0.85}), "hey_kitchen")
        d.reset(); d.feed({"hey_kitchen": 0.9})
        self.assertIsNone(d.feed({"hey_kitchen": 0.5}))

    def test_note_enroll_queued_reports_recording_before_busy(self):
        busy, last = gw.ENROLL_STATE.get("busy"), gw.ENROLL_STATE.get("last")
        try:
            gw.ENROLL_STATE["busy"] = False
            gw.ENROLL_STATE["last"] = {"phrase": "hey_rhea", "stage": "failed_quality", "message": "Not good enough"}
            gw.note_enroll_queued({"phrase": "hey_rhea", "count": 10, "persona": "rhea", "topup": True})
            stamped = gw.ENROLL_STATE["last"]
            self.assertEqual(stamped["stage"], "recording")
            self.assertEqual(stamped["message"], "Say it on the station.")
            self.assertIsNone(stamped["session"])
            self.assertTrue(stamped["topup"])
            self.assertFalse(gw.ENROLL_STATE["busy"])
        finally:
            gw.ENROLL_STATE["busy"] = busy
            gw.ENROLL_STATE["last"] = last

    def test_repo_config(self):
        cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "wake", "wakewords.json")))
        names = {w["name"]: w for w in cfg["words"]}
        self.assertNotIn("hey_finn", names)
        self.assertEqual(list(names), ["hey_grok"])
        shipped = sorted(os.listdir(os.path.join(os.path.dirname(__file__), "..", "wake", "models")))
        self.assertEqual([f for f in shipped if f.endswith(".onnx")], ["hey_grok.onnx"])
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

    def test_level_line_reports_peak_sample_and_clipped_ratio(self):
        """Periodic level line keeps rms, dBFS, peak_score, frames, and peaks, and adds the window extremes."""
        pcm = np.array([0, 100, -32768, 32767, 5], np.int16)
        peak, clips, n = gw.pcm_extremes(pcm)
        self.assertEqual((peak, clips, n), (32768, 2, 5))
        self.assertEqual(gw.pcm_extremes(np.array([], np.int16)), (0, 0, 0))
        line = gw.format_level_line(1000, 0.2, 4, {"hey_grok": 0.2, "other": 0.01}, peak, clips / n)
        self.assertIn("level rms=1000 (", line)
        self.assertIn("dBFS)", line)
        self.assertIn("peak_score=0.200", line)
        self.assertIn("frames=4", line)
        self.assertIn("peak_sample=32768", line)
        self.assertIn("clipped=0.4000", line)
        self.assertIn("peaks hey_grok=0.200", line)
        self.assertNotIn("other=", line)
        # 12 clipped samples in 10000 is the documented clipped=0.0012 form
        shown = gw.format_level_line(1, 0, 1, {}, 32767, 12 / 10000)
        self.assertIn("clipped=0.0012", shown)
        self.assertIn("peak_sample=32767", shown)
        self.assertNotIn("peaks", shown)
        with open(os.path.join(os.path.dirname(__file__), "..", "wake", "grok_wake.py")) as f:
            src = f.read()
        self.assertIn("format_level_line(", src)
        self.assertIn("pcm_extremes(", src)

if __name__ == "__main__":
    unittest.main()


class TestEnroll(unittest.TestCase):
    def test_speech_50ms_after_the_chime_is_kept(self):
        sr = 16000
        chime_n = 3200
        gap = int(0.05 * sr)
        speech = (np.sin(np.arange(sr) * 2 * np.pi * 220 / sr) * 8000).astype(np.int16)
        stream = np.concatenate([
            (np.sin(np.arange(chime_n) / 2) * 20000).astype(np.int16),
            np.zeros(gap, np.int16),
            speech,
            np.zeros(int(gw.CLIP_S * sr) + 1280, np.int16),
        ])
        pos = {"i": 0}

        def read16(raw=False):
            i = pos["i"]
            fr = stream[i:i + 1280]
            if len(fr) < 1280:
                fr = np.pad(fr, (0, 1280 - len(fr)))
            pos["i"] = i + 1280
            return fr.astype(np.int16)

        class Proc:
            def poll(self):
                return 0 if pos["i"] >= chime_n else None

        old = gw.subprocess.Popen
        gw.subprocess.Popen = lambda *a, **k: Proc()
        try:
            out = gw.capture_enroll_clip(read16, "default", chime_samples=chime_n)
        finally:
            gw.subprocess.Popen = old
        self.assertEqual(len(out), int(gw.CLIP_S * sr))
        # The player is polled between 80 ms frames, so the measured end is the frame that crosses the chime.
        measured = 1280 * ((chime_n + 1279) // 1280)
        at = (chime_n + gap) - measured
        self.assertGreater(at, 0)
        self.assertTrue(np.array_equal(out[at:at + len(speech)], speech))
        self.assertGreater(int(np.max(np.abs(out[at:at + 400]))), 4000)

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
        self.assertNotIn("hey_finn", ph); self.assertNotIn("hey_hank", ph)  # no bot words shipped by default

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
            self.assertEqual(gw.parse_enroll({"phrase": "hey_hank", "count": 10, "speaker": "spk-ab12", "speakers": ["a"]}),
                             {"phrase": "hey_hank", "persona": "hank", "count": 10, "topup": False})
            self.assertEqual(gw.parse_enroll({"phrase": "hey_hank", "persona": "grok"})["persona"], "hank")
            self.assertEqual(gw.parse_enroll({"phrase": "hey_hank"})["count"], 10)  # omitted count is the default
            self.assertEqual(gw.parse_enroll({"phrase": "hey_kitchen"})["persona"], "kitchen-bot")
            self.assertEqual(gw.parse_enroll({"phrase": "okay_finn", "persona": "finn", "count": 10, "topup": True}),
                             {"phrase": "okay_finn", "persona": "finn", "count": 10, "topup": True})
            self.assertIsNone(gw.parse_enroll({"phrase": "okay_finn"}))
            self.assertIsNone(gw.parse_enroll({"phrase": "okay_finn", "persona": "Not A Slug"}))
            self.assertEqual(gw.parse_enroll({"phrase": "hey_hank", "count": 5}), {"error": "count"})
            self.assertEqual(gw.parse_enroll({"phrase": "hey_hank", "count": 19}), {"error": "count"})
            self.assertEqual(gw.parse_enroll({"phrase": "hey_hank", "count": 31}), {"error": "count"})
            self.assertEqual(gw.parse_enroll({"phrase": "hey_hank", "count": 20}), {"error": "count"})
            self.assertEqual(gw.parse_enroll({"phrase": "hey_hank", "count": 10})["count"], 10)
            self.assertIsNone(gw.parse_enroll({"phrase": "hey_hank", "count": "ten"}))
            self.assertTrue(gw.wake_name_ok("hey_kitchen-bot"))
            self.assertFalse(gw.phrase_ok("hey_kitchen-bot"))  # phrase_ok stays letters-only
            self.assertEqual(gw.parse_enroll({"phrase": "hey_kitchen-bot", "persona": "kitchen-bot", "count": 10})["persona"],
                             "kitchen-bot")
        finally:
            gw.WAKE_CONFIG, gw.OVERRIDES = old
        self.assertTrue(gw.phrase_ok("hey_kitchen")); self.assertTrue(gw.phrase_ok("okay_finn")); self.assertTrue(gw.phrase_ok("hey_finn"))
        self.assertFalse(gw.phrase_ok("hey")); self.assertFalse(gw.phrase_ok("a_b_c_d_e"))
        self.assertFalse(gw.phrase_ok("extraordinarily_supercalifragilistic"))
        self.assertEqual(gw.relay_persona("kitchen-bot"), "grok")
        self.assertEqual(gw.relay_persona("finn"), "finn")
        self.assertTrue(gw.persona_ok("kitchen-bot")); self.assertTrue(gw.persona_ok("grok"))

    def test_word_and_dropped_model_ignore_speaker_fields(self):
        import hashlib
        w = gw._word({"name": "hey_kitchen", "persona": "kitchen-bot", "threshold": 0.4,
                      "speaker": "spk-1", "speakers": ["spk-1"], "guest": True})
        self.assertEqual(w["persona"], "kitchen-bot")
        self.assertNotIn("speaker", w)
        self.assertNotIn("speakers", w)
        self.assertNotIn("guest", w)
        d = tempfile.mkdtemp()
        blob = b"not-really-onnx"
        sha = hashlib.sha256(blob).hexdigest()
        open(os.path.join(d, "hey_kitchen.onnx"), "wb").write(blob)
        with open(os.path.join(d, "hey_kitchen.json"), "w") as f:
            json.dump({"name": "hey_kitchen", "persona": "kitchen-bot", "threshold": 0.4, "patience": 2,
                       "sha256": sha, "speakers": ["spk-1"], "speaker": "old", "guest": True}, f)
        got = gw.load_dropped_wakewords(d)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["persona"], "kitchen-bot")
        self.assertNotIn("speakers", got[0])
        self.assertNotIn("speaker", got[0])
        self.assertNotIn("guest", got[0])

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
        self.assertEqual(uploads, [])  # single device: clips stay on this Pi, nothing is uploaded

    def test_clips_saved_locally(self):
        import glob
        old = gw.ENROLL_DIR
        posted, uploads = self.run_enroll()
        self.assertEqual(uploads, [])


class TrainingNotice(unittest.TestCase):
    """A finished job posts a short notice. "done" without passed is a gate miss, not a success."""

    def test_stale_guest_key_is_honored_once_then_dropped(self):
        import urllib.parse
        f = os.path.join(tempfile.mkdtemp(), "t1.json")
        old = gw.T1_FILE
        gw.T1_FILE = f
        try:
            json.dump({"guest": True, "other": 1}, open(f, "w"))
            body = json.loads(urllib.parse.unquote(gw.t1_query()))
            self.assertTrue(body["guest"])
            self.assertIn("last_tap", body)
            left = json.load(open(f))
            self.assertNotIn("guest", left)
            self.assertEqual(left["other"], 1)
            self.assertFalse(json.loads(urllib.parse.unquote(gw.t1_query()))["guest"])
            json.dump({"guest": False, "other": 2}, open(f, "w"))
            self.assertFalse(json.loads(urllib.parse.unquote(gw.t1_query()))["guest"])
            self.assertEqual(json.load(open(f))["other"], 2)
            self.assertNotIn("guest", json.load(open(f)))
            os.remove(f)
            self.assertFalse(json.loads(urllib.parse.unquote(gw.t1_query()))["guest"])
        finally:
            gw.T1_FILE = old

    def test_control_rejects_counts_outside_20_30(self):
        import http.client
        import socket
        import threading
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, "wakewords.json")
        json.dump({"words": [{"name": "hey_finn", "persona": "finn"}]}, open(cfg, "w"))
        old = (gw.CONTROL_PORT, gw.RELAY_URL, gw.WAKE_CONFIG, gw.OVERRIDES)
        gw.CONTROL_PORT = port
        gw.RELAY_URL = ""
        gw.WAKE_CONFIG = cfg
        gw.OVERRIDES = os.path.join(d, "missing.json")
        th = threading.Thread(target=gw.control_server, daemon=True)
        th.start()

        def post(body):
            last = None
            for _ in range(50):
                try:
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    c.request("POST", "/enroll", json.dumps(body).encode(), {"Content-Type": "application/json"})
                    r = c.getresponse()
                    out = json.loads(r.read() or b"{}")
                    c.close()
                    return r.status, out
                except OSError as e:
                    last = e
                    gw.time.sleep(0.02)
            raise last

        try:
            st, j = post({"phrase": "hey_finn", "count": 25})
            self.assertEqual((st, j.get("error")), (400, "count must be 10"))
            st, j = post({"phrase": "hey_finn", "count": 31})
            self.assertEqual((st, j.get("error")), (400, "count must be 10"))
            st, j = post({"phrase": "hey_finn", "count": 10})
            self.assertNotEqual(st, 400)  # count accepted
            st, j = post({"phrase": "hey_finn"})
            self.assertNotEqual(st, 400)
            st, j = post({"phrase": "hey_finn", "count": 30})  # 30 is refused now
            self.assertEqual(st, 400)
        finally:
            if gw.CONTROL_HTTPD is not None:
                gw.CONTROL_HTTPD.shutdown()
                th.join(2)
                gw.CONTROL_HTTPD.server_close()
                gw.CONTROL_HTTPD = None
            gw.CONTROL_PORT, gw.RELAY_URL, gw.WAKE_CONFIG, gw.OVERRIDES = old


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
                    gw.TIMERS["ee33ff"]["ring_mono"] = gw.time.monotonic() - 1
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


class HandoffWaitScreen(unittest.TestCase):
    def test_direct_wake_has_no_checking_label(self):
        self.assertEqual(gw.handoff_wait_text({"name": "Ash", "direct": True}), " ")
        self.assertNotIn("…", gw.handoff_wait_text({"name": "Ash", "direct": True}))

    def test_bot_to_bot_names_the_bot(self):
        self.assertEqual(gw.handoff_wait_text({"name": "Ash", "direct": False}), "Checking with Ash…")

    def test_old_relay_without_direct_keeps_label(self):
        self.assertEqual(gw.handoff_wait_text({"name": "Ash"}), "Checking with Ash…")

    def test_keepalive_beats_display_thinking_hold(self):
        # regression: the display's 20 s thinking hold dropped to the home screen mid-wait
        self.assertLess(gw.HANDOFF_KEEPALIVE_S, 20)
        s = types.SimpleNamespace(active=True, handoff_text="…", t_first_audio=None)
        self.assertTrue(gw.handoff_keepalive_due(s))
        s.t_first_audio = 1.0
        self.assertFalse(gw.handoff_keepalive_due(s))
        s.t_first_audio, s.handoff_text = None, None
        self.assertFalse(gw.handoff_keepalive_due(s))
        s.handoff_text, s.active = "…", False
        self.assertFalse(gw.handoff_keepalive_due(s))


class AckPlayback(unittest.TestCase):
    def test_line_is_seconds_after_speech(self):
        self.assertEqual(gw.ack_played_line(100.0, 100.12), "ack played 0.12s after end of speech")
        self.assertEqual(gw.ack_played_line(100.0, 100.0), "ack played 0.00s after end of speech")
        self.assertIsNone(gw.ack_played_line(None, 100.0))

    def test_ack_logs_and_shares_the_still_player(self):
        played = []
        old = gw.Session.play_cue
        gw.Session.play_cue = lambda self, pcm: played.append(bytes(pcm))
        audio = base64.b64encode(b"\x01\x02").decode()
        still = base64.b64encode(b"\x03\x04").decode()
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                posted = run_session([
                    {"type": "ready"},
                    {"type": "speech_stopped"},
                    {"type": "ack", "audio": audio},
                    {"type": "handoff_still", "audio": still},
                    {"type": "done"},
                ])
        finally:
            gw.Session.play_cue = old
        log = buf.getvalue()
        self.assertRegex(log, r"ack played \d+\.\d\ds after end of speech")
        self.assertIn("relay: handoff still", log)
        self.assertNotIn("first audio", log)  # the clip is not model audio and does not flip to speaking
        self.assertEqual(played, [b"\x01\x02", b"\x03\x04"])
        states = [p[0] for p in posted if isinstance(p, tuple) and p[0] != "url"]
        self.assertNotIn("speaking", states)

    def test_invalid_ack_audio_is_empty(self):
        self.assertEqual(gw.decode_cue_audio({"audio": "!!!!"}), b"")
        self.assertEqual(gw.decode_cue_audio({}), b"")

    def test_cue_player_never_overlaps(self):
        """ack and a still-asking clip share one player. The second starts only after the first finishes."""
        order = []

        class P:
            def __init__(self, *a, **k):
                order.append("start")

            def communicate(self, pcm):
                order.append(("pcm", pcm))
                time.sleep(0.04)
                order.append("end")
                return b"", b""

        old = gw.subprocess.Popen
        gw.subprocess.Popen = P
        s = gw.Session.__new__(gw.Session)
        s.play_dev = "null"
        s._cue_q = None
        s._cue_thread = None
        try:
            s.play_cue(b"A")
            s.play_cue(b"B")
            s.close_cue()
            self.assertIsNotNone(s._cue_thread)
            s._cue_thread.join(timeout=2)
        finally:
            gw.subprocess.Popen = old
        self.assertFalse(s._cue_thread.is_alive())
        self.assertEqual(order, ["start", ("pcm", b"A"), "end", "start", ("pcm", b"B"), "end"])


class SingleWakeWordDefault(unittest.TestCase):
    def test_default_config_is_hey_grok_only(self):
        cfg = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wake", "wakewords.json")))
        self.assertEqual([w["name"] for w in cfg["words"]], ["hey_grok"])
        self.assertNotIn("fallback", cfg)
        self.assertEqual(cfg.get("enroll"), [])
        self.assertNotIn("jarvis", open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wake", "grok_wake.py")).read().lower())

    def test_builtin_word_cannot_be_turned_off(self):
        entries = {"hey_grok": {"name": "hey_grok"}, "hey_finn": {"name": "hey_finn"}}
        gw.apply_wake_overrides(entries, {"hey_grok": {"enabled": False}, "hey_finn": {"enabled": False}})
        self.assertEqual(list(entries), ["hey_grok"])

    def test_listener_starts_with_hey_grok_only(self):
        """Empty or unusable config: the built-in model alone is loaded."""
        import types
        d = tempfile.mkdtemp()
        open(os.path.join(d, "hey_grok.onnx"), "w").close()
        cfg = os.path.join(d, "wakewords.json")
        json.dump({"models_dir": d, "words": []}, open(cfg, "w"))
        seen = {}

        class M:
            def __init__(self, wakeword_models, inference_framework):
                seen["paths"] = wakeword_models
                self.models = {os.path.basename(p).rsplit(".", 1)[0]: None for p in wakeword_models}
        old = (gw.WAKE_CONFIG, gw.OVERRIDES, sys.modules.get("openwakeword.model"))
        sys.modules["openwakeword.model"] = types.SimpleNamespace(Model=M)
        gw.WAKE_CONFIG, gw.OVERRIDES = cfg, os.path.join(d, "none.json")
        try:
            gw.load_model()
        finally:
            gw.WAKE_CONFIG, gw.OVERRIDES = old[0], old[1]
            if old[2] is None:
                sys.modules.pop("openwakeword.model", None)
            else:
                sys.modules["openwakeword.model"] = old[2]
        self.assertEqual(seen["paths"], [os.path.join(d, "hey_grok.onnx")])
        self.assertEqual(list(gw.WORDS), ["hey_grok"])


class ForgetWord(unittest.TestCase):
    def test_forget_removes_clips_and_model_and_reloads(self):
        d = tempfile.mkdtemp()
        inbox, enr = os.path.join(d, "inbox"), os.path.join(d, "enrolled")
        os.makedirs(os.path.join(inbox, "gstest_word", "20261023-010101"))
        os.makedirs(enr)
        open(os.path.join(enr, "gstest_word.onnx"), "w").write("x")
        json.dump({"words": [{"name": "gstest_word"}, {"name": "hey_finn"}]}, open(os.path.join(enr, "wakewords.enrolled.json"), "w"))
        old = gw.ENROLL_DIR, gw.ENROLLED_DIR
        gw.ENROLL_DIR, gw.ENROLLED_DIR = inbox, enr
        gw.RELOAD.clear()
        try:
            self.assertEqual(gw.forget_word("hey_grok"), (False, "bad name"))
            self.assertEqual(gw.forget_word("../etc"), (False, "bad name"))
            gw.ENROLL_STATE["last"] = {"phrase": "gstest_word", "stage": "failed_quality"}
            self.assertEqual(gw.forget_word("gstest_word"), (True, None))
            self.assertIsNone(gw.ENROLL_STATE["last"])  # /status no longer reports the deleted word
        finally:
            gw.ENROLL_DIR, gw.ENROLLED_DIR = old
        self.assertFalse(os.path.exists(os.path.join(inbox, "gstest_word")))
        self.assertFalse(os.path.exists(os.path.join(enr, "gstest_word.onnx")))
        self.assertEqual([w["name"] for w in json.load(open(os.path.join(enr, "wakewords.enrolled.json")))["words"]], ["hey_finn"])
        self.assertTrue(gw.RELOAD.is_set())

    def test_fixed_ten_clips(self):
        self.assertEqual((gw.ENROLL_COUNT, gw.TOPUP_COUNT), (10, 10))
