"""Home command tones: generated wavs, catalog ids, and the wake player."""
import array, importlib.util, inspect, io, os, sys, tempfile, unittest, wave
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED = os.path.join(ROOT, "assets", "sounds")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import sounds


def gen_home():
    path = os.path.join(SHIPPED, "gen_home.py")
    spec = importlib.util.spec_from_file_location("gen_home", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class HomeTones(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("GROK_STATION_SOUNDS", "GROK_STATION_SOUNDS_PY")}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_wavs_match_the_generator(self):
        mod = gen_home()
        src = open(os.path.join(SHIPPED, "gen_home.py"), encoding="utf-8").read()
        self.assertNotIn("numpy", src)
        self.assertNotIn("gen.py", src.split("Does not replace gen.py", 1)[-1])
        with tempfile.TemporaryDirectory() as d:
            mod.render(d)
            for name, dur in (("home-ok", 0.25), ("home-fail", 0.40)):
                shipped = os.path.join(SHIPPED, name + ".wav")
                fresh = os.path.join(d, name + ".wav")
                self.assertEqual(open(shipped, "rb").read(), open(fresh, "rb").read(), name)
                self.assertLess(os.path.getsize(shipped), 40 * 1024, name)
                with wave.open(shipped) as w:
                    self.assertEqual(w.getnchannels(), 1, name)
                    self.assertEqual(w.getsampwidth(), 2, name)
                    self.assertEqual(w.getframerate(), mod.SR, name)
                    self.assertAlmostEqual(w.getnframes() / w.getframerate(), dur, places=2, msg=name)
                    frames = w.readframes(w.getnframes())
                samples = array.array("h")
                samples.frombytes(frames)
                peak = max(abs(s) for s in samples)
                self.assertGreater(peak, 14000, name)
                self.assertLess(peak, 18000, name)
        ok = array.array("h")
        fail = array.array("h")
        with wave.open(os.path.join(SHIPPED, "home-ok.wav")) as w:
            ok.frombytes(w.readframes(w.getnframes()))
        with wave.open(os.path.join(SHIPPED, "home-fail.wav")) as w:
            fail.frombytes(w.readframes(w.getnframes()))
        self.assertNotEqual(ok.tobytes(), fail.tobytes())
        # Rising two-note: the second note crosses zero faster than the first.
        self.assertGreater(_rate(ok[1920:]), _rate(ok[:1600]) * 1.2)
        self.assertLess(_rate(fail), _rate(ok[:1600]))

    def test_catalog_plays_them_and_they_are_not_choices(self):
        os.environ["GROK_STATION_SOUNDS"] = SHIPPED
        self.assertTrue(sounds.file_for("home-ok").endswith(os.sep + "home-ok.wav"))
        self.assertTrue(sounds.file_for("home-fail").endswith(os.sep + "home-fail.wav"))
        c = sounds.choices()
        self.assertNotIn("home-ok", c["alarm"] + c["listen"] + c["end"])
        self.assertNotIn("home-fail", c["alarm"] + c["listen"] + c["end"])
        self.assertFalse(sounds.allowed("ALARM_SOUND", "home-ok"))
        self.assertFalse(sounds.allowed("LISTEN_TONE", "home-ok"))
        self.assertFalse(sounds.allowed("END_TONE", "home-fail"))
        self.assertEqual(sounds.present("home_ok"), ["home-ok"])
        self.assertEqual(sounds.present("home_fail"), ["home-fail"])

    def test_wake_plays_the_tone_without_blocking_or_logging_the_result(self):
        os.environ["GROK_STATION_SOUNDS"] = SHIPPED
        os.environ["GROK_STATION_SOUNDS_PY"] = os.path.join(ROOT, "sounds.py")
        sys.path.insert(0, os.path.join(ROOT, "wake"))
        import grok_wake as gw
        calls = []
        orig = gw.play_file
        gw.play_file = lambda dev, path, block=False: calls.append((dev, path, block))
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                gw.on_home_tone("plughw:CARD=X,DEV=0", True)
                gw.on_home_tone("plughw:CARD=X,DEV=0", False)
        finally:
            gw.play_file = orig
        ok = gw.station_sounds().file_for("home-ok")
        fail = gw.station_sounds().file_for("home-fail")
        self.assertEqual(calls, [
            ("plughw:CARD=X,DEV=0", ok, False),
            ("plughw:CARD=X,DEV=0", fail, False),
        ])
        text = buf.getvalue().strip().splitlines()
        self.assertEqual(text, ["relay: home tone", "relay: home tone"])
        blob = buf.getvalue()
        self.assertNotIn("home-ok", blob)
        self.assertNotIn("True", blob)
        self.assertNotIn("False", blob)
        src = open(os.path.join(ROOT, "wake", "grok_wake.py"), encoding="utf-8").read()
        self.assertIn('elif t == "home_tone":', src)
        self.assertIn('on_home_tone(self.play_dev, ev.get("ok") is True)', src)
        fn = inspect.getsource(gw.on_home_tone)
        self.assertIn("block=False", fn)
        self.assertNotIn(".state(", fn)


def _rate(samples):
    n = 0
    for a, b in zip(samples, samples[1:]):
        if a <= 0 < b:
            n += 1
    return n / max(1, len(samples))
