#!/usr/bin/env python3
"""Bots admin API on the relay: auth, webhook allowlist, persona overlay, minted voice token, and a capped test handoff.

Imported inside the tests so collecting this module does not initialize relay before the other suites set their env.
"""
import asyncio, base64, hashlib, importlib.machinery, importlib.util, json, os, stat, tempfile, time, unittest

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
            "SECRETS_DIR": r.SECRETS_DIR,
            "REPLIES_DIR": r.REPLIES_DIR, "PERSONAS_FILE": r.PERSONAS_FILE, "REPLY_WAIT": r.REPLY_WAIT,
            "ADMIN_TOKEN": r.ADMIN_TOKEN, "HANDOFF": r.HANDOFF, "_limits": r._limits, "log": r.log,
            "voices": set(r.VOICES),
            "personas": {k: (dict(v) if isinstance(v, dict) else v) for k, v in r._personas.items()},
            "hooks": {"mtime": r._hooks.get("mtime"), "map": dict(r._hooks.get("map") or {})},
            "fails": dict(r.fails), "locked": dict(r.locked),
        }
        r.SECRETS_DIR = os.path.join(self.tmp, "secrets")
        os.makedirs(r.SECRETS_DIR, exist_ok=True)
        r._reply_seen.clear()
        r._reply_rate = None
        r._rtt = None
        r._rtt_dir = None
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
        r.SECRETS_DIR = s["SECRETS_DIR"]
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
        got = json.loads(text)
        self.assertEqual(got, {"ok": True, "last4": "aaaa"})
        self.assertNotIn("reply_token", text)
        self.assertNotIn(KEY1, text)
        self.assertNotIn(GOOD, text)
        r = await self.client.put("/admin/bots/jules/webhook", headers=h, json={"url": GOOD_443, "key": KEY2, "actor": "admin"})
        text = await r.text()
        got = json.loads(text)
        self.assertEqual(got, {"ok": True, "last4": "bbbb"})
        self.assertNotIn("reply_token", text)
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

    async def test_rename_and_voice_drop_stale_asking_clips(self):
        r = self.relay
        old = r.ACK_DIR
        r.ACK_DIR = os.path.join(self.tmp, "ack")
        os.makedirs(r.ACK_DIR)
        try:
            h = self.headers()
            named = r.ack_cache_path("jules", "Jules", "ara")
            with open(named, "wb") as f:
                f.write(b"old-name")
            st = await self.client.patch("/admin/bots/jules", headers=h, json={"name": "Julie", "actor": "admin"})
            self.assertEqual(st.status, 200)
            self.assertFalse(os.path.exists(named))
            voiced = r.ack_cache_path("jules", "Julie", "ara")
            with open(voiced, "wb") as f:
                f.write(b"old-voice")
            st = await self.client.patch("/admin/bots/jules", headers=h, json={"voice": "rex", "actor": "admin"})
            self.assertEqual(st.status, 200)
            self.assertFalse(os.path.exists(voiced))
            self.assertEqual(r.persona_list()["jules"]["name"], "Julie")
            self.assertEqual(r.persona_list()["jules"]["voice"], "rex")
        finally:
            r.ACK_DIR = old

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
        blank = await self.client.patch("/admin/bots/nova", headers=h, json={"voice": "  ", "actor": "admin"})
        self.assertEqual(blank.status, 200)
        self.assertEqual(self.relay.persona_list()["nova"]["voice"], self.relay.VOICE.lower())

    async def test_empty_voices_use_builtin_add_default_and_offered_reload(self):
        r = self.relay
        h = self.headers()
        self.assertEqual(list(r.BUILTIN_VOICES), sorted(r.BUILTIN_VOICES))
        old_state, old_fetch, old_kick = r.STATE_DIR, r.fetch_voices, r._voice_fetch_at
        calls = []
        done = asyncio.Event()

        async def fake_fetch():
            calls.append(1)
            done.set()

        r.STATE_DIR = self.tmp
        r.fetch_voices = fake_fetch
        r._voice_fetch_at = 0.0
        try:
            r.VOICES.clear()
            r._personas["mtime"] = None
            body = await (await self.client.get("/admin/bots", headers=h)).json()
            self.assertEqual(body["voices"], list(r.BUILTIN_VOICES))
            self.assertEqual(body["default_voice"], r.VOICE)
            await asyncio.wait_for(done.wait(), 1)
            self.assertEqual(calls, [1])
            body = await (await self.client.get("/admin/bots", headers=h)).json()
            self.assertEqual(body["voices"], list(r.BUILTIN_VOICES))
            await asyncio.sleep(0)
            self.assertEqual(calls, [1])  # still inside the 60s gap
            r._voice_fetch_at = time.monotonic() - 61
            done.clear()
            await self.client.get("/admin/bots", headers=h)
            await asyncio.wait_for(done.wait(), 1)
            self.assertEqual(len(calls), 2)

            made = await self.client.post("/admin/bots", headers=h, json={"slug": "nova", "name": "Nova", "actor": "admin"})
            self.assertEqual(made.status, 201, await made.text())
            self.assertEqual(r.persona_list()["nova"]["voice"], r.VOICE.lower())
            blank = await self.client.post("/admin/bots", headers=h,
                                           json={"slug": "pip", "name": "Pip", "voice": "  ", "actor": "admin"})
            self.assertEqual(blank.status, 201, await blank.text())
            self.assertEqual(r.persona_list()["pip"]["voice"], r.VOICE.lower())
            bad = await self.client.post("/admin/bots", headers=h,
                                         json={"slug": "quo", "name": "Quo", "voice": "nope", "actor": "admin"})
            self.assertEqual(bad.status, 400)

            r.VOICES.clear()
            r._personas["mtime"] = None
            path = os.path.join(self.tmp, "voices", "offered.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump({"voices": ["ara", "customvoice"]}, f)
            self.assertTrue(r.seed_offered_voices())
            self.assertEqual(r.VOICES, {"ara", "customvoice"})
            listed = await (await self.client.get("/admin/bots", headers=h)).json()
            self.assertEqual(listed["voices"], ["ara", "customvoice"])
            self.assertEqual(len(calls), 2)  # live list is non-empty, so no kick
            r.VOICES.clear()
            with open(path, "w") as f:
                json.dump({"voices": ["ok", 3]}, f)
            self.assertFalse(r.seed_offered_voices())
            self.assertEqual(r.VOICES, set())
            with open(path, "w") as f:
                f.write("{")
            self.assertFalse(r.seed_offered_voices())
            self.assertEqual(r.VOICES, set())
        finally:
            r.STATE_DIR = old_state
            r.fetch_voices = old_fetch
            r._voice_fetch_at = old_kick
            r.VOICES.clear()
            r.VOICES.update({"ara", "rex", "luna"})
            r._personas["mtime"] = None

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

    def _helper(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "tools", "grok-station-reply")
        loader = importlib.machinery.SourceFileLoader("grok_station_reply", path)
        spec = importlib.util.spec_from_loader("grok_station_reply", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    async def test_sealed_reply_lands_and_the_request_route_is_gone(self):
        """A pinned key unseals one reply. GET /voice/request is not a route. Revoke and the kill switch reject."""
        import botreply
        from nacl.signing import SigningKey
        r = self.relay
        helper = self._helper()
        signer = SigningKey.generate()
        pub = base64.b64encode(bytes(signer.verify_key)).decode()
        seed = base64.b64encode(bytes(signer)).decode()
        put = await self.client.put("/admin/bots/jules/key", headers=self.headers(),
                                    json={"public_key": pub, "actor": "admin"})
        self.assertEqual(put.status, 200, await put.text())
        listed = await (await self.client.get("/admin/bots", headers=self.headers())).json()
        self.assertTrue(listed["bots"][0]["paired"])
        self.assertEqual(listed["bots"][0]["public_key"], pub)
        self.assertEqual(len(base64.b64decode(listed["station_pub"])), 32)
        self.assertFalse(listed["killed"])
        station = botreply.load_or_create_station(r.SECRETS_DIR)
        rid = self.handoff.new_request_id()
        self.handoff.write_pending(r.PENDING_DIR, rid, "jules", "n0nce", int(time.time() * 1000) + 60000,
                                   question="Is it raining?")
        self.assertEqual((await self.client.get("/voice/request/" + rid)).status, 404)
        raw = helper.build_outer("jules", rid, "No rain today.", seed, botreply.station_pub_b64(station))
        bad = await self.client.post("/voice/reply", data=raw[:-8] + b"xxxxxxxx", headers={"Content-Type": "application/json"})
        self.assertIn(bad.status, (400, 403))
        self.assertNotIn("No rain today.", await bad.text())
        p = await self.client.post("/voice/reply", data=raw, headers={"Content-Type": "application/json"})
        self.assertEqual(p.status, 200, await p.text())
        self.assertEqual(self.handoff.take_reply(r.REPLIES_DIR, rid, "jules", "n0nce"), ("No rain today.", None))
        again = await self.client.post("/voice/reply", data=raw, headers={"Content-Type": "application/json"})
        self.assertEqual(again.status, 409)
        audit = open(r.audit_path()).read()
        self.assertIn(" ok ", audit)
        self.assertNotIn("No rain today.", audit)
        self.assertNotIn(seed, audit)
        self.assertNotIn("Is it raining?", audit)
        gone = await self.client.delete("/admin/bots/jules/key", headers=self.headers(), json={"actor": "admin"})
        self.assertEqual(gone.status, 200)
        unpinned = await (await self.client.get("/admin/bots", headers=self.headers())).json()
        self.assertFalse(unpinned["bots"][0]["paired"])
        self.assertIsNone(unpinned["bots"][0]["public_key"])
        rid2 = self.handoff.new_request_id()
        self.handoff.write_pending(r.PENDING_DIR, rid2, "jules", "n2", int(time.time() * 1000) + 60000, question="q")
        raw2 = helper.build_outer("jules", rid2, "Later.", seed, botreply.station_pub_b64(station))
        blocked = await self.client.post("/voice/reply", data=raw2, headers={"Content-Type": "application/json"})
        self.assertEqual(blocked.status, 403)
        killed = await self.client.post("/admin/bots/revoke-all", headers=self.headers(), json={"actor": "admin"})
        self.assertEqual(killed.status, 200)
        listed = await (await self.client.get("/admin/bots", headers=self.headers())).json()
        self.assertTrue(listed["killed"])
        self.assertFalse(listed["bots"][0]["paired"])
        self.assertIsNone(listed["bots"][0]["public_key"])

    def test_admin_forwards_only_the_reply_path(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "admin", "grok_admin.py")).read()
        self.assertIn('if p.startswith("/voice/"):\n            return self.voice_ingress(method, p)', src)
        self.assertIn('method == "POST" and p == "/voice/reply"', src)
        self.assertNotIn("/voice/request", src)
        self.assertIn('if not lan_ok(self.client_address[0]):\n            return self.send(403', src)
        self.assertIn("never be port-forwarded", src)

    def test_handoff_on_by_default(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "relay", "relay.py")).read()
        self.assertIn('E("HANDOFF_ENABLED", "1")', src)

    def test_icon_orders_the_newest_request_across_bots(self):
        r = self.relay
        self.assertEqual(r.icon_from_status([], {}), {"paired": False, "ok": None})
        self.assertEqual(r.icon_from_status(["jules"], {}), {"paired": True, "ok": True})  # paired, no request yet
        doc = {"jules": {"icon": "timeout", "icon_at": 10}, "nova": {"icon": "ok", "icon_at": 20}}
        self.assertEqual(r.icon_from_status(["jules", "nova"], doc), {"paired": True, "ok": True})
        self.assertEqual(r.icon_from_status(["jules"], doc)["ok"], False)
        later = {"jules": {"icon": "ok", "icon_at": 20}, "nova": {"icon": "post-failed", "icon_at": 30}}
        self.assertEqual(r.icon_from_status(["jules", "nova"], later)["ok"], False)
        self.assertEqual(r.icon_view(), {"paired": False, "ok": None})  # nothing pinned in this tmp

    async def test_icon_tracks_post_failure_timeout_and_a_test_reply(self):
        """Paired idle is green. A webhook POST failure is red immediately. A verified reply (the Test
        relay ping) is green again and records RTT. A later timeout is red."""
        import botreply
        from nacl.signing import SigningKey
        r = self.relay
        signer = SigningKey.generate()
        pub = base64.b64encode(bytes(signer.verify_key)).decode()
        put = await self.client.put("/admin/bots/jules/key", headers=self.headers(),
                                    json={"public_key": pub, "actor": "admin"})
        self.assertEqual(put.status, 200, await put.text())
        self.assertEqual(r.icon_view(), {"paired": True, "ok": True})
        old_tokens = list(r.TOKENS)
        r.TOKENS = [b"station-token"]

        class Req:
            def __init__(self, token):
                self.headers = {"Authorization": token}

        try:
            bare = await r.health(Req(""))
            self.assertEqual(json.loads(bare.body), {"ok": True})
            authed = json.loads((await r.health(Req("Bearer station-token"))).body)
            self.assertEqual(authed["bots"], {"paired": True, "ok": True})
            self.assertNotIn("jules", json.dumps(authed["bots"]))
        finally:
            r.TOKENS = old_tokens

        h = self.headers()
        old_wait = r.REPLY_WAIT
        try:
            r.REPLY_WAIT = 2
            srv, _seen = await self._fake("deny")
            self._seed_hook(str(srv.make_url("/hook")), "denied-key-zzzz")
            r._limits = self.handoff.Limits(os.path.join(self.tmp, "limits-icon-401.json"), r.today)
            t0 = time.monotonic()
            resp = await self.client.post("/admin/bots/jules/test", headers=h, json={})
            tid = (await resp.json())["test_id"]
            body = await self._poll(tid, timeout=3)
            self.assertEqual(body["state"], "failed")
            self.assertLess(time.monotonic() - t0, 2.0)  # red on POST failure, not after REPLY_WAIT
            self.assertEqual(r.icon_view(), {"paired": True, "ok": False})
            await srv.close()

            srv, _seen = await self._fake("reply")
            self._seed_hook(str(srv.make_url("/hook")))
            r._limits = self.handoff.Limits(os.path.join(self.tmp, "limits-icon-ok.json"), r.today)
            resp = await self.client.post("/admin/bots/jules/test", headers=h, json={})
            tid = (await resp.json())["test_id"]
            body = await self._poll(tid)
            self.assertEqual(body["state"], "ok")
            self.assertIsInstance(body["answered_in_s"], float)
            self.assertEqual(r.icon_view()["ok"], True)
            listed = await (await self.client.get("/admin/bots", headers=h)).json()
            self.assertIsInstance(listed["bots"][0]["stats"]["avg_rtt_ms"], int)
            self.assertGreater(listed["bots"][0]["stats"]["avg_rtt_ms"], 0)
            self.assertIsInstance(listed["stats"]["avg_rtt_ms"], int)
            await srv.close()

            srv, _seen = await self._fake("silent")
            self._seed_hook(str(srv.make_url("/hook")))
            r._limits = self.handoff.Limits(os.path.join(self.tmp, "limits-icon-wait.json"), r.today)
            r.REPLY_WAIT = 0.4
            resp = await self.client.post("/admin/bots/jules/test", headers=h, json={})
            tid = (await resp.json())["test_id"]
            body = await self._poll(tid, timeout=3)
            self.assertEqual(body["error"], "No answer")
            self.assertEqual(r.icon_view(), {"paired": True, "ok": False})
            listed = await (await self.client.get("/admin/bots", headers=h)).json()
            self.assertGreaterEqual(listed["bots"][0]["stats"]["timeouts"], 1)
            await srv.close()
        finally:
            r.REPLY_WAIT = old_wait

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
            self.assertEqual(set(body0), {"v", "request_id", "persona", "question", "reply_by", "test"})
            self.assertEqual(body0["v"], 1)
            self.assertIs(body0["test"], True)
            self.assertEqual(body0["question"], self.relay.TEST_QUESTION)
            for dropped in ("source", "pii_mode", "tier_max", "include_avatar"):
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
            self.assertIsInstance(listed["bots"][0]["stats"]["avg_rtt_ms"], int)
            self.assertEqual(listed["bots"][0]["stats"]["timeouts"], 0)
            self.assertIsInstance(listed["stats"]["avg_rtt_ms"], int)
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
            self.assertEqual(body["error"], "No answer")
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

    async def test_key_mode_defaults_cloud_and_switching_revokes(self):
        """cloud is the default and is not stored. home is stored. A real switch revokes the pinned key."""
        import botreply
        from nacl.signing import SigningKey
        h = self.headers()
        listed = await (await self.client.get("/admin/bots", headers=h)).json()
        self.assertEqual(listed["bots"][0]["key_mode"], "cloud")
        voiced = await self.client.patch("/admin/bots/jules", headers=h, json={"voice": "rex", "actor": "admin"})
        self.assertEqual(voiced.status, 200, await voiced.text())
        self.assertNotIn(b"key_mode", open(self.relay.overlay_path(), "rb").read())
        bad = await self.client.post("/admin/bots", headers=h,
                                     json={"slug": "nova", "name": "Nova", "voice": "rex", "actor": "admin", "key_mode": "laptop"})
        self.assertEqual(bad.status, 400)
        self.assertEqual((await bad.json())["error"], "bad key mode")
        made = await self.client.post("/admin/bots", headers=h,
                                      json={"slug": "nova", "name": "Nova", "voice": "rex", "actor": "admin", "key_mode": "home"})
        self.assertEqual(made.status, 201, await made.text())
        disk = json.load(open(self.relay.overlay_path()))
        self.assertEqual(disk["key_mode"], {"nova": "home"})
        body = await (await self.client.get("/admin/bots", headers=h)).json()
        modes = {b["slug"]: b["key_mode"] for b in body["bots"]}
        self.assertEqual(modes["nova"], "home")
        self.assertEqual(modes["jules"], "cloud")
        signer = SigningKey.generate()
        pub = base64.b64encode(bytes(signer.verify_key)).decode()
        put = await self.client.put("/admin/bots/jules/key", headers=h, json={"public_key": pub, "actor": "admin"})
        self.assertEqual(put.status, 200, await put.text())
        same = await self.client.patch("/admin/bots/jules", headers=h, json={"key_mode": "cloud", "actor": "admin"})
        self.assertEqual(same.status, 200)
        self.assertEqual(await same.json(), {"ok": True})
        still = await (await self.client.get("/admin/bots", headers=h)).json()
        self.assertTrue(next(b["paired"] for b in still["bots"] if b["slug"] == "jules"))
        switched = await self.client.patch("/admin/bots/jules", headers=h, json={"key_mode": "home", "actor": "admin"})
        self.assertEqual(switched.status, 200)
        self.assertEqual(await switched.json(), {"ok": True, "needs_key": True})
        after = await (await self.client.get("/admin/bots", headers=h)).json()
        jules = next(b for b in after["bots"] if b["slug"] == "jules")
        self.assertEqual(jules["key_mode"], "home")
        self.assertFalse(jules["paired"])
        keys = botreply.load_keys(self.relay.bot_keys_path())
        self.assertNotIn("jules", keys.get("keys") or {})
        audit = self._audit()
        self.assertRegex(audit, r"admin jules key-mode -\n")
        self.assertNotIn(pub, audit)
        for line in audit.splitlines():
            if "key-mode" in line:
                self.assertTrue(line.endswith(" -"))
                self.assertNotIn("home", line)
        put = await self.client.put("/admin/bots/jules/key", headers=h, json={"public_key": pub, "actor": "admin"})
        self.assertEqual(put.status, 200, await put.text())
        again = await self.client.patch("/admin/bots/jules", headers=h, json={"key_mode": "home", "actor": "admin"})
        self.assertEqual(await again.json(), {"ok": True})
        mid = await (await self.client.get("/admin/bots", headers=h)).json()
        self.assertTrue(next(b["paired"] for b in mid["bots"] if b["slug"] == "jules"))
        back = await self.client.patch("/admin/bots/jules", headers=h, json={"key_mode": "cloud", "actor": "admin"})
        self.assertTrue((await back.json()).get("needs_key"))
        disk = json.load(open(self.relay.overlay_path()))
        self.assertNotIn("jules", disk.get("key_mode") or {})
        self.assertEqual(disk["key_mode"]["nova"], "home")
        gone = await self.client.delete("/admin/bots/nova", headers=h, json={"actor": "admin"})
        self.assertEqual(gone.status, 200)
        disk = json.load(open(self.relay.overlay_path()))
        self.assertNotIn("key_mode", disk)
        self.assertNotIn("nova", disk.get("added") or {})


    async def test_reply_host_is_a_label(self):
        """Which computer posts replies: stored, listed, changed without touching the key, gone with the bot."""
        h = self.headers()
        listed = await (await self.client.get("/admin/bots", headers=h)).json()
        self.assertIsNone(listed["bots"][0]["reply_host"])
        bad = await self.client.post("/admin/bots", headers=h, json={"slug": "nova", "name": "Nova", "voice": "rex",
                                                                    "actor": "admin", "reply_host": "<script>"})
        self.assertEqual((bad.status, (await bad.json())["error"]), (400, "bad computer name"))
        made = await self.client.post("/admin/bots", headers=h, json={"slug": "nova", "name": "Nova", "voice": "rex",
                                                                     "actor": "admin", "reply_host": "Den iMac"})
        self.assertEqual(made.status, 201, await made.text())
        self.assertEqual(json.load(open(self.relay.overlay_path()))["reply_host"], {"nova": "Den iMac"})
        moved = await self.client.patch("/admin/bots/jules", headers=h, json={"reply_host": "Office Mini", "actor": "admin"})
        self.assertEqual(await moved.json(), {"ok": True})  # no needs_key: the key is untouched
        body = await (await self.client.get("/admin/bots", headers=h)).json()
        hosts = {b["slug"]: b["reply_host"] for b in body["bots"]}
        self.assertEqual((hosts["jules"], hosts["nova"]), ("Office Mini", "Den iMac"))
        self.assertEqual(self.relay.reply_host_of("jules"), "Office Mini")
        self.assertRegex(self._audit(), r"admin jules reply-host -\n")
        cleared = await self.client.patch("/admin/bots/jules", headers=h, json={"reply_host": "", "actor": "admin"})
        self.assertEqual(cleared.status, 200)
        self.assertIsNone(self.relay.reply_host_of("jules"))
        await self.client.delete("/admin/bots/nova", headers=h, json={"actor": "admin"})
        self.assertNotIn("reply_host", json.load(open(self.relay.overlay_path())))

    def test_timeout_line_names_the_computer(self):
        self.assertEqual(self.relay.handoff_line("Jules", "timeout", "Den iMac"),
                         "I couldn't reach Jules. Den iMac may be offline.")
        self.assertEqual(self.relay.handoff_line("Jules", "timeout"), "I couldn't reach Jules.")
        self.assertEqual(self.relay.handoff_line("Jules", "reject", "Den iMac"), self.relay.handoff_line("Jules", "reject"))


if __name__ == "__main__":
    unittest.main()


class IconMissingStatusTests(__import__("unittest").TestCase):
    def test_paired_unreadable_status_green(self):
        import importlib, sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
        r = importlib.import_module("relay")
        self.assertEqual(r.icon_from_status(["jules"], None), {"paired": True, "ok": True})
        self.assertEqual(r.icon_from_status(["jules"], "garbage"), {"paired": True, "ok": True})
