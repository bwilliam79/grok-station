"""0.13.0 regression (request vs_El5Q…): a bot's reply blocked by the station's PII filter used to leave the
station thinking until the 3-minute limit, and the home helper printed a bare "refused". Now the reply is
answered 400 with a content-free reason code, the waiting handoff ends at once with a visible message, and
the helpers print a one-line reason. The blocked text is never echoed."""
import asyncio, base64, json, os, sys, time, unittest

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from nacl.public import SealedBox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_avatar  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMAIL = "max.example@example.com"


class BlockedReply(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_avatar.Avatar.asyncSetUp
    asyncTearDown = test_avatar.Avatar.asyncTearDown

    def _reply(self, rid, text):
        doc = {"action_class": "answer", "kind": "reply", "request_id": rid, "text": text, "tier": 0}
        signed = self.signer.sign(json.dumps(doc, separators=(",", ":"), sort_keys=True).encode())
        sealed = SealedBox(self.station.public_key).encrypt(signed)
        return json.dumps({"v": 1, "bot": "jules", "sealed": base64.b64encode(sealed).decode()}).encode()

    async def test_pii_reply_ends_the_handoff_with_a_message(self):
        r = self.relay
        got_back = []

        async def hook(req):
            rid = (await req.json())["request_id"]

            async def send():
                await asyncio.sleep(0.1)
                resp = await self.client.post("/voice/reply", data=self._reply(rid, "Write to " + EMAIL + " today."),
                                              headers={"Content-Type": "application/json"})
                got_back.append((resp.status, await resp.text()))
            asyncio.get_running_loop().create_task(send())
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/hook", hook)
        srv = TestServer(app)
        await srv.start_server()
        old_rate, old_wait = r._reply_rate, r.REPLY_WAIT
        r._reply_rate, r.REPLY_WAIT = None, 8.0
        r.write_webhooks({"jules": {"url": str(srv.make_url("/hook")), "token": "hook-token-aaaa", "saved_at": 1}})
        lines = []
        r.log = lambda *a: lines.append(" ".join(map(str, a)))
        try:
            t0 = time.monotonic()
            async with ClientSession() as http:
                text, verdict = await r.ask_teammate(http, "jules", "who should I email")
            took = time.monotonic() - t0
        finally:
            r._reply_rate, r.REPLY_WAIT = old_rate, old_wait
            await srv.close()
        self.assertLess(took, 4.0, "the station kept thinking after the reply was blocked")
        self.assertIsNone(text)
        self.assertEqual(verdict, "blocked-pii")
        status, body = got_back[0]
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body).get("reason"), "pii")
        self.assertNotIn(EMAIL, body)
        self.assertFalse(any(EMAIL in l for l in lines))
        self.assertEqual(r.handoff_line("Jules", "blocked-pii"),
                         "Answer blocked: it contained personal info (PII filter is on).")
        self.assertEqual(r.handoff_line("Jules", "blocked-secret"), "Answer blocked: it looked like a secret.")
        self.assertEqual(r.handoff_line("Jules", "blocked"), "Couldn't deliver the answer.")


class HelperLines(unittest.TestCase):
    def _load(self, name, rel):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader(name, os.path.join(ROOT, rel))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
        loader.exec_module(mod)
        return mod

    def test_reason_lines(self):
        home = self._load("gsr_home_pii", "scripts/gs-seal-reply")
        cloud = self._load("gsr_cloud_pii", "tools/grok-station-reply")
        for mod in (home, cloud):
            self.assertEqual(mod.refusal_line("pii"), "refused: blocked by station PII filter (personal info in reply)")
            self.assertEqual(mod.refusal_line("secret"), "refused: looks like a secret")
            self.assertEqual(mod.refusal_line("unreachable"), "refused: station unreachable")
            self.assertEqual(mod.refusal_line("expired"), "refused: request expired")
            self.assertEqual(mod.refusal_line("<script>"), "refused")
            self.assertEqual(mod.reason_from_body(b'{"error":"bad request","reason":"pii"}'), "pii")
            self.assertIsNone(mod.reason_from_body(b'{"reason":"' + EMAIL.encode() + b'"}'))


if __name__ == "__main__":
    unittest.main()
