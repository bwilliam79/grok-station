"""POST /voice/avatar and the owner upload: decode, centre crop, 512 PNG, no metadata, caps.

Import relay inside the tests so collection does not bind its environment before the other suites.
"""
import asyncio, base64, hashlib, io, json, os, stat, sys, tempfile, unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image, PngImagePlugin

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay"))


def _mods():
    import handoff
    import relay
    return handoff, relay


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
        handoff, relay = _mods()
        self.handoff, self.relay = handoff, relay
        self.tmp = tempfile.mkdtemp()
        r = relay
        self.snap = {
            "WEBHOOKS_FILE": r.WEBHOOKS_FILE, "MINTED_FILE": r.MINTED_FILE, "PORTRAIT_WRITE": r.PORTRAIT_WRITE,
            "PORTRAIT_DIRS": list(r.PORTRAIT_DIRS), "AUDIT_MIRROR": r.AUDIT_MIRROR, "PENDING_DIR": r.PENDING_DIR,
            "REPLIES_DIR": r.REPLIES_DIR, "PERSONAS_FILE": r.PERSONAS_FILE, "ADMIN_TOKEN": r.ADMIN_TOKEN,
            "REPLY_WAIT": r.REPLY_WAIT, "HANDOFF": r.HANDOFF, "log": r.log, "_limits": r._limits,
            "TOKENS": r.TOKENS, "version": r._portraits_version, "hits": dict(r._avatar_hits),
            "policy": dict(r._policy), "voices": set(r.VOICES),
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
        r.PENDING_DIR = os.path.join(self.tmp, "pending")
        r.REPLIES_DIR = os.path.join(self.tmp, "replies")
        r.PERSONAS_FILE = os.path.join(self.tmp, "personas.json")
        os.makedirs(r.PORTRAIT_WRITE)
        os.makedirs(r.PORTRAIT_DIRS[0])
        os.makedirs(os.path.dirname(r.AUDIT_MIRROR))
        os.makedirs(os.path.dirname(r.MINTED_FILE))
        r.ADMIN_TOKEN = b"admin-secret"
        r.TOKENS = [b"test-token"]
        r.HANDOFF = True
        r.REPLY_WAIT = 2
        r._avatar_hits.clear()
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
        r.AUDIT_MIRROR, r.PENDING_DIR, r.REPLIES_DIR = s["AUDIT_MIRROR"], s["PENDING_DIR"], s["REPLIES_DIR"]
        r.PERSONAS_FILE, r.ADMIN_TOKEN = s["PERSONAS_FILE"], s["ADMIN_TOKEN"]
        r.REPLY_WAIT, r.HANDOFF, r.log, r._limits = s["REPLY_WAIT"], s["HANDOFF"], s["log"], s["_limits"]
        r.TOKENS = s["TOKENS"]
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

    def voice(self, token=None, slug="jules", ctype="image/png"):
        h = {"Authorization": "Bearer " + (self.token if token is None else token), "X-Grok-Persona": slug}
        if ctype:
            h["Content-Type"] = ctype
        return h

    def admin(self, ctype="image/png"):
        h = {"Authorization": "Bearer admin-secret", "X-Client-Cert-Fp": "ab" * 32}
        if ctype:
            h["Content-Type"] = ctype
        return h

    def _saved(self):
        return os.path.join(self.relay.PORTRAIT_WRITE, "jules.png")

    async def _post(self, raw, ctype="image/png", token=None, slug="jules"):
        return await self.client.post("/voice/avatar", data=raw, headers=self.voice(token, slug, ctype))

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
        r = await self._post(marked)
        self.assertEqual(r.status, 200)
        doc = await r.json()
        body = open(self._saved(), "rb").read()
        self._png_ok(body)
        self.assertEqual(doc["ok"], True)
        self.assertEqual(doc["sha8"], hashlib.sha256(body).hexdigest()[:8])
        self.assertEqual(stat.S_IMODE(os.stat(self._saved()).st_mode), 0o644)
        audit = open(self.relay.audit_path()).read()
        self.assertIn(f"bot jules avatar-upload {doc['sha8']}", audit)
        self.assertNotIn(self.token, audit)
        self.assertIn(f"avatar-upload {doc['sha8']}", open(self.relay.AUDIT_MIRROR).read())
        # registry decoy loses to the upload
        got = await self.client.get("/portrait/jules", headers={"Authorization": "Bearer test-token"})
        self.assertEqual(got.status, 200)
        self.assertEqual(await got.read(), body)
        self.assertNotEqual(body, self.decoy)
        listed = await (await self.client.get("/personas", headers={"Authorization": "Bearer test-token"})).json()
        self.assertGreaterEqual(listed["portraits_version"], 1)

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

        b64 = base64.b64encode(_png(color=(1, 2, 3))).decode()
        self.relay._avatar_hits.clear()
        r = await self.client.post("/voice/avatar", json={"image_b64": b64},
                                   headers=self.voice(ctype=None))
        self.assertEqual(r.status, 200, await r.text())
        self._png_ok(open(self._saved(), "rb").read())

    async def test_bad_token_oversize_non_image_and_rate_limit(self):
        png = _png()
        self.assertEqual((await self._post(png, token="not-the-token")).status, 401)
        self.assertEqual((await self._post(png, slug="hank")).status, 404)
        self.assertEqual((await self.client.post("/voice/avatar", data=png, headers={"Content-Type": "image/png"})).status, 401)
        missing = await self.client.post("/voice/avatar", data=png, headers=self.voice(slug="nope"))
        self.assertEqual(missing.status, 404)
        huge = b"\x89PNG\r\n\x1a\n" + b"x" * self.relay.AVATAR_MAX
        self.assertEqual((await self._post(huge)).status, 413)
        self.assertEqual((await self._post(b"not-an-image")).status, 415)
        plain = await self.client.post("/voice/avatar", data=b"hello",
                                      headers=self.voice(ctype="text/plain"))
        self.assertEqual(plain.status, 415)
        wide = io.BytesIO()
        Image.new("RGB", (2100, 2100), (4, 5, 6)).save(wide, format="PNG")
        self.assertLess(len(wide.getvalue()), self.relay.AVATAR_MAX)
        self.assertEqual((await self._post(wide.getvalue())).status, 413)
        self.relay._avatar_hits.clear()
        for i in range(6):
            got = await self._post(_png(color=(i, 0, 0)))
            self.assertEqual(got.status, 200, await got.text())
        self.assertEqual((await self._post(png)).status, 429)

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
        # no X-Actor was sent; actor defaults to admin. A bad actor is refused.
        bad_actor = await self.client.post("/admin/bots/jules/avatar", data=raw,
                                          headers={**self.admin(), "X-Actor": "has space"})
        self.assertEqual(bad_actor.status, 400)

        seen = {}

        async def hook(req):
            body = await req.json()
            pend = json.load(open(os.path.join(r.PENDING_DIR, body["request_id"] + ".json")))
            seen["body"], seen["pend"] = body, pend

            async def later():
                await asyncio.sleep(0.05)
                os.makedirs(r.REPLIES_DIR, exist_ok=True)
                json.dump({"request_id": body["request_id"], "agent": pend["persona"], "nonce": pend["nonce"],
                           "tier": 0, "action_class": "answer", "text": "ok"},
                          open(os.path.join(r.REPLIES_DIR, body["request_id"] + ".json"), "w"))

            asyncio.get_running_loop().create_task(later())
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/hook", hook)
        srv = TestServer(app)
        await srv.start_server()
        try:
            self.assertTrue(r.write_webhooks({"jules": {"url": str(srv.make_url("/hook")), "token": "hook-token-aaaa",
                                                       "saved_at": 1}}))
            req = await self.client.post("/admin/bots/jules/avatar-request", headers=self.admin("application/json"),
                                        json={"actor": "admin"})
            self.assertEqual(req.status, 202, await req.text())
            tid = (await req.json())["test_id"]
            deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < deadline:
                rec = r._runs.get(tid)
                if rec and rec["state"] != "waiting":
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(r._runs[tid]["state"], "ok")
            self.assertEqual(set(seen["body"]), {"source", "v", "request_id", "persona", "test", "include_avatar"})
            self.assertIs(seen["body"]["test"], True)
            self.assertIs(seen["body"]["include_avatar"], True)
            self.assertNotIn("question", seen["body"])
            self.assertNotIn("pii_mode", seen["body"])
            self.assertIs(seen["pend"]["test"], True)
            self.assertIn(r.TEST_QUESTION, seen["pend"]["question"])
            self.assertEqual(json.load(open(r._limits.path))["bots"]["jules"]["n"], 1)
            self.assertNotIn("hook-token-aaaa", open(r.audit_path()).read())
        finally:
            await srv.close()
