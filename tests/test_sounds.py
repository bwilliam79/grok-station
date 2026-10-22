"""Sound catalog: manifest allowlist, stored ids (never paths), and the fixed relay-fail tone."""
import io, json, os, sys, tempfile, unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED = os.path.join(ROOT, "assets", "sounds")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import sounds


def helper():
    g = {"__name__": "helper", "__file__": os.path.join(ROOT, "admin", "grok-station-helper")}
    exec(compile(open(os.path.join(ROOT, "admin", "grok-station-helper")).read(), "helper", "exec"), g)
    return g


def call(g, fn, *args, stdin=""):
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    buf = io.StringIO()
    code = 0
    try:
        with redirect_stdout(buf):
            try:
                g[fn](*args)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
    finally:
        sys.stdin = old
    line = buf.getvalue().strip().splitlines()
    return code, json.loads(line[-1] if line else "{}")


class Catalog(unittest.TestCase):
    def setUp(self):
        keys = ("GROK_STATION_SOUNDS", "GROK_STATION_SOUNDS_PY", "WAKE_CHIME", "ALARM_SOUND", "LISTEN_TONE", "END_TONE")
        self._env = {k: os.environ.get(k) for k in keys}
        os.environ["GROK_STATION_SOUNDS"] = SHIPPED
        os.environ["GROK_STATION_SOUNDS_PY"] = os.path.join(ROOT, "sounds.py")
        os.environ["WAKE_CHIME"] = os.path.join(ROOT, "wake", "missing-chime.wav")
        for k in ("ALARM_SOUND", "LISTEN_TONE", "END_TONE"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_every_listed_file_exists(self):
        raw = json.load(open(os.path.join(SHIPPED, "manifest.json")))
        self.assertEqual(list(raw), ["alarm", "listen", "end", "fail"])
        self.assertEqual(raw["fail"], ["comms-fail-2"])
        self.assertEqual(raw["listen"], ["notify-1", "notify-2"])
        self.assertEqual(raw["end"], ["end-1", "end-2", "end-3", "notify-1", "notify-2"])
        for cat, ids in raw.items():
            self.assertEqual(sounds.manifest()[cat], ids)
            for i in ids:
                self.assertTrue(os.path.isfile(os.path.join(SHIPPED, i + ".wav")), i)
                got = sounds.file_for(i)
                self.assertIsNotNone(got, i)
                self.assertTrue(got.endswith(os.sep + i + ".wav"), got)

    def test_missing_entry_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["GROK_STATION_SOUNDS"] = d
            with open(os.path.join(d, "manifest.json"), "w") as f:
                json.dump({"alarm": ["alarm-1", "alarm-9"], "listen": ["notify-1"],
                           "end": ["end-1"], "fail": ["comms-fail-2"]}, f)
            with open(os.path.join(d, "alarm-1.wav"), "wb") as f:
                f.write(b"RIFF")
            self.assertEqual(sounds.present("alarm"), ["alarm-1"])
            self.assertEqual(sounds.present("listen"), [])
            self.assertEqual(sounds.present("fail"), [])
            self.assertNotIn("alarm-9", sounds.choices()["alarm"])
            self.assertIsNone(sounds.file_for("alarm-9"))
            self.assertIsNone(sounds.fail_file())
            self.assertTrue(sounds.allowed("ALARM_SOUND", "alarm-1"))
            self.assertFalse(sounds.allowed("ALARM_SOUND", "alarm-9"))

    def test_rejects_paths_and_unlisted_ids(self):
        for bad in ("../alarm-1", "alarm-1.wav", "/tmp/alarm-1", "alarm-99", "comms-fail-1",
                    "alarm-1/../../etc/passwd", "..", "Alarm-1", "none/alarm-1", "chime.wav"):
            self.assertIsNone(sounds.file_for(bad), bad)
            self.assertFalse(sounds.allowed("ALARM_SOUND", bad), bad)
        self.assertIsNone(sounds.file_for("none"))
        self.assertIsNone(sounds.file_for(""))
        self.assertFalse(sounds.allowed("ALARM_SOUND", None))
        fail = sounds.file_for("comms-fail-2")
        self.assertTrue(fail.endswith(os.sep + "comms-fail-2.wav"))
        self.assertTrue(os.path.commonpath([os.path.realpath(SHIPPED), fail]) == os.path.realpath(SHIPPED))

    def test_symlink_cannot_leave_the_catalog(self):
        with tempfile.TemporaryDirectory() as d:
            outside = tempfile.NamedTemporaryFile(delete=False)
            outside.close()
            try:
                os.environ["GROK_STATION_SOUNDS"] = d
                with open(os.path.join(d, "manifest.json"), "w") as f:
                    json.dump({"alarm": ["alarm-1"], "listen": [], "end": [], "fail": []}, f)
                os.symlink(outside.name, os.path.join(d, "alarm-1.wav"))
                self.assertIsNone(sounds.file_for("alarm-1"))
                self.assertEqual(sounds.present("alarm"), [])
                self.assertFalse(sounds.allowed("ALARM_SOUND", "alarm-1"))
            finally:
                os.unlink(outside.name)

    def test_defaults_and_chime(self):
        c = sounds.choices()
        self.assertEqual(c["defaults"], {"alarm": "alarm-1", "listen": "none", "end": "none"})
        self.assertEqual(c["listen"][0], "none")
        self.assertNotIn("chime", c["listen"])
        self.assertIn("notify-1", c["listen"])
        self.assertIn("notify-2", c["listen"])
        self.assertEqual(c["end"][0], "none")
        for i in ("end-1", "end-2", "end-3", "notify-1", "notify-2"):
            self.assertIn(i, c["end"])
        self.assertNotIn("alarm-1", c["end"])
        self.assertNotIn("comms-fail-2", c["alarm"])
        self.assertTrue(sounds.allowed("END_TONE", "none"))
        self.assertTrue(sounds.allowed("END_TONE", "notify-1"))
        self.assertFalse(sounds.allowed("END_TONE", "alarm-1"))
        self.assertFalse(sounds.allowed("LISTEN_TONE", "end-1"))
        self.assertFalse(sounds.allowed("LISTEN_TONE", "chime"))
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            os.environ["WAKE_CHIME"] = f.name
            c = sounds.choices()
            self.assertEqual(c["defaults"]["listen"], "chime")
            self.assertEqual(c["listen"][:2], ["none", "chime"])
            self.assertEqual(sounds.file_for("chime"), os.path.realpath(f.name))
            self.assertTrue(sounds.allowed("LISTEN_TONE", "chime"))
        os.environ["WAKE_CHIME"] = os.path.join(ROOT, "wake", "missing-chime.wav")
        self.assertIsNone(sounds.file_for("chime"))

    def test_helper_allowlist_and_does_not_store_a_path(self):
        g = helper()
        with tempfile.TemporaryDirectory() as d:
            wake = os.path.join(d, "wake.env")
            open(wake, "w").write("WAKE_GAIN=1\n")
            g["ENV"]["wake"] = wake
            before = open(wake).read()
            code, body = call(g, "settings_set", "audio", stdin=json.dumps({"WAKE_GAIN": "2", "ALARM_SOUND": "../alarm-1"}))
            self.assertNotEqual(code, 0)
            self.assertIn("error", body)
            self.assertEqual(open(wake).read(), before)
            for bad in ("alarm-99", "/etc/passwd", "alarm-1.wav", "comms-fail-1"):
                code, _ = call(g, "settings_set", "audio", stdin=json.dumps({"ALARM_SOUND": bad}))
                self.assertNotEqual(code, 0, bad)
                self.assertEqual(open(wake).read(), before, bad)
            code, _ = call(g, "settings_set", "audio", stdin=json.dumps({"LISTEN_TONE": "chime"}))
            self.assertNotEqual(code, 0)
            self.assertEqual(open(wake).read(), before)
            code, body = call(g, "settings_set", "audio", stdin=json.dumps(
                {"ALARM_SOUND": "alarm-1", "LISTEN_TONE": "notify-2", "END_TONE": "end-2"}))
            self.assertEqual(code, 0, body)
            text = open(wake).read()
            self.assertIn("ALARM_SOUND=alarm-1\n", text)
            self.assertIn("LISTEN_TONE=notify-2\n", text)
            self.assertIn("END_TONE=end-2\n", text)
            self.assertNotIn("..", text)
            open(wake, "w").write('ALARM_SOUND="../alarm-1"\nLISTEN_TONE="/tmp/x.wav"\n')
            code, body = call(g, "settings_get", "audio")
            self.assertEqual(code, 0, body)
            self.assertEqual(body["values"]["ALARM_SOUND"], "alarm-1")
            self.assertEqual(body["values"]["LISTEN_TONE"], "none")
            self.assertEqual(body["values"]["END_TONE"], "none")
            dumped = json.dumps(body)
            self.assertNotIn("..", dumped)
            self.assertNotIn("/tmp", dumped)
            self.assertIn("alarm-1", body["sounds"]["alarm"])
            self.assertNotIn("alarm-9", body["sounds"]["alarm"])
            with tempfile.NamedTemporaryFile(suffix=".wav") as f:
                os.environ["WAKE_CHIME"] = f.name
                code, body = call(g, "settings_set", "audio", stdin=json.dumps({"LISTEN_TONE": "chime"}))
                self.assertEqual(code, 0, body)
                self.assertIn("LISTEN_TONE=chime\n", open(wake).read())

    def test_playback_mapping_and_fail_tone(self):
        sys.path.insert(0, os.path.join(ROOT, "wake"))
        import grok_wake as gw
        self.assertTrue(gw.preview_wav("alarm-1").endswith(os.sep + "alarm-1.wav"))
        self.assertTrue(gw.preview_wav("notify-1").endswith(os.sep + "notify-1.wav"))
        self.assertIsNone(gw.preview_wav("../alarm-1"))
        self.assertIsNone(gw.preview_wav("alarm-1.wav"))
        self.assertIsNone(gw.preview_wav("/etc/passwd"))
        self.assertIsNone(gw.preview_wav("comms-fail-1"))
        self.assertIsNone(gw.preview_wav("none"))
        os.environ["ALARM_SOUND"] = "alarm-3"
        self.assertTrue(gw.configured_wav("ALARM_SOUND").endswith(os.sep + "alarm-3.wav"))
        os.environ["ALARM_SOUND"] = "../alarm-1"
        self.assertTrue(gw.configured_wav("ALARM_SOUND").endswith(os.sep + "alarm-1.wav"))
        os.environ["LISTEN_TONE"] = "notify-2"
        self.assertTrue(gw.configured_wav("LISTEN_TONE").endswith(os.sep + "notify-2.wav"))
        os.environ["END_TONE"] = "none"
        self.assertIsNone(gw.configured_wav("END_TONE"))
        os.environ["END_TONE"] = "end-3"
        self.assertTrue(gw.configured_wav("END_TONE").endswith(os.sep + "end-3.wav"))
        os.environ["END_TONE"] = "alarm-1"
        self.assertIsNone(gw.configured_wav("END_TONE"))
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            os.environ["WAKE_CHIME"] = f.name
            os.environ["LISTEN_TONE"] = "chime"
            self.assertEqual(gw.configured_wav("LISTEN_TONE"), os.path.realpath(f.name))
        calls = []
        orig = gw.subprocess.run
        gw.subprocess.run = lambda cmd, **k: calls.append(list(cmd))
        try:
            gw.on_comms_fail("plughw:CARD=X,DEV=0")
        finally:
            gw.subprocess.run = orig
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ["aplay", "-q", "-D", "plughw:CARD=X,DEV=0"])
        self.assertTrue(calls[0][-1].endswith(os.sep + "comms-fail-2.wav"))
        self.assertNotIn("..", calls[0][-1])
        src = open(os.path.join(ROOT, "wake", "grok_wake.py")).read()
        self.assertIn('elif t == "comms_fail":', src)
        self.assertIn("on_comms_fail(self.play_dev)", src)
        self.assertIn('configured_wav("LISTEN_TONE")', src)
        self.assertIn('configured_wav("END_TONE")', src)
        self.assertIn('configured_wav("ALARM_SOUND")', src)
        self.assertIn('["aplay", "-q", "-D", play, CHIME]', src)  # enrollment beep stays today's chime
        self.assertNotIn("I couldn't reach", src)
        sh = open(os.path.join(ROOT, "tools", "install-files.sh")).read()
        self.assertIn("/opt/grok-station/assets/sounds", sh)
        self.assertIn('"$S"/assets/sounds/*', sh)
        self.assertIn('"$S/sounds.py"', sh)
        ui = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        self.assertIn("ALARM_SOUND:", ui)
        self.assertIn("LISTEN_TONE:", ui)
        self.assertIn("END_TONE:", ui)
        self.assertIn('api("POST", "/api/audio/test", {id})', ui)
        self.assertIn('api("POST", "/api/audio/test", {})', ui)
        self.assertIn("Nothing to play.", ui)
