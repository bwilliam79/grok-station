"""Few-shot wake words: template match, calibration gate, storage, and the listener wiring. No audio models."""
import json, os, sys, tempfile, types, unittest
import numpy as np
ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "wake"))
import fewshot as fs
import grok_wake as gw

rng = np.random.default_rng(3)
WORD = rng.standard_normal((12, 96)).astype(np.float32)
OTHER = rng.standard_normal((12, 96)).astype(np.float32)


def say(base, noise=0.25, lead=14, tail=5):
    """Embedding stream: background, a time-warped noisy copy of base, background. Rows in template order."""
    idx = np.sort(rng.choice(np.repeat(np.arange(len(base)), 2), size=len(base) + rng.integers(-2, 3), replace=False))
    w = base[idx] + noise * rng.standard_normal((len(idx), 96))
    bg = lambda n: rng.standard_normal((n, 96)) * 0.3 + 1.0
    return np.vstack([bg(lead), w, bg(tail)]).astype(np.float32), (len(idx) * fs.FRAME_S)


def frames_for(stream_len_s):
    return stream_len_s


class Matcher(unittest.TestCase):
    def test_own_word_scores_high_other_low(self):
        m = fs.Matcher([WORD[:10], WORD[1:12]])
        hit = fs.peak(m, say(WORD)[0])
        miss = fs.peak(m, say(OTHER)[0])
        self.assertGreater(hit, 0.6)
        self.assertLess(miss, hit - 0.2)

    def test_reset_clears_state(self):
        m = fs.Matcher([WORD])
        for e in fs._unit(WORD):
            m.feed(e)
        m.reset()
        self.assertTrue(np.all(np.isinf(m.D)))

    def test_cost_is_small_per_frame(self):
        m = fs.Matcher([WORD[:fs.MAX_ROWS]] * fs.MAX_TEMPLATES)
        self.assertEqual(len(m.R), 12 * fs.MAX_TEMPLATES)  # one matmul of at most 20 x 18 rows per frame


class Calibrate(unittest.TestCase):
    def _reps(self, base, n=10, noise=0.25):
        out = [say(base, noise) for _ in range(n)]
        # clip_frames picks rows by time; put the word where a padded clip would have it
        frames, lens = [], []
        for s, dur in out:
            frames.append(s); lens.append(dur)
        return frames, lens

    def test_clean_reps_pass_and_set_threshold_above_negatives(self):
        fs_orig = fs.clip_frames
        fs.clip_frames = lambda f, s, lead_s=fs.LEAD_S: f[14:-5]
        try:
            frames, lens = self._reps(WORD)
            negs = [say(OTHER)[0] for _ in range(5)] + [rng.standard_normal((40, 96)).astype(np.float32)]
            cal = fs.calibrate(frames, lens, negs)
        finally:
            fs.clip_frames = fs_orig
        self.assertTrue(cal["ok"], cal["message"])
        self.assertGreaterEqual(cal["heard"], fs.REPS_NEEDED)
        self.assertGreater(cal["threshold"], cal["neg_max"])
        self.assertTrue(cal["message"].startswith("Ready"))

    def test_inconsistent_reps_ask_for_train_more(self):
        fs_orig = fs.clip_frames
        fs.clip_frames = lambda f, s, lead_s=fs.LEAD_S: f[14:-5]
        try:
            frames = [say(rng.standard_normal((12, 96)).astype(np.float32))[0] for _ in range(10)]  # 10 different words
            cal = fs.calibrate(frames, [1.0] * 10, [say(OTHER)[0]])
        finally:
            fs.clip_frames = fs_orig
        self.assertFalse(cal["ok"])
        self.assertTrue(cal["message"].startswith("Not good enough"))
        self.assertIn("Train more", cal["message"])

    def test_templates_capped(self):
        fs_orig = fs.clip_frames
        fs.clip_frames = lambda f, s, lead_s=fs.LEAD_S: f[14:-5]
        try:
            frames, lens = self._reps(WORD, n=30)
            cal = fs.calibrate(frames, lens, [say(OTHER)[0]])
        finally:
            fs.clip_frames = fs_orig
        self.assertLessEqual(len(cal["templates"]), fs.MAX_TEMPLATES)

    def test_collision_with_another_word(self):
        fs_orig = fs.clip_frames
        fs.clip_frames = lambda f, s, lead_s=fs.LEAD_S: f[14:-5]
        try:
            frames, lens = self._reps(WORD)
            twin = fs.Matcher([WORD]); twin.threshold = 0.5
            cal = fs.calibrate(frames, lens, [say(OTHER)[0]], others=[("Hey Twin", twin)])
        finally:
            fs.clip_frames = fs_orig
        self.assertFalse(cal["ok"])
        self.assertIn("Hey Twin", cal["message"])

    def test_clip_frames_window(self):
        emb = np.arange(40 * 96, dtype=np.float32).reshape(40, 96)
        rows = fs.clip_frames(emb, 1.0)
        self.assertTrue(fs.MIN_ROWS <= len(rows) <= fs.MAX_ROWS)

    def test_save_load_roundtrip(self):
        p = os.path.join(tempfile.mkdtemp(), "w.fewshot.npz")
        fs.save(p, [WORD[:5], WORD[:7]], {})
        back = fs.load(p)
        self.assertEqual([len(t) for t in back], [5, 7])
        self.assertTrue(np.allclose(back[1], WORD[:7], atol=1e-2))


class ListenerWiring(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old = (gw.ENROLLED_DIR, gw.ENROLL_DIR, gw.DROP_DIR, gw._embed, gw.hey_grok_hears, gw.FEWSHOT_NEG, fs.clip_frames)
        gw.ENROLLED_DIR = os.path.join(self.d, "enrolled"); gw.ENROLL_DIR = os.path.join(self.d, "inbox")
        gw.DROP_DIR = os.path.join(self.d, "drop"); os.makedirs(gw.DROP_DIR)
        gw.FEWSHOT_NEG = os.path.join(self.d, "none.npz")
        ses = os.path.join(gw.ENROLL_DIR, "hey_finn", "20261023-080000"); os.makedirs(ses)
        with open(os.path.join(ses, "session.json"), "w") as f:
            json.dump({"phrase": "hey_finn", "persona": "finn", "count": 10}, f)
        import wave
        for i in range(10):
            with wave.open(os.path.join(ses, f"{i:02d}.wav"), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(np.zeros(16000, "<i2").tobytes())
        fake = iter([say(WORD)[0] for _ in range(10)])
        gw._embed = lambda pcm: next(fake, say(OTHER)[0])
        gw.hey_grok_hears = lambda clips: 0.01
        fs.clip_frames = lambda f, s, lead_s=fs.LEAD_S: f[14:-5]
        gw.RELOAD.clear()

    def tearDown(self):
        (gw.ENROLLED_DIR, gw.ENROLL_DIR, gw.DROP_DIR, gw._embed, gw.hey_grok_hears, gw.FEWSHOT_NEG, fs.clip_frames) = self.old
        gw.FEWSHOT.clear()

    def _doc(self):
        return json.load(open(os.path.join(gw.ENROLLED_DIR, "wakewords.enrolled.json")))

    def test_build_sync_and_forget(self):
        cal = gw.build_fewshot("hey_finn", "finn")
        self.assertTrue(cal["ok"], cal["message"])
        self.assertTrue(gw.RELOAD.is_set())
        w = self._doc()["words"][0]
        self.assertEqual((w["name"], w["persona"], w["kind"]), ("hey_finn", "finn", "fewshot"))
        self.assertTrue(os.path.isfile(w["path"]))
        gw.sync_wakewords(gw.DROP_DIR)  # an empty drop dir must not erase recorded words
        self.assertEqual([x["name"] for x in self._doc()["words"]], ["hey_finn"])
        ok, err = gw.forget_word("hey_finn")
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(w["path"]))
        self.assertEqual(self._doc()["words"], [])
        self.assertFalse(os.path.exists(os.path.join(gw.ENROLL_DIR, "hey_finn")))

    def test_marker_cleared_and_stale_build_reported(self):
        gw.build_fewshot("hey_finn", "finn", "20261023-080000")
        self.assertFalse(os.path.exists(gw._marker("hey_finn")))
        self.assertTrue("built_at" in self._doc()["words"][0])
        json.dump({"session": "s1", "persona": "finn"}, open(gw._marker("hey_finn"), "w"))  # listener died mid-build
        old = gw.ENROLL_STATE["last"]
        try:
            self.assertEqual(gw.stale_build(), "hey_finn")
            self.assertEqual(gw.ENROLL_STATE["last"]["stage"], "failed")
            self.assertEqual(gw.ENROLL_STATE["last"]["message"], gw.BUILD_STOPPED)
            self.assertFalse(os.path.exists(gw._marker("hey_finn")))
        finally:
            gw.ENROLL_STATE["last"] = old

    def test_build_error_marks_failed_with_plain_message(self):
        posted = []
        old = (gw.build_fewshot, gw.post_state, gw.subprocess.Popen, gw.time.sleep, gw.follow_training)
        gw.build_fewshot = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        gw.post_state = lambda *a, **k: None
        gw.subprocess.Popen = lambda *a, **k: None
        gw.time.sleep = lambda s: None
        gw.follow_training = lambda *a, **k: None
        t = np.arange(30 * 1280) / 16000.0
        speech = (6000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16); speech[:8000] = 0; speech[-8000:] = 0
        frames = iter([])
        def read16(raw=False):
            return np.zeros(1280, np.int16)
        oc = gw.np.concatenate
        gw.np.concatenate = lambda parts, *a, **k: speech if len(parts) == 30 else oc(parts, *a, **k)
        try:
            gw.enroll({"phrase": "hey_zed", "persona": "grok", "count": 10}, read16, "default")
        finally:
            (gw.build_fewshot, gw.post_state, gw.subprocess.Popen, gw.time.sleep, gw.follow_training) = old
            gw.np.concatenate = oc
        last = gw.ENROLL_STATE["last"]
        self.assertEqual(last["stage"], "failed")
        self.assertIn("Press Record voice", last["message"])
        self.assertFalse(gw.ENROLL_STATE["busy"])

    def test_hey_grok_collision_blocks(self):
        gw.hey_grok_hears = lambda clips: 0.9
        cal = gw.build_fewshot("hey_finn", "finn")
        self.assertFalse(cal["ok"])
        self.assertIn("Hey Grok", cal["message"])
        self.assertFalse(os.path.exists(os.path.join(gw.ENROLLED_DIR, "wakewords.enrolled.json")))

    def test_load_model_and_scores(self):
        gw.build_fewshot("hey_finn", "finn")
        cfg = os.path.join(self.d, "wakewords.json")
        json.dump({"models_dir": self.d, "words": [{"name": "hey_grok", "persona": "grok"}]}, open(cfg, "w"))
        open(os.path.join(self.d, "hey_grok.onnx"), "wb").write(b"x")

        class FakeModel:
            def __init__(self, wakeword_models, inference_framework):
                self.models = {"hey_grok": 1}
                self.preprocessor = types.SimpleNamespace(feature_buffer=np.zeros((1, 96), np.float32))
            def predict(self, pcm):
                return {"hey_grok": 0.0}
            def reset(self):
                pass
        mod = types.ModuleType("openwakeword.model"); mod.Model = FakeModel
        saved = {k: sys.modules.get(k) for k in ("openwakeword", "openwakeword.model")}
        sys.modules["openwakeword"] = types.ModuleType("openwakeword"); sys.modules["openwakeword.model"] = mod
        old = (gw.WAKE_CONFIG, gw.OVERRIDES)
        gw.WAKE_CONFIG, gw.OVERRIDES = cfg, os.path.join(self.d, "ov.json")
        os.environ["WAKE_ENROLLED_DIR"] = gw.ENROLLED_DIR
        try:
            json.dump({"hey_finn": {"threshold": 0.1}}, open(gw.OVERRIDES, "w"))
            m = gw.load_model()
            self.assertIn("hey_finn", gw.FEWSHOT)
            self.assertEqual(gw.WORDS["hey_finn"]["patience"], gw.FEWSHOT_PATIENCE)
            self.assertEqual(gw.WORDS["hey_finn"]["threshold"], self._doc()["words"][0]["threshold"])  # sensitivity slider doesn't move a recorded word
            det, fired = gw.Detector(), None
            for e in say(WORD, noise=0.1)[0]:
                m.preprocessor.feature_buffer = e[None]
                fired = fired or det.feed(gw.scores(m, np.zeros(1280, np.int16)))
            self.assertEqual(fired, "hey_finn")
        finally:
            gw.WAKE_CONFIG, gw.OVERRIDES = old
            os.environ.pop("WAKE_ENROLLED_DIR", None)
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


class Shipping(unittest.TestCase):
    def test_installed_and_updated(self):
        inst = open(os.path.join(ROOT, "install.sh")).read()
        files = open(os.path.join(ROOT, "tools", "install-files.sh")).read()
        self.assertIn("wake/fewshot.py", inst)
        self.assertIn("wake/models/fewshot_negatives.npz", inst)
        self.assertIn('"$S/wake/fewshot.py"', files)
        self.assertIn('"$S/wake/models/fewshot_negatives.npz"', files)

    def test_bank_loads(self):
        bank = fs.load(os.path.join(ROOT, "wake", "models", "fewshot_negatives.npz"))
        self.assertGreater(len(bank), 100)
        self.assertEqual(bank[0].shape[1], 96)

    def test_no_separate_computer_message(self):
        self.assertNotIn("computer", gw.TRAINER_LATER.lower())
