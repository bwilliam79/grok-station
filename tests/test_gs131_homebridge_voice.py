"""Homebridge voice. Admin setup, then a transcript through relay.session against the fake.

Voice home control off: the fake gets no PUT. On: turn off writes On=0.
Read-back ("is the porch light on") is not matched; it would need a second inventory read.
"""
import json, os, shutil, sys, tempfile, time, unittest

import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "admin"))
import test_gs131_homebridge_api as api  # noqa: E402
import test_gs130_home_flow as thf  # noqa: E402
from fakes.fake_homebridge import FakeHomebridge  # noqa: E402

import grok_admin as ga  # noqa: E402
import gs_integrations as gsi  # noqa: E402

SECRET = api.SECRET
GOOD = api.GOOD
MARKER = api.MARKER


class HomebridgeVoice(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.hb = FakeHomebridge()
        cls.hb.start_thread()
        cls.admin = api._admin_server()

    @classmethod
    def tearDownClass(cls):
        cls.admin.shutdown()
        cls.hb.close_thread()

    async def asyncSetUp(self):
        self.relay, self.home = thf.load_mods()
        self.tmp = tempfile.mkdtemp(prefix="gs131-hb-voice-")
        self.creds = os.path.join(self.tmp, "secrets", "homebridge.json")
        self.block = os.path.join(self.tmp, "status", "homebridge.json")
        self.reg = os.path.join(self.tmp, "integrations.json")
        self._env = {k: os.environ.get(k) for k in (
            "ADMIN_ALLOW_LOOPBACK_FOR_TESTS", "ADMIN_INTEGRATIONS", "HOMEBRIDGE_BLOCK", "GS_HOMEBRIDGE_CREDS",
            "HOME_REGISTRY", "HOME_ALLOW_LOOPBACK_FOR_TESTS")}
        os.environ["ADMIN_ALLOW_LOOPBACK_FOR_TESTS"] = "1"
        os.environ["ADMIN_INTEGRATIONS"] = self.reg
        os.environ["HOMEBRIDGE_BLOCK"] = self.block
        os.environ["GS_HOMEBRIDGE_CREDS"] = self.creds
        os.environ["HOME_REGISTRY"] = self.reg
        os.environ["HOME_ALLOW_LOOPBACK_FOR_TESTS"] = "1"
        self._resolve = gsi.resolve_host
        self._own = gsi.station_addresses
        self._home_own = self.home.station_addresses
        self._timeout = self.home._TIMEOUT
        self._helper = ga.helper
        gsi.station_addresses = lambda: ["10.0.0.8"]
        gsi.resolve_host = lambda host: ["127.0.0.1"]
        self.home.station_addresses = lambda: ["10.0.0.8"]
        ga.helper = self._helper_stub
        self.hb.reset()
        self.home._cache.clear()
        self.home._offline.clear()
        self.home._hb_mem_clear()
        self.lines = []
        self.tts = []
        self.audits = []
        self._log = self.relay.log
        self._tts = self.relay.tts_pcm
        self._audit = self.relay._write_audit_line
        self.relay.log = lambda *a: self.lines.append(" ".join(map(str, a)))
        self.relay._write_audit_line = self.audits.append

        async def fake_tts(*a, **k):
            self.tts.append(a)
            return b"TTS"

        self.relay.tts_pcm = fake_tts
        self.c = api._signed_in(self.admin.port)

    async def asyncTearDown(self):
        self.relay.log = self._log
        self.relay.tts_pcm = self._tts
        self.relay._write_audit_line = self._audit
        ga.helper = self._helper
        gsi.resolve_host = self._resolve
        gsi.station_addresses = self._own
        self.home.station_addresses = self._home_own
        self.home._TIMEOUT = self._timeout
        self.home._cache.clear()
        self.home._hb_mem_clear()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _helper_stub(self, *args, stdin=None, timeout=30):
        cmd = args[0] if args else ""
        if cmd == "homebridge-set":
            os.makedirs(os.path.dirname(self.creds), exist_ok=True)
            with open(self.creds, "w", encoding="utf-8") as fh:
                json.dump({"username": stdin["username"], "password": stdin["password"]}, fh)
            os.chmod(self.creds, 0o600)
            return True, {"ok": True}
        if cmd == "homebridge-get":
            try:
                doc = json.load(open(self.creds, encoding="utf-8"))
            except (OSError, ValueError):
                return False, {}
            return True, {"username": doc.get("username"), "password": doc.get("password")}
        if cmd == "homebridge-delete":
            try:
                os.unlink(self.creds)
            except FileNotFoundError:
                pass
            return True, {"ok": True}
        return False, {}

    def _setup(self, uids=None):
        uids = uids or [("light-1", "Porch Light")]
        status, body = self.c.req("POST", "/api/integrations/homebridge/verify", {
            "base": self.hb.base, "username": "grok-station", "password": SECRET, "stepup": GOOD})
        self.assertEqual(status, 200, body)
        status, saved = self.c.req("POST", "/api/integrations/homebridge/devices", {"devices": [
            {"uid": uid, "name": name, "aliases": []} for uid, name in uids]})
        self.assertEqual(status, 200, saved)
        self.home._cache.clear()
        self.home._hb_mem_clear()
        self.hb.requests.clear()
        self.hb.logins = 0
        self.hb.tokens.clear()
        return saved

    def _quiet(self, blob):
        text = blob if isinstance(blob, str) else json.dumps(blob)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(MARKER, text)
        self.assertNotIn("Porch", text)
        self.assertNotIn("light-1", text)

    async def _run(self, text):
        reg = self.home.load_registry()
        intent = self.home.home_match(text, reg)
        self.assertIsNotNone(intent, text)
        async with aiohttp.ClientSession() as http:
            ok = await self.home.run_intent(http, intent, self.lines.append)
        return ok, intent

    async def test_transcript_voice_off_then_on(self):
        saved = self._setup([("light-1", "Porch Light"), ("fan-1", "Garage Fan")])
        reg = self.home.load_registry()
        self.assertIsNone(self.home.home_match("is the porch light on", reg))
        self.assertIsNone(self.home.home_match("turn off the lights", reg))
        self.assertFalse(self.home.voice_on(reg))
        self.hb.requests.clear()

        def prepare():
            self.home._cache.clear()
            self.home._hb_mem_clear()

        out = await thf.drive_transcript(self.relay, "hey grok turn off the porch light", prepare)
        puts = [r for r in self.hb.requests if r["method"] == "PUT"]
        self.assertEqual(puts, [])
        self.assertEqual([e for e in out["events"] if e.get("type") == "home_tone"], [])
        self.assertTrue(any(self.home.VOICE_OFF_SAY in str(a) for a in self.tts), self.tts)
        self._quiet("".join(self.audits))
        self.assertTrue(any("home-off off" in a for a in self.audits), self.audits)

        status, body = self.c.req("PUT", "/api/integrations/wled/voice", {"on": True, "password": GOOD})
        self.assertEqual(status, 200, body)
        self.assertTrue(gsi.load()["integrations"]["homebridge"]["voice"])
        self.tts.clear()
        self.audits.clear()
        self.lines.clear()
        self.hb.requests.clear()
        self.home._cache.clear()
        out = await thf.drive_transcript(self.relay, "hey grok turn off the porch light", prepare)
        puts = [r for r in self.hb.requests if r["method"] == "PUT"]
        self.assertEqual(len(puts), 1, self.hb.requests)
        self.assertEqual(puts[0]["path"], "/api/accessories/light-1")
        self.assertEqual(puts[0]["body"], {"characteristicType": "On", "value": 0})
        self.assertEqual([e for e in out["events"] if e.get("type") == "home_tone"], [{"type": "home_tone", "ok": True}])
        self.assertTrue(any("home homebridge off ok" in ln for ln in self.lines), self.lines)
        self._quiet("\n".join(self.lines))
        self._quiet("".join(self.audits))
        self.assertTrue(any("home-off ok" in a for a in self.audits), self.audits)
        self.assertFalse(any(self.home.VOICE_OFF_SAY in str(a) for a in self.tts))
        # The saved id is in the registry, not the audit line.
        self.assertNotIn(saved["devices"][0]["id"], "".join(self.audits))

        self.hb.requests.clear()
        ok, intent = await self._run("turn off the garage fan")
        self.assertTrue(ok)
        self.assertEqual(intent["char"], "Active")
        put = next(r for r in self.hb.requests if r["method"] == "PUT")
        self.assertEqual(put["body"], {"characteristicType": "Active", "value": 0})
        self.assertTrue(any("home homebridge off ok" in ln for ln in self.lines))

    async def test_expired_token_relogs_and_a_daily_admin_check_stops_control(self):
        self._setup()
        self.hb.token_exp = int(time.time()) + 10
        ok, _intent = await self._run("turn off the porch light")
        self.assertTrue(ok)
        self.assertEqual(self.hb.logins, 1)
        self.hb.requests.clear()
        ok, _intent = await self._run("turn on the porch light")
        self.assertTrue(ok)
        self.assertEqual(self.hb.logins, 2)
        put = next(r for r in self.hb.requests if r["method"] == "PUT")
        self.assertEqual(put["body"]["value"], 1)

        self.hb.reset()
        self.hb.mode = "admin"
        self.home._hb_mem_clear()
        ok, _intent = await self._run("turn off the porch light")
        self.assertFalse(ok)
        self.assertFalse(any(r["method"] == "PUT" for r in self.hb.requests))
        self.assertEqual(json.load(open(self.block, encoding="utf-8"))["blocked"], "admin")
        self._quiet("\n".join(self.lines))
        # Later commands stay off without another PUT.
        self.hb.requests.clear()
        ok, _intent = await self._run("turn off the porch light")
        self.assertFalse(ok)
        self.assertEqual(self.hb.requests, [])

    async def test_put_401_retries_once_and_a_bad_relogin_flags_signin(self):
        self._setup()
        self.hb.put_401_left = 1
        ok, _intent = await self._run("turn off the porch light")
        self.assertTrue(ok)
        self.assertEqual(self.hb.logins, 2)
        puts = [r for r in self.hb.requests if r["method"] == "PUT"]
        self.assertEqual(len(puts), 2)
        self.assertFalse(os.path.exists(self.block))

        self.hb.reset()
        self.home._hb_mem_clear()
        self.hb.put_401_left = 1
        self.hb.login_ok_left = 1
        ok, _intent = await self._run("turn off the porch light")
        self.assertFalse(ok)
        self.assertEqual(json.load(open(self.block, encoding="utf-8"))["blocked"], "signin")
        self.assertEqual(len([r for r in self.hb.requests if r["method"] == "PUT"]), 1)

    async def test_timeout_and_users_500_do_not_flag_signin(self):
        self._setup()
        self.home._TIMEOUT = aiohttp.ClientTimeout(total=0.2)
        self.hb.mode = "slow"
        self.hb.delay = 1.0
        ok, _intent = await self._run("turn off the porch light")
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(self.block))
        self.assertFalse(any(r["method"] == "PUT" for r in self.hb.requests))

        self.hb.reset()
        self.home._hb_mem_clear()
        self.home._TIMEOUT = aiohttp.ClientTimeout(total=3)
        self.hb.mode = "users500"
        ok, _intent = await self._run("turn off the porch light")
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(self.block))
        self.assertFalse(any(r["method"] == "PUT" for r in self.hb.requests))

    async def test_https_to_a_plain_port_fails_closed(self):
        self._setup()
        doc = gsi.load()
        doc["integrations"]["homebridge"]["base"] = "https://127.0.0.1:%d" % self.hb.port
        gsi.save(doc)
        self.home._cache.clear()
        self.home._hb_mem_clear()
        self.hb.requests.clear()
        ok, _intent = await self._run("turn off the porch light")
        self.assertFalse(ok)
        self.assertFalse(any(r["method"] == "PUT" for r in self.hb.requests))

    async def test_quoted_uid_and_token_not_on_disk(self):
        self._setup([("a+b", "Plus Light")])
        ok, _intent = await self._run("turn off plus light")
        self.assertTrue(ok, self.lines)
        put = next(r for r in self.hb.requests if r["method"] == "PUT")
        self.assertIn("%2B", put["raw_path"])
        blob = ""
        for dirpath, _dirs, files in os.walk(self.tmp):
            for name in files:
                with open(os.path.join(dirpath, name), "rb") as fh:
                    blob += fh.read().decode("utf-8", "replace")
        self.assertNotIn(MARKER, blob)
        self.assertIn(SECRET, blob)  # the creds file, and nowhere else we just scanned is the token
        self.assertEqual(blob.count(SECRET), 1)
