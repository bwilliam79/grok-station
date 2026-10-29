"""0.13.0 regression: a bot that acknowledges first and answers later. The station used to speak the
acknowledgement ("Checking, one moment.") as the answer and drop the real answer as a duplicate.

Real flow: ask_teammate posts the webhook; the fake bot posts sealed replies to the relay's /voice/reply."""
import asyncio, base64, json, os, sys, time, unittest

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from nacl.public import SealedBox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_avatar  # noqa: E402


class InterimThenFinal(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_avatar.Avatar.asyncSetUp
    asyncTearDown = test_avatar.Avatar.asyncTearDown

    def _reply(self, rid, text, **extra):
        doc = {"action_class": "answer", "kind": "reply", "request_id": rid, "text": text, "tier": 0}
        doc.update(extra)
        signed = self.signer.sign(json.dumps(doc, separators=(",", ":"), sort_keys=True).encode())
        sealed = SealedBox(self.station.public_key).encrypt(signed)
        return json.dumps({"v": 1, "bot": "jules", "sealed": base64.b64encode(sealed).decode()}).encode()

    async def _ask(self, script):
        """script: [(delay_s, text, extra)] posted by the fake bot after the webhook. Returns (answer, statuses)."""
        statuses, tasks = [], []

        async def hook(req):
            rid = (await req.json())["request_id"]

            async def send():
                for delay, text, extra in script:
                    await asyncio.sleep(delay)
                    resp = await self.client.post("/voice/reply", data=self._reply(rid, text, **extra),
                                                  headers={"Content-Type": "application/json"})
                    statuses.append(resp.status)
            tasks.append(asyncio.get_running_loop().create_task(send()))
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/hook", hook)
        srv = TestServer(app)
        await srv.start_server()
        r = self.relay
        old_rate = r._reply_rate
        r._reply_rate = None  # a fresh 6-per-30-s bucket: the tests in this file together send more than six
        self.addCleanup(setattr, r, "_reply_rate", old_rate)
        self.addCleanup(r._LATE.clear)
        r.write_webhooks({"jules": {"url": str(srv.make_url("/hook")), "token": "hook-token-aaaa", "saved_at": 1}})
        lines = []
        r.log = lambda *a: lines.append(" ".join(map(str, a)))
        try:
            async with ClientSession() as http:
                got = await r.ask_teammate(http, "jules", "what's on my calendar tonight")
            await asyncio.gather(*tasks)
        finally:
            await srv.close()
        return got, statuses, lines

    async def test_unflagged_ack_then_answer_speaks_the_answer(self):
        got, statuses, lines = await self._ask([(0.1, "Checking, one moment.", {}), (0.3, "Dinner is at 6.", {})])
        self.assertEqual(got, ("Dinner is at 6.", "ok"))
        self.assertEqual(statuses, [200, 200])
        self.assertTrue(any("reply=1 interim" in l for l in lines), lines)
        self.assertTrue(any("reply=2 final" in l for l in lines), lines)

    async def test_flagged_interim_keeps_the_handoff_open(self):
        got, statuses, _ = await self._ask([(0.1, "Looking through your calendar now, back in a bit.", {"interim": True}),
                                            (0.3, "Dinner is at 6.", {})])
        self.assertEqual(got, ("Dinner is at 6.", "ok"))
        self.assertEqual(statuses, [200, 200])

    async def test_only_one_interim_and_no_replay(self):
        body_rid = {}
        got, statuses, _ = await self._ask([(0.1, "On it.", {"interim": True}), (0.1, "Still on it.", {"interim": True}),
                                            (0.2, "Dinner is at 6.", {})])
        self.assertEqual(got, ("Dinner is at 6.", "ok"))
        self.assertEqual(statuses, [200, 409, 200])

    async def test_a_second_reply_after_a_final_one_is_spoken_late(self):
        r = self.relay
        spoken, old_sink, old_busy = [], r._LATE_SINK, r.stats["busy"]
        r._LATE_SINK, r.stats["busy"] = spoken.append, False
        try:
            got, statuses, _ = await self._ask([(0.1, "Your evening has two things on it tonight.", {}),
                                                (0.9, "Dinner is at 6 and practice at 7:30.", {}),
                                                (0.1, "Dinner is at 6 and practice at 7:30.", {})])
        finally:
            r._LATE_SINK, r.stats["busy"] = old_sink, old_busy
        self.assertEqual(got, ("Your evening has two things on it tonight.", "ok"))
        self.assertEqual(statuses[:2], [200, 200])
        self.assertEqual(statuses[2], 409)  # one follow-up only; never a replay
        self.assertEqual(len(spoken), 1)
        self.assertIn("Dinner is at 6 and practice at 7:30.", json.dumps(spoken[0]))


if __name__ == "__main__":
    unittest.main()


class InterimWire(unittest.TestCase):
    """The signed "interim" field: botreply accepts only true, in both answer shapes; both helpers can send it;
    the home helper's ledger allows one acknowledgement and one answer per request."""

    def test_botreply_schema(self):
        import importlib.machinery, importlib.util, tempfile, time as _t
        from nacl.public import PrivateKey
        from nacl.signing import SigningKey
        import botreply, handoff
        station, signer = PrivateKey.generate(), SigningKey.generate()
        rid = "vs_" + "b" * 22
        keys = {"killed": False, "keys": {"max": base64.b64encode(bytes(signer.verify_key)).decode()}}
        pend = {"request_id": rid, "persona": "max", "nonce": "n0", "expires_ms": int(_t.time() * 1000) + 60000}

        def go(doc):
            signed = signer.sign(json.dumps(doc, separators=(",", ":"), sort_keys=True).encode())
            raw = json.dumps({"v": 1, "bot": "max", "sealed": base64.b64encode(
                SealedBox(station.public_key).encrypt(signed)).decode()}).encode()
            return botreply.ingest(raw, station, keys, {"max"}, lambda _r: pend, set(), botreply.Limiter(60, 60))
        base = {"action_class": "answer", "request_id": rid, "text": "On it.", "tier": 0}
        self.assertTrue(go(dict(base, kind="reply", interim=True))["interim"])
        self.assertTrue(go(dict(base, interim=True))["interim"])  # the home helper's shape (no kind)
        self.assertNotIn("interim", go(dict(base, kind="reply")))
        for bad in (False, 1, "true"):
            with self.assertRaises(botreply.Reject):
                go(dict(base, kind="reply", interim=bad))
        self.assertTrue(botreply.ack_like("Checking, one moment."))
        self.assertFalse(botreply.ack_like("Checking your calendar: dinner at 6."))

    def test_helpers(self):
        import importlib.machinery, importlib.util, tempfile
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        def load(name, rel):
            loader = importlib.machinery.SourceFileLoader(name, os.path.join(root, rel))
            mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
            loader.exec_module(mod)
            return mod
        cloud = load("gsr_cloud_130", "tools/grok-station-reply")
        self.assertTrue(json.loads(cloud.canonical("vs_" + "c" * 22, "On it.", True))["interim"])
        self.assertNotIn("interim", json.loads(cloud.canonical("vs_" + "c" * 22, "Dinner.")))
        home = load("gsr_home_130", "scripts/gs-seal-reply")
        self.assertTrue(json.loads(home.canonical_answer("vs_" + "c" * 22, "On it.", True))["interim"])
        self.assertTrue(home._parser().parse_args(["reply", "--persona", "max", "--request-id", "vs_" + "c" * 22,
                                                   "--interim"]).interim)
        d = tempfile.mkdtemp()
        home.sent_path = lambda: os.path.join(d, "sent")
        home._open_private = lambda p: os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
        rid = "vs_" + "d" * 22
        home.claim_rid(rid, True)
        home.claim_rid(rid)
        with self.assertRaises(home.Refused):
            home.claim_rid(rid, True)
        with self.assertRaises(home.Refused):
            home.claim_rid(rid)
