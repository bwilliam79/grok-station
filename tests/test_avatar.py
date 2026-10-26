"""POST /voice/avatar is a sealed picture. The voice-token upload is retired.

Owner upload (certificate + admin token) is unchanged. Import relay inside the tests so collection
does not bind its environment before the other suites.
"""
import asyncio, base64, hashlib, io, json, os, secrets, stat, sys, tempfile, unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from nacl.public import SealedBox
from nacl.signing import SigningKey
from PIL import Image, PngImagePlugin

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay"))


def _mods():
    import botreply
    import handoff
    import relay
    return botreply, handoff, relay


def _png(size=(80, 40), color=(10, 20, 30), text=None):
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    info = None
    if text:
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", text)
    im.save(buf, format="PNG", pnginfo=info)
    return buf.getvalue()


def _jpeg():
    """400x200: left green, centre red, right blue, with an EXIF description."""
    im = Image.new("RGB", (400, 200), (0, 0, 255))
    im.paste((0, 255, 0), (0, 0, 100, 200))
    im.paste((255, 0, 0), (100, 0, 300, 200))
    exif = im.getexif()
    exif[270] = "SECRET_EXIF_MARKER"
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif, quality=95)
    return buf.getvalue()


def _webp():
    im = Image.new("RGB", (60, 90), (7, 8, 9))
    buf = io.BytesIO()
    im.save(buf, format="WEBP")
    return buf.getvalue()


class Avatar(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        botreply, handoff, relay = _mods()
        self.botreply, self.handoff, self.relay = botreply, handoff, relay
        self.tmp = tempfile.mkdtemp()
        r = relay
        self.snap = {
            "WEBHOOKS_FILE": r.WEBHOOKS_FILE, "MINTED_FILE": r.MINTED_FILE, "PORTRAIT_WRITE": r.PORTRAIT_WRITE,
            "PORTRAIT_DIRS": list(r.PORTRAIT_DIRS), "AUDIT_MIRROR": r.AUDIT_MIRROR,
            "AUDIT_MIRROR_DIR": r.AUDIT_MIRROR_DIR, "PENDING_DIR": r.PENDING_DIR,
            "REPLIES_DIR": r.REPLIES_DIR, "PERSONAS_FILE": r.PERSONAS_FILE, "ADMIN_TOKEN": r.ADMIN_TOKEN,
            "REPLY_WAIT": r.REPLY_WAIT, "HANDOFF": r.HANDOFF, "log": r.log, "_limits": r._limits,
            "TOKENS": r.TOKENS, "version": r._portraits_version, "hits": dict(r._avatar_hits),
            "policy": dict(r._policy), "voices": set(r.VOICES), "SECRETS_DIR": r.SECRETS_DIR,
            "avatar_seen": r._avatar_seen,
            "personas": {k: (dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v))
                         for k, v in r._personas.items()},
            "hooks": {"mtime": r._hooks.get("mtime"), "map": dict(r._hooks.get("map") or {})},
            "runs": dict(r._runs),
        }
        r.WEBHOOKS_FILE = os.path.join(self.tmp, "webhooks.json")
        r.MINTED_FILE = os.path.join(self.tmp, "minted", "voice-tokens.json")
        r.PORTRAIT_WRITE = os.path.join(self.tmp, "portraits")
        r.PORTRAIT_DIRS = [os.path.join(self.tmp, "ro")]
        r.AUDIT_MIRROR = os.path.join(self.tmp, "voice-audit", "audit.log")
        r.AUDIT_MIRROR_DIR = ""  # legacy AUDIT_MIRROR path, not a preferred directory from the environment
        r.PENDING_DIR = os.path.join(self.tmp, "pending")
        r.REPLIES_DIR = os.path.join(self.tmp, "replies")
        r.PERSONAS_FILE = os.path.join(self.tmp, "personas.json")
        r.SECRETS_DIR = os.path.join(self.tmp, "secrets")
        os.makedirs(r.PORTRAIT_WRITE)
        os.makedirs(r.PORTRAIT_DIRS[0])
        os.makedirs(os.path.dirname(r.AUDIT_MIRROR))
        os.makedirs(os.path.dirname(r.MINTED_FILE))
        os.makedirs(r.SECRETS_DIR)
        os.makedirs(r.PENDING_DIR)
        r.ADMIN_TOKEN = b"admin-secret"
        r.TOKENS = [b"test-token"]
        r.HANDOFF = True
        r.REPLY_WAIT = 2
        r._avatar_hits.clear()
        r._avatar_seen = botreply.ShowSeen(os.path.join(self.tmp, "avatar-seen.json"))
        self.station = botreply.load_or_create_station(r.SECRETS_DIR)
        self.signer = SigningKey.generate()
        self.assertTrue(botreply.pin_key(
            r.bot_keys_path(), "jules", base64.b64encode(bytes(self.signer.verify_key)).decode()))
        r._limits = handoff.Limits(os.path.join(self.tmp, "limits.json"), r.today)
        r._policy.update(mtime=None, pii=True)
        r.VOICES.clear()
        r.VOICES.update({"ara", "luna"})
        r._personas.update(mtime=None, map={}, voices=None, hosts=[], names=[])
        r._hooks.update(mtime=None, map={})
        r.log = lambda *_a: None
        self.token = "vt_voice_token_test_value"
        digest = hashlib.sha256(self.token.encode()).hexdigest()
        with open(r.MINTED_FILE, "w") as f:
            json.dump({"agents": {"jules": {"hashes": [digest], "maxTier": 0, "updatedAt": 1}}}, f)
        self.decoy = b"DECOY-REGISTRY-BYTES"
        with open(os.path.join(r.PORTRAIT_DIRS[0], "jules.png"), "wb") as f:
            f.write(self.decoy)
        with open(r.PERSONAS_FILE, "w") as f:
            json.dump({"personas": [{"slug": "jules", "name": "Jules", "voice": "ara", "portrait": "jules.png",
                                     "prompt": "The notes helper."}]}, f)
        app = web.Application(client_max_size=8 << 20)
        app.add_routes([
            web.post("/voice/avatar", r.voice_avatar),
            web.get("/portrait/{slug}", r.portrait_route),
            web.get("/personas", r.personas_route),
            web.post("/admin/bots/{slug}/avatar", r.admin_avatar_post),
            web.post("/admin/bots/{slug}/avatar-request", r.admin_avatar_request),
        ])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        r = self.relay
        for t in list(r._run_tasks):
            t.cancel()
        if r._run_tasks:
            await asyncio.gather(*list(r._run_tasks), return_exceptions=True)
        r._runs.clear()
        if getattr(self, "client", None) is not None:
            await self.client.close()
        s = self.snap
        r.WEBHOOKS_FILE, r.MINTED_FILE = s["WEBHOOKS_FILE"], s["MINTED_FILE"]
        r.PORTRAIT_WRITE, r.PORTRAIT_DIRS = s["PORTRAIT_WRITE"], s["PORTRAIT_DIRS"]
        r.AUDIT_MIRROR, r.AUDIT_MIRROR_DIR = s["AUDIT_MIRROR"], s["AUDIT_MIRROR_DIR"]
        r.PENDING_DIR, r.REPLIES_DIR = s["PENDING_DIR"], s["REPLIES_DIR"]
        r.PERSONAS_FILE, r.ADMIN_TOKEN = s["PERSONAS_FILE"], s["ADMIN_TOKEN"]
        r.REPLY_WAIT, r.HANDOFF, r.log, r._limits = s["REPLY_WAIT"], s["HANDOFF"], s["log"], s["_limits"]
        r.TOKENS = s["TOKENS"]
        r.SECRETS_DIR = s["SECRETS_DIR"]
        r._avatar_seen = s["avatar_seen"]
        r._portraits_version = s["version"]
        r._avatar_hits.clear()
        r._avatar_hits.update(s["hits"])
        r._policy.clear()
        r._policy.update(s["policy"])
        r.VOICES.clear()
        r.VOICES.update(s["voices"])
        r._personas.clear()
        r._personas.update(s["personas"])
        r._hooks.clear()
        r._hooks.update(s["hooks"])

    def admin(self, ctype="image/png"):
        h = {"Authorization": "Bearer admin-secret", "X-Client-Cert-Fp": "ab" * 32}
        if ctype:
            h["Content-Type"] = ctype
        return h

    def _saved(self):
        return os.path.join(self.relay.PORTRAIT_WRITE, "jules.png")

    def _seal(self, image, ctype, slug="jules"):
        doc = {"content_type": ctype, "exp_ms": int(__import__("time").time() * 1000) + 60_000,
               "image_b64": base64.b64encode(image).decode("ascii"), "kind": "avatar",
               "nonce": secrets.token_urlsafe(16)}
        signed = self.signer.sign(json.dumps(doc, separators=(",", ":"), sort_keys=True).encode())
        sealed = SealedBox(self.station.public_key).encrypt(signed)
        outer = {"v": 1, "bot": slug, "sealed": base64.b64encode(sealed).decode("ascii")}
        return json.dumps(outer, separators=(",", ":")).encode()

    async def _post(self, image, ctype="image/png", slug="jules"):
        body = self._seal(image, ctype, slug=slug)
        return await self.client.post("/voice/avatar", data=body, headers={"Content-Type": "application/json"})

    async def _post_body(self, body, headers=None):
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        return await self.client.post("/voice/avatar", data=body, headers=h)

    def _png_ok(self, body, marker=b"SECRET_PNG_TEXT"):
        self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertNotIn(marker, body)
        self.assertNotIn(b"SECRET_EXIF_MARKER", body)
        im = Image.open(io.BytesIO(body))
        self.assertEqual(im.size, (512, 512))
        self.assertNotIn("SECRET_EXIF_MARKER", " ".join(map(str, im.getexif().values())))
        return im

    async def test_png_jpeg_webp_become_512_without_metadata(self):
        marked = _png(text="SECRET_PNG_TEXT")
        self.assertIn(b"SECRET_PNG_TEXT", marked)
        sealed = self._seal(marked, "image/png")
        r = await self._post_body(sealed)
        self.assertEqual(r.status, 200, await r.text())
        doc = await r.json()
        body = open(self._saved(), "rb").read()
        self._png_ok(body)
        self.assertEqual(doc["ok"], True)
        self.assertEqual(doc["sha8"], hashlib.sha256(body).hexdigest()[:8])
        self.assertEqual(stat.S_IMODE(os.stat(self._saved()).st_mode), 0o644)
        audit = open(self.relay.audit_path()).read()
        self.assertIn(f"bot jules avatar-upload {doc['sha8']}", audit)
        self.assertNotIn(self.token, audit)
        self.assertIn(f"avatar-upload {doc['sha8']}", open(self.relay.audit_mirror_path()).read())
        got = await self.client.get("/portrait/jules", headers={"Authorization": "Bearer test-token"})
        self.assertEqual(got.status, 200)
        self.assertEqual(await got.read(), body)
        self.assertNotEqual(body, self.decoy)
        listed = await (await self.client.get("/personas", headers={"Authorization": "Bearer test-token"})).json()
        self.assertGreaterEqual(listed["portraits_version"], 1)
        again = await self._post_body(sealed)
        self.assertEqual(again.status, 409, await again.text())

        jpeg = _jpeg()
        src = Image.open(io.BytesIO(jpeg))
        self.assertIn("SECRET_EXIF_MARKER", str(src.getexif()))
        self.relay._avatar_hits.clear()
        r = await self._post(jpeg, "image/jpeg")
        self.assertEqual(r.status, 200, await r.text())
        im = self._png_ok(open(self._saved(), "rb").read())
        px = im.getpixel((256, 256))
        self.assertGreater(px[0], 240)
        self.assertLess(px[1], 20)
        self.assertLess(px[2], 20)

        self.relay._avatar_hits.clear()
        r = await self._post(_webp(), "image/webp")
        self.assertEqual(r.status, 200, await r.text())
        self._png_ok(open(self._saved(), "rb").read())

    async def test_retired_route_and_picture_limits(self):
        png = _png()
        retired = await self.client.post(
            "/voice/avatar", data=png,
            headers={"Authorization": "Bearer " + self.token, "X-Grok-Persona": "jules", "Content-Type": "image/png"})
        self.assertEqual(retired.status, 410)
        self.assertEqual((await retired.json())["error"], "retired")
        self.assertFalse(os.path.exists(self._saved()))
        leftover = await self.client.post(
            "/voice/avatar", json={"image_b64": base64.b64encode(png).decode()},
            headers={"Authorization": "Bearer " + self.token, "X-Grok-Persona": "jules"})
        self.assertNotEqual(leftover.status, 410)
        self.assertEqual(leftover.status, 400)
        self.assertEqual((await self._post(png, slug="hank")).status, 403)
        raw = await self.client.post("/voice/avatar", data=png, headers={"Content-Type": "image/png"})
        self.assertEqual(raw.status, 400)
        self.assertNotEqual(raw.status, 401)

        self.relay._avatar_hits.clear()
        huge = b"\x89PNG\r\n\x1a\n" + b"x" * self.relay.AVATAR_MAX
        self.assertGreater(len(huge), self.botreply.AVATAR_IMAGE_MAX)
        self.assertEqual((await self._post(huge)).status, 413)
        self.assertEqual(self.relay._avatar_hits.get("jules", []), [])

        outer = b"{" + b"x" * (self.botreply.AVATAR_OUTER_MAX + 1)
        too_big = await self._post_body(outer)
        self.assertEqual(too_big.status, 413)

        lied = self._seal(b"hello", "image/png")
        self.assertEqual((await self._post_body(lied)).status, 415)
        self.assertEqual((await self._post_body(lied)).status, 415)  # nonce released, not a replay

        self.relay._avatar_hits.clear()
        wide = io.BytesIO()
        Image.new("RGB", (2100, 2100), (4, 5, 6)).save(wide, format="PNG")
        blob = wide.getvalue()
        self.assertLess(len(blob), self.relay.AVATAR_MAX)
        sealed_wide = self._seal(blob, "image/png")
        self.assertEqual((await self._post_body(sealed_wide)).status, 413)
        self.assertEqual((await self._post_body(sealed_wide)).status, 413)
        self.assertEqual(len(self.relay._avatar_hits.get("jules", [])), 2)

        self.relay._avatar_hits.clear()
        for i in range(6):
            got = await self._post(_png(color=(i, 0, 0)))
            self.assertEqual(got.status, 200, await got.text())
        self.assertEqual((await self._post(png)).status, 429)
        # The owner's upload is not the bot's 6/hour.
        owner = await self.client.post("/admin/bots/jules/avatar", data=png, headers=self.admin())
        self.assertEqual(owner.status, 200, await owner.text())

    async def test_owner_upload_and_avatar_request(self):
        r = self.relay
        before = (await (await self.client.get("/personas", headers={"Authorization": "Bearer test-token"})).json())["portraits_version"]
        raw = _png(text="SECRET_PNG_TEXT")
        got = await self.client.post("/admin/bots/jules/avatar", data=raw, headers=self.admin())
        self.assertEqual(got.status, 200, await got.text())
        doc = await got.json()
        body = open(self._saved(), "rb").read()
        self._png_ok(body)
        self.assertEqual(doc["sha8"], hashlib.sha256(body).hexdigest()[:8])
        self.assertIn(f"admin jules avatar-upload {doc['sha8']}", open(r.audit_path()).read())
        self.assertNotIn(self.token, open(r.audit_path()).read())
        after = (await (await self.client.get("/personas", headers={"Authorization": "Bearer test-token"})).json())["portraits_version"]
        self.assertEqual(after, before + 1)
        bad_actor = await self.client.post("/admin/bots/jules/avatar", data=raw,
                                          headers={**self.admin(), "X-Actor": "has space"})
        self.assertEqual(bad_actor.status, 400)

        missing = await self.client.post("/admin/bots/jules/avatar-request", headers=self.admin("application/json"),
                                        json={"actor": "admin"})
        self.assertEqual(missing.status, 400)
        self.assertEqual((await missing.json())["error"], "not configured")
        unknown = await self.client.post("/admin/bots/hank/avatar-request", headers=self.admin("application/json"),
                                        json={"actor": "admin"})
        self.assertEqual(unknown.status, 404)
        spaced = await self.client.post("/admin/bots/jules/avatar-request", headers=self.admin("application/json"),
                                       json={"actor": "has space"})
        self.assertEqual(spaced.status, 400)

        seen = {}

        async def hook(req):
            seen["auth"] = req.headers.get("Authorization", "")
            seen["body"] = await req.json()
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/hook", hook)
        srv = TestServer(app)
        await srv.start_server()
        limits = r._limits.path
        try:
            self.assertFalse(os.path.exists(limits))
            self.assertTrue(r.write_webhooks({"jules": {"url": str(srv.make_url("/hook")), "token": "hook-token-aaaa",
                                                       "saved_at": 1}}))
            req = await self.client.post("/admin/bots/jules/avatar-request", headers=self.admin("application/json"),
                                        json={"actor": "admin"})
            self.assertEqual(req.status, 202, await req.text())
            self.assertEqual(await req.json(), {"ok": True})
            self.assertEqual(set(seen["body"]), {"v", "request_id", "persona", "question", "reply_by", "kind"})
            self.assertEqual(seen["body"]["kind"], "avatar")
            self.assertEqual(seen["body"]["v"], 1)
            self.assertEqual(seen["body"]["persona"], "jules")
            self.assertEqual(seen["body"]["question"], r.AVATAR_ASK)
            self.assertNotIn("test", seen["body"])
            self.assertNotIn("include_avatar", seen["body"])
            self.assertNotIn("pii_mode", seen["body"])
            self.assertEqual(seen["auth"], "Bearer hook-token-aaaa")
            self.assertEqual(os.listdir(r.PENDING_DIR), [])
            self.assertFalse(os.path.exists(limits))
            audit = open(r.audit_path()).read()
            self.assertIn("admin jules avatar-ask", audit)
            self.assertNotIn("hook-token-aaaa", audit)
            self.assertNotIn(str(srv.make_url("/hook")), audit)
        finally:
            await srv.close()

        async def down(_req):
            return web.json_response({"ok": False}, status=500)

        bad = web.Application()
        bad.router.add_post("/hook", down)
        bad_srv = TestServer(bad)
        await bad_srv.start_server()
        try:
            self.assertTrue(r.write_webhooks({"jules": {"url": str(bad_srv.make_url("/hook")), "token": "hook-token-bbbb",
                                                       "saved_at": 2}}))
            refused = await self.client.post("/admin/bots/jules/avatar-request", headers=self.admin("application/json"),
                                            json={"actor": "admin"})
            self.assertEqual(refused.status, 502, await refused.text())
            self.assertNotIn("hook-token-bbbb", open(r.audit_path()).read())
        finally:
            await bad_srv.close()

        self.assertTrue(r.write_webhooks({"jules": {"url": "http://127.0.0.1:9/hook", "token": "hook-token-cccc",
                                                   "saved_at": 3}}))
        closed = await self.client.post("/admin/bots/jules/avatar-request", headers=self.admin("application/json"),
                                       json={"actor": "admin"})
        self.assertEqual(closed.status, 502, await closed.text())
        self.assertNotIn("hook-token-cccc", open(r.audit_path()).read())
