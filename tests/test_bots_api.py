#!/usr/bin/env python3
"""Bots admin API on the relay: auth, webhook allowlist, persona overlay, minted voice token, and a capped test handoff.

Imported inside the tests so collecting this module does not initialize relay before the other suites set their env.
"""
import asyncio, hashlib, json, os, stat, tempfile, time, unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

GOOD = "https://api2.cursor.sh/automations/webhook/one"
GOOD_443 = "https://api2.cursor.sh:443/automations/webhook/two"
KEY1 = "first-key-aaaa"
KEY2 = "second-key-bbbb"


class BotsApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import handoff
        import relay
        self.handoff, self.relay = handoff, relay
        r = relay
        self.tmp = tempfile.mkdtemp()
        self.snap = {
            "WEBHOOKS_FILE": r.WEBHOOKS_FILE, "MINTED_FILE": r.MINTED_FILE, "PENDING_DIR": r.PENDING_DIR,
            "REPLIES_DIR": r.REPLIES_DIR, "PERSONAS_FILE": r.PERSONAS_FILE, "REPLY_WAIT": r.REPLY_WAIT,
            "ADMIN_TOKEN": r.ADMIN_TOKEN, "HANDOFF": r.HANDOFF, "_limits": r._limits, "log": r.log,
            "voices": set(r.VOICES),
            "personas": {k: (dict(v) if isinstance(v, dict) else v) for k, v in r._personas.items()},
            "hooks": {"mtime": r._hooks.get("mtime"), "map": dict(r._hooks.get("map") or {})},
            "fails": dict(r.fails), "locked": dict(r.locked),
        }
        r.WEBHOOKS_FILE = os.path.join(self.tmp, "webhooks.json")
        r.MINTED_FILE = os.path.join(self.tmp, "minted", "voice-tokens.json")
        r.PENDING_DIR = os.path.join(self.tmp, "pending")
        r.REPLIES_DIR = os.path.join(self.tmp, "replies")
        r.PERSONAS_FILE = os.path.join(self.tmp, "personas.json")
        r.ADMIN_TOKEN = b"admin-secret"
        r.VOICES.clear()
        r.VOICES.update({"ara", "rex", "luna"})
        r._personas.update(mtime=None, map={}, voices=None, hosts=[])
        r._hooks.update(mtime=None, map={})
        r.fails.clear()
        r.locked.clear()
        r._limits = handoff.Limits(os.path.join(self.tmp, "limits.json"), r.today)
        r._runs.clear()
        self.lines = []
        r.log = lambda *a: self.lines.append(" ".join(map(str, a)))
        self._registry()
        app = web.Application()
        app.add_routes(r.ADMIN_ROUTES)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    def _registry(self, personas=None):
        doc = {"personas": personas if personas is not None else [
            {"slug": "jules", "name": "Jules", "voice": "ara", "prompt": "The notes helper.", "aliases": [], "facts": []}]}
        with open(self.relay.PERSONAS_FILE, "w") as f:
            json.dump(doc, f)
        self.relay._personas["mtime"] = None

    def headers(self, token="admin-secret", fp="c" * 64, origin=None):
        h = {}
        if token is not None:
            h["Authorization"] = "Bearer " + token
        if fp is not None:
            h["X-Client-Cert-Fp"] = fp
        if origin is not None:
            h["Origin"] = origin
        return h

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    async def asyncTearDown(self):
        r = getattr(self, "relay", None)
        if r is not None:
            for t in list(r._run_tasks):
                t.cancel()
            if r._run_tasks:
                await asyncio.gather(*list(r._run_tasks), return_exceptions=True)
            r._runs.clear()
        if getattr(self, "client", None) is not None:
            await self.client.close()
        if r is None:
            return
        s = self.snap
        r.WEBHOOKS_FILE, r.MINTED_FILE = s["WEBHOOKS_FILE"], s["MINTED_FILE"]
        r.PENDING_DIR, r.REPLIES_DIR = s["PENDING_DIR"], s["REPLIES_DIR"]
        r.PERSONAS_FILE, r.REPLY_WAIT = s["PERSONAS_FILE"], s["REPLY_WAIT"]
        r.ADMIN_TOKEN, r.HANDOFF, r._limits, r.log = s["ADMIN_TOKEN"], s["HANDOFF"], s["_limits"], s["log"]
        r.VOICES.clear()
        r.VOICES.update(s["voices"])
        r._personas.clear()
        r._personas.update(s["personas"])
        r._hooks.clear()
        r._hooks.update(s["hooks"])
        r.fails.clear()
        r.fails.update(s["fails"])
        r.locked.clear()
        r.locked.update(s["locked"])

    async def test_auth(self):
        c = self.client
        self.assertEqual((await c.get("/admin/bots", headers=self.headers(token=None))).status, 401)
        self.assertEqual((await c.get("/admin/bots", headers=self.headers(token="wrong-token"))).status, 401)
        self.assertEqual((await c.get("/admin/bots", headers=self.headers(token="test-token"))).status, 401)  # relay token
        self.assertEqual((await c.get("/admin/bots", headers=self.headers(origin="https://evil.example"))).status, 403)
        ok = await c.get("/admin/bots", headers=self.headers())
        self.assertEqual(ok.status, 200)
        body = await ok.json()
        self.assertEqual([b["slug"] for b in body["bots"]], ["jules"])
        self.assertEqual(body["voices"], ["ara", "luna", "rex"])

    async def test_url_allowlist_and_unknown_persona(self):
        bad = [
            "http://api2.cursor.sh/hook",
            "https://example.com/hook",
            "https://api2.cursor.sh.evil.com/hook",
            "https://user:secret@api2.cursor.sh/hook",
            "https://api2.cursor.sh@evil.com/hook",
            "https://api2.cursor.sh:8443/hook",
            "https://api2.cursor.sh./hook",                                  # trailing dot: a redirect-y parser sends it elsewhere
            "https://api2.cursor.sh%2eevil.com/hook",
            "https://evil.example/redirect?next=https://api2.cursor.sh/hook",
        ]
        for url in bad:
            r = await self.client.put("/admin/bots/jules/webhook", headers=self.headers(),
                                      json={"url": url, "key": KEY1, "actor": "admin"})
            text = await r.text()
            self.assertEqual(r.status, 400, url)
            self.assertNotIn(url, text)
            self.assertNotIn(KEY1, text)
            self.assertFalse(os.path.exists(self.relay.WEBHOOKS_FILE), url)
        r = await self.client.put("/admin/bots/nobody/webhook", headers=self.headers(),
                                  json={"url": GOOD, "key": KEY1, "actor": "admin"})
        self.assertEqual(r.status, 404)
        self.assertNotIn(GOOD, await r.text())

    async def test_save_shows_last4_keeps_bak_and_rejects_a_bad_doc(self):
        h = self.headers()
        r = await self.client.put("/admin/bots/jules/webhook", headers=h, json={"url": GOOD, "key": KEY1, "actor": "admin"})
        text = await r.text()
        self.assertEqual(r.status, 200)
        got = json.loads(text); self.assertTrue(got.pop("reply_token").startswith("gsr_"))
        self.assertEqual(got, {"ok": True, "last4": "aaaa"})
        self.assertNotIn(KEY1, text)
        self.assertNotIn(GOOD, text)
        r = await self.client.put("/admin/bots/jules/webhook", headers=h, json={"url": GOOD_443, "key": KEY2, "actor": "admin"})
        text = await r.text()
        got = json.loads(text); got.pop("reply_token")
        self.assertEqual(got, {"ok": True, "last4": "bbbb"})
        self.assertNotIn(KEY2, text)
        self.assertNotIn(GOOD_443, text)
        listed = await (await self.client.get("/admin/bots", headers=h)).json()
        wh = listed["bots"][0]["webhook"]
        self.assertEqual(wh, {"configured": True, "last4": "bbbb", "saved_at": wh["saved_at"]})
        self.assertIsInstance(wh["saved_at"], int)
        blob = json.dumps(listed)
        self.assertNotIn(KEY1, blob)
        self.assertNotIn(KEY2, blob)
        self.assertNotIn("api2.cursor.sh", blob)
        logged = "\n".join(self.lines)
        for secret in (KEY1, KEY2, GOOD, GOOD_443):
            self.assertNotIn(secret, logged)
        bak = os.path.join(self.tmp, "webhooks.json.bak")
        self.assertEqual(self.mode(self.relay.WEBHOOKS_FILE), 0o600)
        self.assertEqual(self.mode(bak), 0o600)
        old = json.load(open(bak))
        self.assertEqual(old["jules"]["token"], KEY1)          # the previous file, one bak only
        self.assertNotIn(KEY2, open(bak).read())
        new = json.load(open(self.relay.WEBHOOKS_FILE))
        self.assertEqual(new["jules"]["token"], KEY2)
        self.assertEqual(self.handoff.load_webhooks(json.dumps(new))["jules"]["url"], GOOD_443)
        audit = open(self.relay.audit_path()).read()
        self.assertIn("webhook-save aaaa", audit)
        self.assertIn("webhook-save bbbb", audit)
        self.assertNotIn(KEY1, audit)
        self.assertNotIn(KEY2, audit)
        self.assertNotIn("api2.cursor.sh", audit)
        self.assertEqual(self.mode(self.relay.audit_path()), 0o600)
        before = open(self.relay.WEBHOOKS_FILE, "rb").read()
        bak_before = open(bak, "rb").read()
        self.assertFalse(self.relay.write_webhooks({"jules": {"url": "http://evil.example/h", "token": "12345678"}}))
        self.assertEqual(open(self.relay.WEBHOOKS_FILE, "rb").read(), before)
        real = self.handoff.load_webhooks
        self.handoff.load_webhooks = lambda raw: {}
        try:
            r = await self.client.put("/admin/bots/jules/webhook", headers=h,
                                      json={"url": GOOD, "key": "third-key-cccc", "actor": "admin"})
            text = await r.text()
            self.assertEqual(r.status, 400)
            self.assertNotIn("third-key-cccc", text)
            self.assertNotIn(GOOD, text)
        finally:
            self.handoff.load_webhooks = real
        self.assertEqual(open(self.relay.WEBHOOKS_FILE, "rb").read(), before)
        self.assertEqual(open(bak, "rb").read(), bak_before)
        self.assertFalse(any(n.startswith(".tmp-") for n in os.listdir(self.tmp)))

    async def test_add_rename_remove_mints_a_token_once(self):
        h = self.headers()
        reg = open(self.relay.PERSONAS_FILE).read()
        r = await self.client.post("/admin/bots", headers=h,
                                   json={"slug": "nova", "name": "Nova", "voice": "ara", "actor": "admin"})
        self.assertEqual(r.status, 201)
        body = await r.json()
        token = body["voice_token"]
        self.assertTrue(token.startswith("vt_") and len(token) > 10)
        self.assertEqual(body["slug"], "nova")
        minted = json.load(open(self.relay.MINTED_FILE))
        self.assertEqual(minted["agents"]["nova"]["hashes"], [hashlib.sha256(token.encode()).hexdigest()])
        self.assertEqual(minted["agents"]["nova"]["maxTier"], 0)
        self.assertEqual(len(minted["agents"]["nova"]["hashes"]), 1)
        self.assertEqual(self.mode(self.relay.MINTED_FILE), 0o600)
        for dirpath, _, files in os.walk(self.tmp):
            for fn in files:
                self.assertNotIn(token.encode(), open(os.path.join(dirpath, fn), "rb").read())
        self.assertNotIn(b"vt_", open(self.relay.MINTED_FILE, "rb").read())
        nova = self.relay.persona_list()["nova"]
        self.assertEqual(nova["prompt"], "You are Nova, one of the household's assistant team.")
        self.assertEqual(nova["facts"], [])
        self.assertEqual((await self.client.patch("/admin/bots/nova", headers=h, json={"name": "Nora", "actor": "admin"})).status, 200)
        self.assertEqual(self.relay.persona_list()["nova"]["name"], "Nora")
        self.assertEqual(self.relay.persona_list()["nova"]["prompt"], "You are Nora, one of the household's assistant team.")
        self.assertEqual((await self.client.patch("/admin/bots/jules", headers=h, json={"name": "Julie", "actor": "admin"})).status, 200)
        self.assertEqual(self.relay.persona_list()["jules"]["name"], "Julie")
        self.assertEqual(open(self.relay.PERSONAS_FILE).read(), reg)       # registry stays read-only
        listed = await (await self.client.get("/admin/bots", headers=h)).json()
        by = {b["slug"]: b for b in listed["bots"]}
        self.assertEqual(by["nova"]["source"], "added")
        self.assertEqual(by["nova"]["name"], "Nora")
        self.assertEqual(by["jules"]["source"], "registry")
        self.assertEqual(by["jules"]["name"], "Julie")
        r = await self.client.put("/admin/bots/nova/webhook", headers=h, json={"url": GOOD, "key": KEY1, "actor": "admin"})
        self.assertEqual(r.status, 200)
        r = await self.client.delete("/admin/bots/nova", headers=h, json={"actor": "admin"})
        self.assertEqual(await r.json(), {"ok": True})
        self.assertNotIn("nova", self.relay.persona_list())
        self.assertNotIn("nova", json.load(open(self.relay.MINTED_FILE))["agents"])
        self.assertNotIn("nova", json.load(open(self.relay.WEBHOOKS_FILE)))
        # a registry persona is hidden, not deleted from the file; its webhook and minted hash go
        self.relay.save_minted({"agents": {"jules": {"hashes": ["ab" * 32], "maxTier": 0, "updatedAt": 1}}})
        r = await self.client.delete("/admin/bots/jules", headers=h, json={"actor": "admin"})
        self.assertEqual((await r.json())["ok"], True)
        self.assertNotIn("jules", self.relay.persona_list())
        self.assertIn("jules", self.relay.load_overlay_doc()["hidden"])
        self.assertIn('"slug": "jules"', open(self.relay.PERSONAS_FILE).read())
        self.assertNotIn("jules", json.load(open(self.relay.MINTED_FILE))["agents"])
        hooks = json.load(open(self.relay.WEBHOOKS_FILE))
        self.assertNotIn("jules", hooks)
        audit = open(self.relay.audit_path()).read()
        self.assertRegex(audit, r"admin nova add -")
        self.assertRegex(audit, r"admin nova rename -")
        self.assertRegex(audit, r"admin jules rename -")
        self.assertRegex(audit, r"admin nova webhook-save aaaa")
        self.assertRegex(audit, r"admin nova remove aaaa")
        self.assertNotIn(KEY1, audit)
        self.assertNotIn(token, audit)
        self.assertNotIn("api2.cursor.sh", audit)

    def _audit(self):
        p = self.relay.audit_path()
        return open(p).read() if os.path.exists(p) else ""

    async def test_patch_voice_validates_persists_and_audits(self):
        h = self.headers()
        reg = open(self.relay.PERSONAS_FILE).read()
        bad = await self.client.patch("/admin/bots/jules", headers=h, json={"voice": "nope", "actor": "admin"})
        self.assertEqual(bad.status, 400)
        self.assertEqual(await bad.json(), {"error": "bad voice"})
        self.assertEqual(self.relay.persona_list()["jules"]["voice"], "ara")
        empty = await self.client.patch("/admin/bots/jules", headers=h, json={"actor": "admin"})
        self.assertEqual(empty.status, 400)
        self.assertNotIn("voice", self._audit())
        missing = await self.client.patch("/admin/bots/nobody", headers=h, json={"voice": "rex", "actor": "admin"})
        self.assertEqual(missing.status, 404)
        ok = await self.client.patch("/admin/bots/jules", headers=h, json={"voice": "REX", "actor": "admin"})
        self.assertEqual(ok.status, 200)
        self.assertEqual(self.relay.persona_list()["jules"]["voice"], "rex")
        self.assertEqual(self.relay.persona_list()["jules"]["name"], "Jules")
        self.assertEqual(self.relay.load_overlay_doc()["voices"]["jules"], "rex")
        self.assertEqual(open(self.relay.PERSONAS_FILE).read(), reg)
        before = self._audit()
        named = await self.client.patch("/admin/bots/jules", headers=h, json={"name": "Julie", "actor": "admin"})
        self.assertEqual(named.status, 200)
        added = self._audit()[len(before):]
        self.assertIn("rename", added)
        self.assertNotIn("voice", added)
        both = await self.client.patch("/admin/bots/jules", headers=h, json={"name": "Jules", "voice": "luna", "actor": "admin"})
        self.assertEqual(both.status, 200)
        self.assertEqual(self.relay.persona_list()["jules"]["name"], "Jules")
        self.assertEqual(self.relay.persona_list()["jules"]["voice"], "luna")
        made = await self.client.post("/admin/bots", headers=h, json={"slug": "nova", "name": "Nova", "voice": "ara", "actor": "admin"})
        self.assertEqual(made.status, 201)
        changed = await self.client.patch("/admin/bots/nova", headers=h, json={"voice": "rex", "actor": "admin"})
        self.assertEqual(changed.status, 200)
        self.assertEqual(self.relay.persona_list()["nova"]["voice"], "rex")
        self.assertEqual(self.relay.persona_list()["nova"]["name"], "Nova")
        ov = self.relay.load_overlay_doc()
        self.assertEqual(ov["added"]["nova"]["voice"], "rex")
        self.assertEqual(ov["voices"]["nova"], "rex")
        audit = self._audit()
        self.assertRegex(audit, r"admin jules voice -")
        self.assertRegex(audit, r"admin nova voice -")
        self.assertNotIn("nope", audit)

    async def _fake(self, mode):
        seen = {"bodies": [], "auths": []}

        async def hook(req):
            body = await req.json()
            seen["bodies"].append(body)
            seen["auths"].append(req.headers.get("Authorization", ""))
            pend_path = os.path.join(self.relay.PENDING_DIR, body["request_id"] + ".json")
            seen.setdefault("pending", []).append(json.load(open(pend_path)))
            if mode == "deny":
                return web.Response(status=401)
            if mode == "reply":
                async def later():
                    await asyncio.sleep(0.2)
                    os.makedirs(self.relay.REPLIES_DIR, exist_ok=True)
                    pend = json.load(open(os.path.join(self.relay.PENDING_DIR, body["request_id"] + ".json")))
                    json.dump({"request_id": body["request_id"], "agent": pend["persona"], "nonce": pend["nonce"],
                               "tier": 0, "action_class": "answer", "text": "ok"},
                              open(os.path.join(self.relay.REPLIES_DIR, body["request_id"] + ".json"), "w"))
                asyncio.get_running_loop().create_task(later())
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/hook", hook)
        srv = TestServer(app)
        await srv.start_server()
        return srv, seen

    def _seed_hook(self, url, key=KEY1):
        self.assertTrue(self.relay.write_webhooks({"jules": {"url": url, "token": key, "saved_at": 10}}))

    async def _poll(self, tid, timeout=3):
        deadline = time.monotonic() + timeout
        body = None
        while time.monotonic() < deadline:
            r = await self.client.get(f"/admin/bots/jules/test/{tid}", headers=self.headers())
            body = await r.json()
            if body.get("state") != "waiting":
                return body
            await asyncio.sleep(0.05)
        return body

    async def test_reply_ingress_on_the_station(self):
        """Webhook save mints a reply token; the bot reads its question and its reply lands for take_reply."""
        r = self.relay
        resp = await self.client.put("/admin/bots/jules/webhook", headers=self.headers(),
                                     json={"url": GOOD, "key": KEY1, "actor": "admin"})
        self.assertEqual(resp.status, 200)
        tok = (await resp.json())["reply_token"]
        self.assertTrue(tok.startswith("gsr_"))
        self.assertNotIn(tok, open(os.path.join(self.tmp, "reply_tokens.json")).read())  # hash only
        self.assertEqual(self.mode(os.path.join(self.tmp, "reply_tokens.json")), 0o600)
        rid = self.handoff.new_request_id()
        self.handoff.write_pending(r.PENDING_DIR, rid, "jules", "n0nce", time.time() * 1000 + 60000, question="Is it raining?")
        bot = {"Authorization": "Bearer " + tok}
        self.assertEqual((await self.client.get("/voice/request/" + rid)).status, 401)
        self.assertEqual((await self.client.get("/voice/request/" + rid, headers={"Authorization": "Bearer nope"})).status, 401)
        g = await self.client.get("/voice/request/" + rid, headers=bot)
        self.assertEqual(g.status, 200)
        self.assertEqual((await g.json())["question"], "Is it raining?")
        self.assertEqual((await self.client.post("/voice/reply", headers=bot, json={
            "request_id": rid, "text": "x", "tier": 1, "action_class": "note"})).status, 403)
        p = await self.client.post("/voice/reply", headers=bot, json={
            "request_id": rid, "text": "No rain today.", "tier": 0, "action_class": "answer"})
        self.assertEqual(p.status, 200)
        self.assertEqual(self.handoff.take_reply(r.REPLIES_DIR, rid, "jules", "n0nce"), ("No rain today.", None))
        # another bot's pending request is not visible with this token
        rid2 = self.handoff.new_request_id()
        self.handoff.write_pending(r.PENDING_DIR, rid2, "hank", "n2", time.time() * 1000 + 60000, question="q")
        self.assertEqual((await self.client.get("/voice/request/" + rid2, headers=bot)).status, 404)
        # removing the webhook revokes the token
        await self.client.delete("/admin/bots/jules/webhook", headers=self.headers(), json={"actor": "admin"})
        self.assertEqual((await self.client.get("/voice/request/" + rid, headers=bot)).status, 401)

    def test_admin_forwards_only_the_two_voice_paths(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "admin", "grok_admin.py")).read()
        self.assertIn('if p.startswith("/voice/"):\n            return self.voice_ingress(method, p)', src)
        self.assertIn('method == "POST" and p == "/voice/reply"', src)
        self.assertIn('if not lan_ok(self.client_address[0]):\n            return self.send(403', src)

    def test_handoff_on_by_default(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "relay", "relay.py")).read()
        self.assertIn('E("HANDOFF_ENABLED", "1")', src)

    async def test_webhook_round_trip_401_silence_cancel_and_caps(self):
        h = self.headers()
        old_wait = self.relay.REPLY_WAIT
        try:
            srv, seen = await self._fake("reply")
            self._seed_hook(str(srv.make_url("/hook")))
            self.relay.REPLY_WAIT = 2
            r = await self.client.post("/admin/bots/jules/test", headers=h, json={})
            self.assertEqual(r.status, 202)
            tid = (await r.json())["test_id"]
            body = await self._poll(tid)
            self.assertEqual(body["state"], "ok")
            self.assertIsInstance(body["answered_in_s"], float)
            self.assertGreater(body["answered_in_s"], 0)
            self.assertIsNone(body["error"])
            self.assertEqual(len(seen["bodies"]), 1)
            body0 = seen["bodies"][0]
            self.assertEqual(set(body0), {"source", "v", "request_id", "persona", "test"})
            self.assertEqual(body0["v"], 2)
            self.assertIs(body0["test"], True)
            for dropped in ("question", "pii_mode", "reply_by", "tier_max"):
                self.assertNotIn(dropped, body0)
            pend = seen["pending"][0]
            self.assertEqual(pend["question"], self.relay.TEST_QUESTION)
            self.assertIs(pend["test"], True)
            self.assertIs(pend["pii_mode"], True)
            self.assertEqual(pend["tier_max"], 0)
            self.assertTrue(pend["reply_by"])
            self.assertIsInstance(pend["expires_ms"], int)
            self.assertEqual(body0["persona"], "jules")
            self.assertEqual(seen["auths"], ["Bearer " + KEY1])
            limits = json.load(open(self.relay._limits.path))
            self.assertEqual(limits["bots"]["jules"]["n"], 1)             # a test counts like a handoff
            listed = await (await self.client.get("/admin/bots", headers=h)).json()
            self.assertEqual(listed["bots"][0]["status"]["state"], "connected")
            self.assertIsInstance(listed["bots"][0]["status"]["last_answered_at"], int)
            await srv.close()

            srv, seen = await self._fake("deny")
            self._seed_hook(str(srv.make_url("/hook")), "denied-key-zzzz")
            # gap is 30s; a fresh limits file so the 401 is not "too soon"
            self.relay._limits = self.handoff.Limits(os.path.join(self.tmp, "limits-401.json"), self.relay.today)
            r = await self.client.post("/admin/bots/jules/test", headers=h, json={})
            tid = (await r.json())["test_id"]
            body = await self._poll(tid, timeout=3)
            self.assertEqual(body["state"], "failed")
            self.assertEqual(body["error"], "Key was refused (401)")
            self.assertIsNone(body["answered_in_s"])
            await srv.close()

            self.relay._limits = self.handoff.Limits(os.path.join(self.tmp, "limits-silent.json"), self.relay.today)
            srv, _seen = await self._fake("silent")
            self._seed_hook(str(srv.make_url("/hook")))
            self.relay.REPLY_WAIT = 0.6
            r = await self.client.post("/admin/bots/jules/test", headers=h, json={})
            tid = (await r.json())["test_id"]
            t0 = time.monotonic()
            body = await self._poll(tid, timeout=3)
            self.assertEqual(body["state"], "failed")
            self.assertEqual(body["error"], "No answer after 2 min")
            self.assertLess(time.monotonic() - t0, 2.5)
            await srv.close()

            self.relay._limits = self.handoff.Limits(os.path.join(self.tmp, "limits-cancel.json"), self.relay.today)
            srv, _seen = await self._fake("silent")
            self._seed_hook(str(srv.make_url("/hook")))
            self.relay.REPLY_WAIT = 30
            r = await self.client.post("/admin/bots/jules/test", headers=h, json={})
            tid = (await r.json())["test_id"]
            d = await self.client.delete(f"/admin/bots/jules/test/{tid}", headers=h)
            self.assertEqual(await d.json(), {"ok": True})
            body = await (await self.client.get(f"/admin/bots/jules/test/{tid}", headers=h)).json()
            self.assertEqual(body["state"], "cancelled")
            self.assertIsNone(body["error"])
            await asyncio.sleep(0.3)
            pending = os.listdir(self.relay.PENDING_DIR) if os.path.isdir(self.relay.PENDING_DIR) else []
            self.assertEqual(pending, [])
            await srv.close()

            self.relay._limits = self.handoff.Limits(os.path.join(self.tmp, "limits-cap.json"), self.relay.today,
                                                       per_bot_day=8)
            json.dump({"day": self.relay.today(), "bots": {"jules": {"n": 8, "last": 0}}, "total": 8, "out": {}},
                      open(self.relay._limits.path, "w"))
            srv, seen = await self._fake("reply")
            self._seed_hook(str(srv.make_url("/hook")))
            r = await self.client.post("/admin/bots/jules/test", headers=h, json={})
            self.assertEqual(r.status, 202)
            body = await self._poll((await r.json())["test_id"], timeout=2)
            self.assertEqual(body["state"], "failed")
            self.assertEqual(body["error"], "Daily limit reached")
            self.assertEqual(seen["bodies"], [])                              # the capped test never POSTed
            await srv.close()
        finally:
            self.relay.REPLY_WAIT = old_wait
        audit = open(self.relay.audit_path()).read()
        for secret in (KEY1, "denied-key-zzzz", "api2.cursor.sh", "127.0.0.1", self.relay.TEST_QUESTION):
            self.assertNotIn(secret, audit)
        self.assertNotIn(self.relay.TEST_QUESTION, "\n".join(self.lines))
        self.assertIn(" test ", audit)


if __name__ == "__main__":
    unittest.main()
