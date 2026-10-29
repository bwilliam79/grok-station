"""Nix review fixes: admin ingress length parsing, per-route body caps, Pillow off the loop, pin release check."""
import io, os, subprocess, sys, unittest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "admin"))
sys.path.insert(0, os.path.join(ROOT, "relay"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# test_admin sets the admin environment (temp state dir, fake helper) before grok_admin is first imported. Importing
# grok_admin here first fixed its paths at /var/lib/grok-station for every later test file in the same run.
import test_admin  # noqa: E402
ga = test_admin.ga


class _Fake:
    def __init__(self, headers, body=b""):
        self.headers = headers
        self.rfile = io.BytesIO(body)
        self.client_address = ("127.0.0.1", 5000)
        self.sent = None

    def send(self, status, doc):
        self.sent = (status, doc)


class AdminIngressLength(unittest.TestCase):
    def setUp(self):
        self.old = ga.lan_ok
        ga.lan_ok = lambda ip, *a: True

    def tearDown(self):
        ga.lan_ok = self.old

    def go(self, headers, path="/voice/avatar"):
        f = _Fake(headers)
        ga.Handler.voice_ingress(f, "POST", path)
        return f.sent[0]

    def test_negative_non_numeric_chunked_and_over_cap(self):
        self.assertEqual(self.go({"Content-Length": "-1"}), 400)
        self.assertEqual(self.go({"Content-Length": "abc"}), 400)
        self.assertEqual(self.go({"Content-Length": "10", "Transfer-Encoding": "chunked"}), 411)
        self.assertEqual(self.go({"Transfer-Encoding": "chunked"}), 411)
        self.assertEqual(self.go({"Content-Length": str((4 << 20) + 1)}), 413)
        self.assertEqual(self.go({"Content-Length": "8193"}, "/voice/reply"), 413)


class RelayBodyCaps(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import relay as r
        self.r = r

        async def plain(req):
            await req.read()
            return web.json_response({"ok": True})

        async def capped(req):
            raw = await r._read_capped(req, 4 << 20)
            return web.json_response({"n": -1 if raw is None else len(raw)})

        app = web.Application()  # same as relay main()
        app.add_routes([web.post("/plain", plain), web.post("/capped", capped)])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_default_cap_kept_on_other_routes(self):
        big = b"x" * (2 << 20)  # over aiohttp's 1 MB default
        self.assertEqual((await self.client.post("/plain", data=big)).status, 413)
        got = await (await self.client.post("/capped", data=big)).json()
        self.assertEqual(got["n"], 2 << 20)
        over = await (await self.client.post("/capped", data=b"x" * ((4 << 20) + 1))).json()
        self.assertEqual(over["n"], -1)

    def test_main_has_no_global_body_override_and_pillow_runs_in_a_thread(self):
        src = open(os.path.join(ROOT, "relay", "relay.py")).read()
        self.assertNotIn("client_max_size=", src)
        self.assertIn("await asyncio.to_thread(transcode_avatar, image)", src)
        self.assertIn("await asyncio.to_thread(transcode_avatar, raw)", src)


class PinsAtRelease(unittest.TestCase):
    def test_reply_tools_match_pins_at_release(self):
        # Feature branches change the reply tools before the re-pin, so this runs only for a release check.
        if os.environ.get("GS_RELEASE_CHECK") != "1":
            self.skipTest("set GS_RELEASE_CHECK=1 for a release")
        out = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "check-deploy-pins.py")],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        for word in ("differs at", "not pinned yet", "missing at"):
            self.assertNotIn(word, out.stdout + out.stderr)
