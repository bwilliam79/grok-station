"""WLED runtime against a fake device, including one transcript through relay.session."""
import asyncio, base64, contextlib, json, os, socket, sys, tempfile, time, unittest

import aiohttp
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes.fake_wled import FakeWLED

AUDIO = base64.b64encode(b"\0\0" * 240).decode()
PRE = "MODEL-PREANSWER"


def load_mods():
    """Import relay after the token env is set. A later test file must not lose that import."""
    sys.path.insert(0, os.path.join(ROOT, "relay"))
    first = "relay" not in sys.modules
    if first:
        os.environ.setdefault("RELAY_TOKEN", "test-token")
        os.environ.setdefault("XAI_API_KEY", "test-key")
        os.environ.setdefault("ALERT_DIR", os.path.join(tempfile.mkdtemp(), "alerts"))
    import relay
    import home
    return relay, home


def _reg(host, device_id):
    return {
        "version": 1,
        "integrations": {"wled": {"enabled": True}},
        "devices": [{
            "id": device_id, "kind": "wled", "host": host, "name": "Kitchen Strip",
            "aliases": ["kitchen lights"], "type": "light",
            "caps": ["onoff", "bri", "color", "preset"], "presets": {"party": 3},
        }],
    }


def write_reg(path, host, device_id, home_mod):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_reg(host, device_id), fh)
    home_mod._cache.pop(path, None)


def closed_port():
    """A 127.0.0.1 port that is not listening. Not port 1."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    finally:
        sock.close()
    if port <= 1:
        raise AssertionError("expected a high port")
    return port


async def drive_transcript(relay, transcript, prepare, on_ready=None, on_done=None, timeout=8):
    """One utterance through relay.session. prepare() runs after the sockets are up, before mic audio.

    on_done() runs after the turn, before those sockets are closed.
    """
    upstream, events, binaries = [], [], []

    async def xai(req):
        s = web.WebSocketResponse()
        await s.prepare(req)
        armed = True
        async for m in s:
            if m.type != WSMsgType.TEXT:
                continue
            ev = json.loads(m.data)
            upstream.append(ev)
            if ev.get("type") == "session.update":
                await s.send_json({"type": "session.updated"})
            elif ev.get("type") == "input_audio_buffer.clear":
                armed = True
            elif ev.get("type") == "input_audio_buffer.append" and armed:
                armed = False
                await s.send_json({"type": "input_audio_buffer.speech_started"})
                await s.send_json({"type": "input_audio_buffer.speech_stopped"})
                await s.send_json({"type": "response.created"})
                await s.send_json({"type": "conversation.item.input_audio_transcription.completed",
                                   "transcript": transcript})
                await s.send_json({"type": "response.audio.delta", "delta": AUDIO})
                await s.send_json({"type": "response.text", "text": PRE})
                await s.send_json({"type": "response.done"})
        return s

    xapp = web.Application()
    xapp.router.add_get("/rt", xai)
    xs = TestServer(xapp)
    client = ws = mic = None
    old_url = relay.URL
    try:
        await xs.start_server()
        relay.URL = str(xs.make_url("/rt")).replace("http", "ws")
        rapp = web.Application()
        rapp.router.add_get("/ws", relay.session)
        client = TestClient(TestServer(rapp))
        await client.start_server()
        prepare()
        if on_ready:
            on_ready()
        ws = await client.ws_connect("/ws", headers={"Authorization": "Bearer test-token"})

        async def pump():
            while not ws.closed:
                await ws.send_bytes(b"\0\0" * 1920)
                await asyncio.sleep(0.05)

        mic = asyncio.create_task(pump())

        def take(msg):
            if msg.type == WSMsgType.BINARY:
                binaries.append(bytes(msg.data))
            elif msg.type == WSMsgType.TEXT:
                events.append(json.loads(msg.data))

        async def read():
            async for msg in ws:
                take(msg)
                if msg.type == WSMsgType.TEXT and events and events[-1].get("type") == "turn_done":
                    while True:
                        try:
                            nxt = await asyncio.wait_for(ws.receive(), 0.3)
                        except asyncio.TimeoutError:
                            return
                        if nxt.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                            return
                        take(nxt)

        try:
            await asyncio.wait_for(read(), timeout)
        except asyncio.TimeoutError:
            kinds = [e.get("type") for e in events]
            raise AssertionError("no turn_done in %ss; station=%s" % (timeout, kinds))
        if on_done:
            on_done()
        return {"events": events, "upstream": upstream, "binaries": binaries}
    finally:
        if mic:
            mic.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await mic
        if ws is not None and not ws.closed:
            await ws.close()
        if client is not None:
            await client.close()
        await xs.close()
        relay.URL = old_url


class Flow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.relay, self.home = load_mods()
        self.tmp = tempfile.mkdtemp()
        self.reg = os.path.join(self.tmp, "integrations.json")
        self.env = {k: os.environ.get(k) for k in ("HOME_REGISTRY", "HOME_ALLOW_LOOPBACK_FOR_TESTS")}
        os.environ["HOME_REGISTRY"] = self.reg
        os.environ["HOME_ALLOW_LOOPBACK_FOR_TESTS"] = "1"
        self.home._cache.clear()
        self.home._offline.clear()
        self.lines = []
        self.tts = []
        self._log, self._tts, self._url = self.relay.log, self.relay.tts_pcm, self.relay.URL
        self.relay.log = lambda *a: self.lines.append(" ".join(map(str, a)))

        async def fake_tts(*a, **k):
            self.tts.append(a)
            return b"TTS"

        self.relay.tts_pcm = fake_tts

    async def asyncTearDown(self):
        self.relay.log = self._log
        self.relay.tts_pcm = self._tts
        self.relay.URL = self._url
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.home._cache.clear()
        self.home._offline.clear()

    def _intent(self, text, host, device_id="w-direct"):
        reg = _reg(host, device_id)
        intent = self.home.home_match(text, reg)
        self.assertIsNotNone(intent, text)
        return intent

    async def _run(self, fake, text, device_id="w-direct"):
        intent = self._intent(text, fake.host, device_id)
        async with aiohttp.ClientSession() as http:
            ok = await self.home.run_intent(http, intent, self.lines.append)
        return ok

    async def test_routes_and_command_bodies(self):
        fake = FakeWLED()
        await fake.start()
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get("http://%s/json/info" % fake.host) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual((await resp.json())["ver"], "16.0.0")
                async with http.get("http://%s/presets.json" % fake.host) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertIn("3", await resp.json())
            cases = (
                ("turn on the kitchen strip", {"on": True}, "on"),
                ("turn off the kitchen strip", {"on": False}, "off"),
                ("set kitchen strip to 50 percent", {"on": True, "bri": 128}, "bri"),
                ("set kitchen strip to 40 percent", {"on": True, "bri": 102}, "bri"),
                ("dim kitchen strip to 0", {"on": True, "bri": 0}, "bri"),
                ("set kitchen strip to 100 percent", {"on": True, "bri": 255}, "bri"),
                ("set the kitchen strip to red", {"on": True, "seg": [{"col": [[255, 0, 0]]}]}, "color"),
                ("kitchen strip preset party", {"ps": 3}, "preset"),
            )
            for text, body, _action in cases:
                self.lines.clear()
                fake.requests.clear()
                self.assertTrue(await self._run(fake, text), text)
                posts = [r for r in fake.requests if r["method"] == "POST"]
                self.assertEqual(posts, [{"method": "POST", "path": "/json/state", "body": body}], text)
                self.assertEqual(self.lines, ["home wled %s ok" % _action], text)
                blob = "\n".join(self.lines)
                for banned in ("Kitchen", "Strip", "wled-kitchen", "127.0.0.1", "party", "red", fake.host):
                    self.assertNotIn(banned, blob, text)
        finally:
            await fake.close()

    async def test_brighten_and_dim_clamp(self):
        for bri, text, body in ((128, "brighten the kitchen strip", {"bri": 179}),
                                (250, "brighten the kitchen strip", {"bri": 255}),
                                (10, "dim the kitchen strip", {"bri": 0})):
            fake = FakeWLED(state={"bri": bri})
            await fake.start()
            try:
                self.lines.clear()
                self.assertTrue(await self._run(fake, text, "w-step-%d" % bri))
                self.assertEqual(fake.requests[0]["method"], "GET")
                self.assertEqual(fake.requests[1]["method"], "POST")
                self.assertEqual(fake.requests[1]["body"], body)
                self.assertNotIn("on", fake.requests[1]["body"])
            finally:
                await fake.close()

    async def test_loopback_refused_without_the_flag(self):
        fake = FakeWLED()
        await fake.start()
        try:
            os.environ.pop("HOME_ALLOW_LOOPBACK_FOR_TESTS", None)
            intent = self._intent("turn off the kitchen strip", fake.host, "w-loop")
            async with aiohttp.ClientSession() as http:
                ok = await self.home.run_intent(http, intent, self.lines.append)
            self.assertFalse(ok)
            self.assertEqual(fake.requests, [])
            self.assertEqual(self.lines, ["home wled off fail"])
            os.environ["HOME_ALLOW_LOOPBACK_FOR_TESTS"] = "1"
            self.lines.clear()
            async with aiohttp.ClientSession() as http:
                ok = await self.home.run_intent(http, intent, self.lines.append)
            self.assertTrue(ok)
            self.assertEqual(fake.requests[-1]["body"], {"on": False})
        finally:
            await fake.close()

    async def test_redirect_is_not_followed(self):
        second = FakeWLED()
        first = FakeWLED(mode="redirect")
        await second.start()
        await first.start()
        first.location = "http://127.0.0.1:%d/json/state" % second.port
        try:
            self.assertFalse(await self._run(first, "turn off the kitchen strip", "w-redir"))
            self.assertEqual(len(first.requests), 1)
            self.assertEqual(first.requests[0]["body"], {"on": False})
            self.assertEqual(second.requests, [])
            self.assertEqual(self.lines, ["home wled off fail"])
            self.assertIsNotNone(self.home.offline_since("w-redir"))
        finally:
            await first.close()
            await second.close()

    async def test_timeout_and_body_cap(self):
        slow = FakeWLED(mode="slow")
        await slow.start()
        try:
            t0 = time.monotonic()
            ok = await self._run(slow, "turn on the kitchen strip", "w-slow")
            elapsed = time.monotonic() - t0
            self.assertFalse(ok)
            self.assertGreater(elapsed, 2.5)
            self.assertLess(elapsed, 4.5)
            self.assertEqual(self.lines, ["home wled on fail"])
        finally:
            await slow.close()
        big = FakeWLED(mode="big")
        await big.start()
        try:
            self.lines.clear()
            self.assertFalse(await self._run(big, "turn off the kitchen strip", "w-big"))
            self.assertEqual(self.lines, ["home wled off fail"])
        finally:
            await big.close()
        exact = FakeWLED(mode="exact")
        await exact.start()
        try:
            self.lines.clear()
            self.assertTrue(await self._run(exact, "turn on the kitchen strip", "w-exact"))
            self.assertEqual(self.lines, ["home wled on ok"])
        finally:
            await exact.close()

    async def test_transcript_turns_the_light_off_and_plays_a_tone(self):
        fake = FakeWLED()
        await fake.start()
        try:
            def prepare():
                write_reg(self.reg, fake.host, "w-flow-off", self.home)

            out = await drive_transcript(self.relay, "hey grok turn off the kitchen strip", prepare)
            types = [e.get("type") for e in out["events"]]
            self.assertIn("home_tone", types)
            self.assertLess(types.index("home_tone"), types.index("turn_done"))
            tones = [e for e in out["events"] if e.get("type") == "home_tone"]
            self.assertEqual(tones, [{"type": "home_tone", "ok": True}])
            for banned in ("heard", "reply", "reply_delta", "response_started"):
                self.assertNotIn(banned, types)
            self.assertNotIn(PRE, json.dumps(out["events"]))
            ups = [e.get("type") for e in out["upstream"]]
            self.assertNotIn("response.create", ups)
            self.assertEqual(self.tts, [])
            self.assertEqual(out["binaries"], [])
            posts = [r for r in fake.requests if r["method"] == "POST"]
            self.assertEqual(posts, [{"method": "POST", "path": "/json/state", "body": {"on": False}}])
            home_lines = [ln for ln in self.lines if ln.startswith("home ")]
            self.assertEqual(home_lines, ["home wled off ok"])
            blob = "\n".join(self.lines)
            for banned in ("Kitchen", "Strip", "w-flow-off", "wled-kitchen", fake.host):
                self.assertNotIn(banned, blob)
        finally:
            await fake.close()

    async def test_unreachable_transcript_plays_the_fail_tone(self):
        port_box = {}

        def prepare():
            port_box["port"] = closed_port()
            write_reg(self.reg, "127.0.0.1:%d" % port_box["port"], "w-down", self.home)

        out = await drive_transcript(self.relay, "turn off the kitchen strip", prepare)
        types = [e.get("type") for e in out["events"]]
        tones = [e for e in out["events"] if e.get("type") == "home_tone"]
        self.assertEqual(tones, [{"type": "home_tone", "ok": False}])
        self.assertNotIn("heard", types)
        self.assertNotIn("reply", types)
        self.assertEqual([ln for ln in self.lines if ln.startswith("home ")], ["home wled off fail"])
        self.assertIsNotNone(self.home.offline_since("w-down"))
        blob = "\n".join(self.lines)
        self.assertNotIn("Kitchen", blob)
        self.assertNotIn("127.0.0.1:%d" % port_box["port"], blob)
        ups = [e.get("type") for e in out["upstream"]]
        self.assertNotIn("response.create", ups)
        self.assertEqual(self.tts, [])
