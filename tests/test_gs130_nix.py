"""0.13.0 Nix security review (NOT GO on dc50676): L1-L3, I1-I3.

L1  a WLED name is resolved once; every address is checked (private or link-local, not loopback outside
    tests, not 169.254.169.254, not the station's own addresses) and the request goes to that address with
    the name only in the Host header. Literal station addresses are refused too. Relay and admin both.
L2  Voice home control is its own switch, off by default, password to turn on; saving a light never turns it
    on; when off, a voice command is answered with one fixed line and no light is touched.
L3  every voice light command writes one content-free audit line.
I1  adding a light needs the admin password or an open grace window; PATCH cannot change a host.
I3  the cloud helper prints "refused: station unreachable" on a certificate pin miss, and does not retry.
(I2, the mDNS source checks, is in test_gs130_no_listen.MdnsBrowse.)
"""
import asyncio, contextlib, importlib.machinery, importlib.util, io, json, os, ssl, sys, unittest, urllib.request

import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import test_gs130_integrations_api as tia  # noqa: E402  (admin test env; module import so its tests are not re-collected)
import test_gs130_home_flow as thf  # noqa: E402
from fakes.fake_wled import FakeWLED  # noqa: E402

gsi, ga = tia.gsi, tia.ga


def setUpModule():
    tia.setUpModule()


def tearDownModule():
    tia.tearDownModule()


class AdminPinned(unittest.TestCase):
    def setUp(self):
        self.saved = (gsi.resolve_host, gsi.station_addresses, urllib.request.build_opener)
        os.environ.pop("ADMIN_ALLOW_LOOPBACK_FOR_TESTS", None)

    def tearDown(self):
        gsi.resolve_host, gsi.station_addresses, urllib.request.build_opener = self.saved

    def test_name_resolving_to_a_bad_address_is_refused(self):
        gsi.station_addresses = lambda: {"10.0.0.8"}
        for bad in (["10.0.0.8"], ["127.0.0.1"], ["203.0.113.9"], ["169.254.169.254"], ["10.0.0.5", "127.0.0.1"]):
            gsi.resolve_host = lambda h, bad=bad: list(bad)
            self.assertFalse(gsi.host_ok("wled-kitchen.local"), bad)
            self.assertEqual(gsi.fetch("wled-kitchen.local", "/json/info"), ("refused", None))

    def test_literal_station_address_is_refused(self):
        gsi.station_addresses = lambda: {"10.0.0.8"}
        self.assertFalse(gsi.host_ok("10.0.0.8"))
        self.assertFalse(gsi.host_ok("10.0.0.8:8787"))
        self.assertTrue(gsi.host_ok("10.0.0.5"))

    def test_fetch_connects_to_the_checked_address_with_a_host_header(self):
        gsi.station_addresses = lambda: set()
        calls = []
        gsi.resolve_host = lambda h: calls.append(h) or ["10.0.0.5"]
        seen = {}

        class Opener:
            def open(self, req, timeout=None):
                seen["url"], seen["host"] = req.full_url, req.get_header("Host")
                raise OSError("stop here")
        urllib.request.build_opener = lambda *a: Opener()
        gsi.fetch("wled-kitchen.local:8080", "/json/info")
        self.assertEqual(seen, {"url": "http://10.0.0.5:8080/json/info", "host": "wled-kitchen.local:8080"})
        self.assertEqual(calls, ["wled-kitchen.local"])  # resolved once


class RelayPinned(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.relay, self.home = thf.load_mods()
        self.saved = (self.home.resolve_host, self.home.station_addresses)
        self.flag = os.environ.pop("HOME_ALLOW_LOOPBACK_FOR_TESTS", None)

    async def asyncTearDown(self):
        self.home.resolve_host, self.home.station_addresses = self.saved
        if self.flag is not None:
            os.environ["HOME_ALLOW_LOOPBACK_FOR_TESTS"] = self.flag

    def _resolve_to(self, addrs):
        async def fake(name):
            return list(addrs)
        self.home.resolve_host = fake

    async def test_resolved_bad_addresses_are_refused(self):
        self.home.station_addresses = lambda: {"10.0.0.8"}
        for bad in (["10.0.0.8"], ["127.0.0.1"], ["203.0.113.9"], ["169.254.169.254"], [], ["10.0.0.5", "10.0.0.8"]):
            self._resolve_to(bad)
            self.assertIsNone(await self.home.checked_target("wled-kitchen.local"), bad)
        self.assertIsNone(await self.home.checked_target("10.0.0.8:8787"))
        self.assertFalse(self.home.host_ok("10.0.0.8"))

    async def test_request_goes_to_the_checked_address(self):
        self.home.station_addresses = lambda: set()
        self._resolve_to(["10.0.0.5"])
        self.assertEqual(await self.home.checked_target("wled-kitchen.local"),
                         ("http://10.0.0.5/json/state", "wled-kitchen.local"))
        seen = []

        class Stub:
            def request(self, method, url, **kw):
                seen.append((url, kw.get("headers")))
                raise aiohttp.ClientError("stop")
        intent = {"id": "w-1", "kind": "wled", "host": "wled-kitchen.local", "action": "on"}
        self.assertFalse(await self.home.run_intent(Stub(), intent))
        self.assertEqual(seen, [("http://10.0.0.5/json/state", {"Host": "wled-kitchen.local"})])


class VoiceSwitchAndSave(unittest.TestCase):
    setUp = tia.IntegrationsAPI.setUp
    tearDown = tia.IntegrationsAPI.tearDown
    _host = tia.IntegrationsAPI._host

    def _item(self):
        return {"host": self._host(), "name": "Kitchen Strip", "aliases": []}

    def test_save_needs_the_password(self):
        status, body = self.c.req("POST", "/api/integrations/wled/devices", [self._item()])
        self.assertEqual(status, 403, body)
        self.assertEqual(gsi.load()["devices"], [])
        status, body = self.c.req("POST", "/api/integrations/wled/devices", {"devices": [self._item()], "password": "nope-nope-nope"})
        self.assertEqual(status, 403, body)
        self.assertEqual(gsi.load()["devices"], [])
        status, body = self.c.req("POST", "/api/integrations/wled/devices", {"devices": [self._item()], "password": tia.GOOD})
        self.assertEqual(status, 200, body)
        ident = body["devices"][0]["id"]
        self.assertEqual(self.c.req("PATCH", "/api/integrations/devices/" + ident, {"host": "10.0.0.5"})[0], 400)

    def test_saving_does_not_turn_on_voice_and_turning_on_needs_the_password(self):
        status, body = self.c.req("POST", "/api/integrations/wled/devices", {"devices": [self._item()], "password": tia.GOOD})
        self.assertEqual(status, 200, body)
        self.assertNotIn("voice", gsi.load()["integrations"]["wled"])
        self.assertFalse(self.c.req("GET", "/api/integrations")[1]["integrations"]["wled"]["voice"])
        ga.AUTH.stepup_open = lambda token: False  # as if the grace window the save opened had closed
        try:
            status, body = self.c.req("PUT", "/api/integrations/wled/voice", {"on": True})
        finally:
            del ga.AUTH.stepup_open
        self.assertEqual(status, 403, body)
        self.assertNotIn("voice", gsi.load()["integrations"]["wled"])

    def test_voice_on_with_password_and_off_without(self):
        self.c.req("POST", "/api/integrations/wled/devices", {"devices": [self._item()], "password": tia.GOOD})
        status, body = self.c.req("PUT", "/api/integrations/wled/voice", {"on": True, "password": tia.GOOD})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["integrations"]["wled"]["voice"])
        self.assertTrue(gsi.load()["integrations"]["wled"]["voice"])
        self.assertEqual(self.c.req("PUT", "/api/integrations/wled/voice", {"on": False})[0], 200)
        self.assertNotIn("voice", gsi.load()["integrations"]["wled"])
        self.assertEqual(self.c.req("PUT", "/api/integrations/wled/voice", {"on": "yes"})[0], 400)

    def test_voice_cannot_turn_on_before_setup(self):
        status, body = self.c.req("PUT", "/api/integrations/wled/voice", {"on": True, "password": tia.GOOD})
        self.assertEqual(status, 400, body)


class VoiceOffFlow(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = thf.Flow.asyncSetUp
    asyncTearDown = thf.Flow.asyncTearDown

    async def _drive(self, voice):
        fake = FakeWLED()
        await fake.start()
        audits = []
        old = self.relay._write_audit_line
        self.relay._write_audit_line = audits.append
        try:
            def prepare():
                reg = thf._reg(fake.host, "w-nix")
                if voice:
                    reg["integrations"]["wled"]["voice"] = True
                else:
                    reg["integrations"]["wled"].pop("voice", None)
                with open(self.reg, "w") as fh:
                    json.dump(reg, fh)
                self.home._cache.clear()
            out = await thf.drive_transcript(self.relay, "hey grok turn off the kitchen strip", prepare)
        finally:
            self.relay._write_audit_line = old
            await fake.close()
        return out, fake, audits

    async def test_voice_off_says_so_and_touches_no_light(self):
        out, fake, audits = await self._drive(False)
        self.assertEqual([e for e in out["events"] if e.get("type") == "home_tone"], [])
        self.assertEqual([r for r in fake.requests if r["method"] == "POST"], [])
        self.assertTrue(any(self.home.VOICE_OFF_SAY in a for a in self.tts), self.tts)
        self.assertEqual([a.split(" ", 1)[1] for a in audits], ["voice - home-off off -\n"])

    async def test_voice_on_runs_and_audits_content_free(self):
        out, fake, audits = await self._drive(True)
        self.assertEqual([e for e in out["events"] if e.get("type") == "home_tone"], [{"type": "home_tone", "ok": True}])
        self.assertEqual([a.split(" ", 1)[1] for a in audits], ["voice - home-off ok -\n"])
        for banned in ("Kitchen", "kitchen", "w-nix", fake.host):
            self.assertNotIn(banned, "".join(audits))


class CloudHelperPinMiss(unittest.TestCase):
    def test_pin_miss_prints_station_unreachable(self):
        path = os.path.join(ROOT, "tools", "grok-station-reply")
        loader = importlib.machinery.SourceFileLoader("gsr_nix", path)
        spec = importlib.util.spec_from_loader("gsr_nix", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        tries = []

        def boom(*a, **k):
            tries.append(k)
            raise ssl.SSLError("pin")
        mod.post_reply = boom

        class Args:
            station, insecure = "https://station.example:8443", True
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mod._do_post(Args(), b"{}", "ab" * 32)
        self.assertEqual(rc, 1)
        self.assertEqual(err.getvalue(), "refused: station unreachable\n")
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(tries), 1)  # no unpinned retry


class Docs(unittest.TestCase):
    def test_ack_comment_and_bot_id_docstring(self):
        src = open(os.path.join(ROOT, "relay", "botreply.py")).read()
        self.assertIn("# An acknowledgement and nothing else", src[:src.index("_ACK_LIKE = re.compile(")][-600:])
        self.assertNotIn("stay editable after this", ga.bot_id_from_name.__doc__)
