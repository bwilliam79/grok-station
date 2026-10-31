"""A bot's reported voice seeds the station once. Later reports do not change a saved voice.

Real relay handlers (aiohttp TestClient on 127.0.0.1) and the seal commands. No relay import at collection.
"""
import asyncio, base64, importlib.machinery, importlib.util, io, json, os, secrets, sys, tempfile, time, unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from nacl.public import PrivateKey, SealedBox
from nacl.signing import SigningKey
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(ROOT, "relay")
_MISSING = object()
_EXPECTED = {
    "carina", "zagan", "helix", "orion", "luna", "iris", "altair", "zenith",
    "perseus", "helios", "lux", "kepler", "rigel", "cosmo", "celeste", "ursa",
    "sirius", "lumen", "castor", "naksh", "atlas", "aurora", "liora",
    "ara", "eve", "leo", "rex", "sal",
}
_VOICE_SENTENCE = ("Read voiceId from your own Grok Bot settings.json "
                   "(your own agent's settings file, never another bot's). "
                   "If it is present, add --voice <voiceId> to the command below. "
                   "If it is not, send that command without --voice.")


def _mods():
    if RELAY not in sys.path:
        sys.path.insert(0, RELAY)
    import botreply
    import handoff
    import relay
    import voices
    return botreply, handoff, relay, voices


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (20, 40, 60)).save(buf, format="PNG")
    return buf.getvalue()


def _load_named(name, path, patch_lstat=False):
    """Load a script by path. gs-seal-reply replaces sys.path while it imports, so that is restored."""
    saved_path = list(sys.path)
    saved_dont = sys.dont_write_bytecode
    saved_lstat = os.lstat
    if patch_lstat:
        tests = os.path.dirname(os.path.abspath(__file__))
        if tests not in sys.path:
            sys.path.insert(0, tests)
        import test_gs_seal_reply as harness
        os.lstat = harness._lstat_as_root_on_stdlib
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        loader.exec_module(mod)
    finally:
        os.lstat = saved_lstat
        sys.path[:] = saved_path
        sys.dont_write_bytecode = saved_dont
    return mod


class MapReportedVoice(unittest.TestCase):
    def setUp(self):
        botreply, handoff, relay, voices = _mods()
        self.voices, self.relay = voices, relay

    def test_table_is_the_documented_xai_ids(self):
        self.assertEqual(self.voices.XAI_VOICES, frozenset(_EXPECTED))
        self.assertEqual(len(self.voices.XAI_VOICES), 28)
        self.assertEqual(self.voices.ALIASES, {})
        self.assertEqual(self.relay.BUILTIN_VOICES, tuple(sorted(self.voices.XAI_VOICES)))

    def test_every_id_maps_to_itself_ignoring_case(self):
        for vid in sorted(self.voices.XAI_VOICES):
            self.assertEqual(self.voices.map_reported_voice(vid), vid)
            self.assertEqual(self.voices.map_reported_voice("  " + vid.upper() + "\n"), vid)
            self.assertEqual(self.voices.map_reported_voice(vid.title()), vid)

    def test_unknown_garbage_and_oversized_are_none(self):
        m = self.voices.map_reported_voice
        for raw in ("", "   ", None, 3, "robot9", "9abc", "a" * 33, "a" + "b" * 32, "ara_2", "ara 2", "-ara", "not a voice"):
            self.assertIsNone(m(raw), raw)
        self.assertEqual(m("a" * 32), None)  # shape matches, name is not an xAI voice
        self.assertIsNone(m("a" * 32, ["a" * 32]))

    def test_offered_list_is_respected_and_a_string_is_not_a_list(self):
        m = self.voices.map_reported_voice
        self.assertEqual(m("eve"), "eve")
        self.assertEqual(m("eve", None), "eve")
        self.assertEqual(m("eve", []), "eve")
        self.assertEqual(m("eve", set()), "eve")
        self.assertEqual(m("eve", "nope"), "eve")  # a string is not walked as voice ids
        self.assertEqual(m("eve", ["Eve"]), "eve")
        self.assertEqual(m("Castor", {"CASTOR"}), "castor")
        self.assertIsNone(m("eve", ["luna"]))
        self.assertIsNone(m("eve", ["luna", "not-a-real-voice"]))

    def test_alias_is_applied_before_the_table(self):
        self.voices.ALIASES["oldname"] = "ara"
        try:
            self.assertEqual(self.voices.map_reported_voice("OldName"), "ara")
            self.assertIsNone(self.voices.map_reported_voice("OldName", ["eve"]))
        finally:
            self.voices.ALIASES.clear()


class ReportedVoiceFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        botreply, handoff, relay, voices = _mods()
        self.botreply, self.handoff, self.relay, self.voices = botreply, handoff, relay, voices
        r = relay
        self.tmp = tempfile.mkdtemp()
        self.snap = {
            "WEBHOOKS_FILE": r.WEBHOOKS_FILE, "MINTED_FILE": r.MINTED_FILE, "PORTRAIT_WRITE": r.PORTRAIT_WRITE,
            "PORTRAIT_DIRS": list(r.PORTRAIT_DIRS), "AUDIT_MIRROR": r.AUDIT_MIRROR,
            "AUDIT_MIRROR_DIR": r.AUDIT_MIRROR_DIR, "PENDING_DIR": r.PENDING_DIR,
            "REPLIES_DIR": r.REPLIES_DIR, "PERSONAS_FILE": r.PERSONAS_FILE, "ADMIN_TOKEN": r.ADMIN_TOKEN,
            "REPLY_WAIT": r.REPLY_WAIT, "HANDOFF": r.HANDOFF, "log": r.log, "_limits": r._limits,
            "TOKENS": r.TOKENS, "version": r._portraits_version, "hits": dict(r._avatar_hits),
            "policy": dict(r._policy), "voices": set(r.VOICES), "SECRETS_DIR": r.SECRETS_DIR,
            "avatar_seen": r._avatar_seen, "ACK_DIR": r.ACK_DIR, "HELPER_FILE": r.HELPER_FILE,
            "UPDATES_FILE": r.UPDATES_FILE, "fetch_at": r._voice_fetch_at, "rtt": r._rtt, "rtt_dir": r._rtt_dir,
            "fails": dict(r.fails), "locked": dict(r.locked),
            "personas": {k: (dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v))
                         for k, v in r._personas.items()},
            "hooks": {"mtime": r._hooks.get("mtime"), "map": dict(r._hooks.get("map") or {})},
        }
        r.WEBHOOKS_FILE = os.path.join(self.tmp, "webhooks.json")
        r.MINTED_FILE = os.path.join(self.tmp, "minted", "voice-tokens.json")
        r.PORTRAIT_WRITE = os.path.join(self.tmp, "portraits")
        r.PORTRAIT_DIRS = [os.path.join(self.tmp, "ro")]
        r.AUDIT_MIRROR = os.path.join(self.tmp, "voice-audit", "audit.log")
        r.AUDIT_MIRROR_DIR = ""
        r.PENDING_DIR = os.path.join(self.tmp, "pending")
        r.REPLIES_DIR = os.path.join(self.tmp, "replies")
        r.PERSONAS_FILE = os.path.join(self.tmp, "personas.json")
        r.SECRETS_DIR = os.path.join(self.tmp, "secrets")
        r.ACK_DIR = os.path.join(self.tmp, "ack")
        r.HELPER_FILE = os.path.join(self.tmp, "helpers.json")
        r.UPDATES_FILE = os.path.join(self.tmp, "updates.json")
        for path in (r.PORTRAIT_WRITE, r.PORTRAIT_DIRS[0], os.path.dirname(r.AUDIT_MIRROR),
                     os.path.dirname(r.MINTED_FILE), r.SECRETS_DIR, r.PENDING_DIR, r.ACK_DIR):
            os.makedirs(path, exist_ok=True)
        r.ADMIN_TOKEN = b"admin-secret"
        r.TOKENS = [b"test-token"]
        r.HANDOFF = True
        r.REPLY_WAIT = 2
        r._avatar_hits.clear()
        r._avatar_seen = botreply.ShowSeen(os.path.join(self.tmp, "avatar-seen.json"))
        r._limits = handoff.Limits(os.path.join(self.tmp, "limits.json"), r.today)
        r._policy.update(mtime=None, pii=True)
        r.VOICES.clear()
        r._voice_fetch_at = time.monotonic()
        r._rtt = None
        r._rtt_dir = None
        r.fails.clear()
        r.locked.clear()
        r._personas.update(mtime=None, map={}, voices=None, hosts=[], names=[])
        r._hooks.update(mtime=None, map={})
        r._runs.clear()
        r._helper_reset_state()
        r.log = lambda *_a: None
        self.station = botreply.load_or_create_station(r.SECRETS_DIR)
        self.signers = {}
        self.png = _png()
        with open(r.PERSONAS_FILE, "w") as fh:
            json.dump({"personas": []}, fh)
        app = web.Application()
        app.add_routes(r.ADMIN_ROUTES)
        app.add_routes([web.post("/voice/avatar", r.voice_avatar)])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        r = self.relay
        loop = asyncio.get_running_loop()
        for t in list(r._run_tasks):
            t.cancel()
            if t.get_loop() is not loop:  # left by another test file's loop; it can't be awaited here
                r._run_tasks.discard(t)
        if r._run_tasks:
            await asyncio.gather(*list(r._run_tasks), return_exceptions=True)
        r._runs.clear()
        if getattr(self, "client", None) is not None:
            await self.client.close()
        r._helper_reset_state()
        s = self.snap
        r.WEBHOOKS_FILE, r.MINTED_FILE = s["WEBHOOKS_FILE"], s["MINTED_FILE"]
        r.PORTRAIT_WRITE, r.PORTRAIT_DIRS = s["PORTRAIT_WRITE"], s["PORTRAIT_DIRS"]
        r.AUDIT_MIRROR, r.AUDIT_MIRROR_DIR = s["AUDIT_MIRROR"], s["AUDIT_MIRROR_DIR"]
        r.PENDING_DIR, r.REPLIES_DIR = s["PENDING_DIR"], s["REPLIES_DIR"]
        r.PERSONAS_FILE, r.ADMIN_TOKEN = s["PERSONAS_FILE"], s["ADMIN_TOKEN"]
        r.REPLY_WAIT, r.HANDOFF, r.log, r._limits = s["REPLY_WAIT"], s["HANDOFF"], s["log"], s["_limits"]
        r.TOKENS = s["TOKENS"]
        r.SECRETS_DIR = s["SECRETS_DIR"]
        r.ACK_DIR, r.HELPER_FILE, r.UPDATES_FILE = s["ACK_DIR"], s["HELPER_FILE"], s["UPDATES_FILE"]
        r._avatar_seen = s["avatar_seen"]
        r._portraits_version = s["version"]
        r._avatar_hits.clear()
        r._avatar_hits.update(s["hits"])
        r._policy.clear()
        r._policy.update(s["policy"])
        r.VOICES.clear()
        r.VOICES.update(s["voices"])
        r._voice_fetch_at = s["fetch_at"]
        r._rtt, r._rtt_dir = s["rtt"], s["rtt_dir"]
        r._personas.clear()
        r._personas.update(s["personas"])
        r._hooks.clear()
        r._hooks.update(s["hooks"])
        r.fails.clear()
        r.fails.update(s["fails"])
        r.locked.clear()
        r.locked.update(s["locked"])

    def headers(self):
        return {"Authorization": "Bearer admin-secret", "X-Client-Cert-Fp": "ab" * 32}

    def _pin(self, slug):
        signer = SigningKey.generate()
        self.signers[slug] = signer
        self.assertTrue(self.botreply.pin_key(
            self.relay.bot_keys_path(), slug, base64.b64encode(bytes(signer.verify_key)).decode()))

    async def _add(self, slug, name, voice=_MISSING):
        body = {"slug": slug, "name": name, "actor": "admin"}
        if voice is not _MISSING:
            body["voice"] = voice
        resp = await self.client.post("/admin/bots", headers=self.headers(), json=body)
        self.assertEqual(resp.status, 201, await resp.text())
        return resp

    def _seal(self, slug, voice=_MISSING, extra=None):
        doc = {"content_type": "image/png", "exp_ms": int(time.time() * 1000) + 60_000,
               "image_b64": base64.b64encode(self.png).decode("ascii"), "kind": "avatar",
               "nonce": secrets.token_urlsafe(16)}
        if voice is not _MISSING:
            doc["voice"] = voice
        if extra:
            doc.update(extra)
        signed = self.signers[slug].sign(json.dumps(doc, separators=(",", ":"), sort_keys=True).encode())
        sealed = SealedBox(self.station.public_key).encrypt(signed)
        outer = {"v": 1, "bot": slug, "sealed": base64.b64encode(sealed).decode("ascii")}
        return json.dumps(outer, separators=(",", ":")).encode()

    async def _post(self, slug, voice=_MISSING, extra=None):
        body = self._seal(slug, voice=voice, extra=extra)
        return await self.client.post("/voice/avatar", data=body, headers={"Content-Type": "application/json"})

    def _added(self, slug):
        return self.relay.load_overlay_doc()["added"][slug]

    def _portrait(self, slug):
        return os.path.join(self.relay.PORTRAIT_WRITE, slug + ".png")

    def _actions(self, action):
        path = self.relay.audit_path()
        if not os.path.exists(path):
            return []
        found = []
        for ln in open(path):
            parts = ln.split()
            if len(parts) >= 5 and parts[3] == action:
                found.append(ln.rstrip("\n"))
        return found

    async def test_add_without_voice_uses_the_station_default(self):
        await self._add("max", "Max")
        ent = self._added("max")
        self.assertEqual(ent, {"name": "Max"})
        self.assertNotIn("voice", ent)
        self.assertEqual(self.relay.persona_list()["max"]["voice"], self.relay.VOICE.lower())
        self.assertEqual(self.relay._stored_voice("max"), "")
        blank = await self._add("pia", "Pia", voice="  ")
        self.assertEqual(blank.status, 201)
        self.assertNotIn("voice", self._added("pia"))
        self.assertEqual(self.relay.persona_list()["pia"]["voice"], self.relay.VOICE.lower())
        bad = await self.client.post("/admin/bots", headers=self.headers(),
                                     json={"slug": "quo", "name": "Quo", "voice": "robot9", "actor": "admin"})
        self.assertEqual(bad.status, 400)
        self.assertEqual(await bad.json(), {"error": "bad voice"})
        listed = await (await self.client.get("/admin/bots", headers=self.headers())).json()
        self.assertNotIn("not reported", json.dumps(listed).lower())
        row = next(b for b in listed["bots"] if b["slug"] == "max")
        self.assertEqual(row["voice"], self.relay.VOICE.lower())

    async def test_report_seeds_once_then_a_user_voice_sticks(self):
        await self._add("max", "Max")
        self._pin("max")
        self.assertEqual(self.relay._stored_voice("max"), "")
        first = await self._post("max", "Castor")
        self.assertEqual(first.status, 200, await first.text())
        body = await first.json()
        self.assertEqual(set(body), {"ok", "sha8"})
        self.assertNotIn("voice", body)
        self.assertTrue(open(self._portrait("max"), "rb").read().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(self._added("max")["voice"], "castor")
        self.assertEqual(self.relay.persona_list()["max"]["voice"], "castor")
        seeded = self._actions("voice-seeded")
        self.assertEqual(len(seeded), 1)
        self.assertIn(" bot max voice-seeded -", seeded[0])
        self.assertNotIn("castor", seeded[0])
        again = await self._post("max", "eve")
        self.assertEqual(again.status, 200, await again.text())
        self.assertEqual(self._added("max")["voice"], "castor")
        self.assertEqual(self._actions("voice-seeded"), seeded)
        changed = await self.client.patch("/admin/bots/max", headers=self.headers(),
                                          json={"voice": "rex", "actor": "admin"})
        self.assertEqual(changed.status, 200, await changed.text())
        self.assertEqual(self._added("max")["voice"], "rex")
        self.assertEqual(self.relay.load_overlay_doc()["voices"]["max"], "rex")
        later = await self._post("max", "eve")
        self.assertEqual(later.status, 200, await later.text())
        self.assertEqual(self._added("max")["voice"], "rex")
        self.assertEqual(self.relay.load_overlay_doc()["voices"]["max"], "rex")
        self.assertEqual(self._actions("voice-seeded"), seeded)
        self.assertTrue(any(ln.split()[3] == "voice" and " max " in ln for ln in self._actions("voice")))

    async def test_unsupported_voice_saves_the_picture_and_adds_no_label(self):
        await self._add("rio", "Rio")
        self._pin("rio")
        odd = await self._post("rio", 3)
        self.assertEqual(odd.status, 200, await odd.text())
        self.assertNotIn("voice", self._added("rio"))
        self.assertTrue(os.path.isfile(self._portrait("rio")))
        bad = await self._post("rio", "robot9")
        self.assertEqual(bad.status, 200, await bad.text())
        self.assertNotIn("voice", self._added("rio"))
        self.assertEqual(self.relay.persona_list()["rio"]["voice"], self.relay.VOICE.lower())
        self.assertEqual(self.relay._stored_voice("rio"), "")
        self.assertEqual(self._actions("voice-seeded"), [])
        listed = await (await self.client.get("/admin/bots", headers=self.headers())).json()
        dumped = json.dumps(listed).lower()
        self.assertNotIn("not reported", dumped)
        self.assertNotIn("robot9", dumped)

    async def test_live_voice_list_filters_the_seed(self):
        await self._add("ned", "Ned")
        self._pin("ned")
        self.relay.VOICES.clear()
        self.relay.VOICES.update({"ara", "luna"})
        self.relay._personas["mtime"] = None
        held = await self._post("ned", "Castor")
        self.assertEqual(held.status, 200, await held.text())
        self.assertNotIn("voice", self._added("ned"))
        self.assertTrue(os.path.isfile(self._portrait("ned")))
        self.relay.VOICES.clear()
        self.relay._personas["mtime"] = None
        taken = await self._post("ned", "eve")
        self.assertEqual(taken.status, 200, await taken.text())
        self.assertEqual(self._added("ned")["voice"], "eve")

    async def test_registry_bot_seeds_into_the_overlay_once(self):
        doc = {"personas": [{"slug": "noa", "name": "Noa", "prompt": "A household assistant.",
                             "aliases": [], "facts": []}]}
        raw = json.dumps(doc)
        with open(self.relay.PERSONAS_FILE, "w") as fh:
            fh.write(raw)
        self.relay._personas["mtime"] = None
        self._pin("noa")
        self.assertEqual(self.relay.persona_list()["noa"]["voice"], self.relay.VOICE.lower())
        self.assertEqual(self.relay._stored_voice("noa"), "")
        first = await self._post("noa", "Eve")
        self.assertEqual(first.status, 200, await first.text())
        ov = self.relay.load_overlay_doc()
        self.assertEqual(ov["voices"]["noa"], "eve")
        self.assertNotIn("noa", ov["added"])
        self.assertEqual(open(self.relay.PERSONAS_FILE).read(), raw)
        self.assertEqual(self.relay.persona_list()["noa"]["voice"], "eve")
        second = await self._post("noa", "sal")
        self.assertEqual(second.status, 200, await second.text())
        self.assertEqual(self.relay.load_overlay_doc()["voices"]["noa"], "eve")
        seeded = [ln for ln in self._actions("voice-seeded") if " noa " in ln]
        self.assertEqual(len(seeded), 1)
        self.assertNotIn("eve", seeded[0])

    async def test_blank_user_voice_is_saved_and_not_overwritten(self):
        await self._add("pia", "Pia")
        self._pin("pia")
        blank = await self.client.patch("/admin/bots/pia", headers=self.headers(),
                                        json={"voice": "  ", "actor": "admin"})
        self.assertEqual(blank.status, 200, await blank.text())
        self.assertEqual(self._added("pia")["voice"], self.relay.VOICE.lower())
        posted = await self._post("pia", "eve")
        self.assertEqual(posted.status, 200, await posted.text())
        self.assertEqual(self._added("pia")["voice"], self.relay.VOICE.lower())
        self.assertEqual(self._actions("voice-seeded"), [])

    async def test_unknown_field_is_still_rejected(self):
        await self._add("lea", "Lea")
        self._pin("lea")
        resp = await self._post("lea", "eve", extra={"note": "hello"})
        self.assertEqual(resp.status, 400)
        self.assertFalse(os.path.exists(self._portrait("lea")))
        self.assertNotIn("voice", self._added("lea"))
        self.assertEqual(self._actions("voice-seeded"), [])


class SealCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if RELAY not in sys.path:
            sys.path.insert(0, RELAY)
        import botreply
        cls.botreply = botreply
        cls.seal = _load_named("gs130_seal_reply", os.path.join(ROOT, "scripts", "gs-seal-reply"), patch_lstat=True)
        cls.tool = _load_named("gs130_reply_tool", os.path.join(ROOT, "tools", "grok-station-reply"))
        cls.png = _png()

    def _open(self, body, signer, station, slug="max"):
        keys = {"killed": False, "keys": {slug: base64.b64encode(bytes(signer.verify_key)).decode()}}
        return self.botreply.ingest_avatar(body, station, keys, {slug})

    def test_home_avatar_voice_is_sealed_and_invalid_is_omitted(self):
        """seal_avatar is the function cmd_avatar calls; main's platform checks are covered by the seal tests."""
        mod = self.seal
        signer = SigningKey.generate()
        station = PrivateKey.generate()
        mod.load_nacl = lambda: None  # PyNaCl is importable here; the vendor tree is not
        seal = lambda v: mod.seal_avatar("max", self.png, "image/png", bytearray(bytes(signer)),
                                         bytes(station.public_key), v)
        self.assertEqual(self._open(seal("Castor"), signer, station)["voice"], "castor")
        self.assertNotIn("voice", self._open(seal(None), signer, station))
        self.assertNotIn("voice", self._open(seal("!!!"), signer, station))
        self.assertEqual(self._open(seal("robot9"), signer, station)["voice"], "robot9")  # the station decides
        args = mod._parser().parse_args(["avatar", "--persona", "max", "--voice", "ara"])
        self.assertEqual(args.voice, "ara")
        with self.assertRaises(SystemExit):
            mod._parser().parse_args(["reply", "--persona", "max", "--request-id", "vs_" + "a" * 22, "--voice", "ara"])

    def test_cloud_seal_avatar_voice_flag(self):
        mod = self.tool
        signer = SigningKey.generate()
        station = PrivateKey.generate()
        pub = base64.b64encode(bytes(station.public_key)).decode()
        seed = base64.b64encode(bytes(signer)).decode()

        def run(argv, png):
            old = (sys.stdin, sys.stdout, sys.stderr, os.environ.get("GROK_STATION_BOT_KEY"))
            try:
                sys.stdin, sys.stdout, sys.stderr = io.BytesIO(png), io.StringIO(), io.StringIO()
                os.environ["GROK_STATION_BOT_KEY"] = seed
                rc = mod.main(argv)
                return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
            finally:
                sys.stdin, sys.stdout, sys.stderr, key = old
                if key is None:
                    os.environ.pop("GROK_STATION_BOT_KEY", None)
                else:
                    os.environ["GROK_STATION_BOT_KEY"] = key

        base = ["seal-avatar", "--bot", "max", "--station-pub", pub]
        rc, out, err = run(base + ["--voice", "Castor"], self.png)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._open(out.encode(), signer, station)["voice"], "castor")
        rc, out, err = run(base, self.png)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("voice", self._open(out.encode(), signer, station))
        rc, out, err = run(base + ["--voice", "!!!"], self.png)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("voice", self._open(out.encode(), signer, station))
        rc, out, err = run(base + ["--voice", "robot9"], self.png)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._open(out.encode(), signer, station)["voice"], "robot9")
        rc, out, err = run(["seal", "--bot", "max", "--station-pub", pub, "--request-id", "vs_" + "a" * 22, "--voice", "eve"], b"")
        self.assertNotEqual(rc, 0)


class InstructionTemplates(unittest.TestCase):
    def test_profile_paragraph_names_voice_in_every_copy(self):
        admin = os.path.join(ROOT, "admin")
        if admin not in sys.path:
            sys.path.insert(0, admin)
        import gs_routine
        home = open(os.path.join(admin, "routine-home.txt"), encoding="utf-8").read()
        cloud = open(os.path.join(admin, "routine-cloud.txt"), encoding="utf-8").read()
        self.assertEqual(gs_routine.TEMPLATES["home"], home)
        self.assertEqual(gs_routine.TEMPLATES["cloud"], cloud)
        sh = open(os.path.join(ROOT, "scripts", "gs-update.sh"), encoding="utf-8").read()
        embedded = sh.split("<<'GS_ROUTINE_PY'\n", 1)[1].split("\nGS_ROUTINE_PY\n", 1)[0] + "\n"
        self.assertEqual(embedded, open(os.path.join(admin, "gs_routine.py"), encoding="utf-8").read())
        js = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
        docs = open(os.path.join(ROOT, "docs", "bots.md"), encoding="utf-8").read()
        copies = {"routine-home.txt": home, "routine-cloud.txt": cloud,
                  "gs_routine home": gs_routine.TEMPLATES["home"], "gs_routine cloud": gs_routine.TEMPLATES["cloud"]}
        for name, fname in (("BOT_ROUTINE_HOME", "routine-home.txt"), ("BOT_ROUTINE", "routine-cloud.txt")):
            body = js.split("const %s = `" % name, 1)[1].split("`;", 1)[0]
            self.assertEqual(body, open(os.path.join(admin, fname), encoding="utf-8").read().rstrip("\n"), name)
            copies["app.js " + name] = body
        for label, text in copies.items():
            pic = text[text.index("Profile picture."):text.index("Station update.")]
            self.assertIn(_VOICE_SENTENCE, pic, label)
            self.assertIn("voiceId", pic, label)
            self.assertIn("--voice", pic, label)
            self.assertEqual(pic.count('"$HOME/.local/share/grok-station/avatar-<slug>.png"'), 2, label)
            self.assertLess(len(text), 12000, label)
            self.assertTrue(text.startswith("Grok Station instructions, release 0.13.13."), label)
            self.assertNotIn("not reported", text.lower(), label)
        self.assertIn(_VOICE_SENTENCE, sh)
        self.assertEqual(docs.count(_VOICE_SENTENCE), 2)
        self.assertEqual(docs.count("Grok Station instructions, release 0.13.12."), 2)
        self.assertIn("never replaced by a report", docs)  # 0.13.0: the add-bot picker stays; a report only seeds


if __name__ == "__main__":
    unittest.main()
