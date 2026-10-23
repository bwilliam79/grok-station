#!/usr/bin/env python3
"""Bot list import: sealed list schema, merge store, one ask per minute, and the admin fetch."""
import asyncio, datetime, base64, importlib.machinery, importlib.util, io, json, os, stat, sys, tempfile, time, unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from nacl.public import PrivateKey, SealedBox
from nacl.signing import SigningKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import botreply  # noqa: E402
import handoff  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("grok_station_reply_list", os.path.join(ROOT, "tools", "grok-station-reply"))
_spec = importlib.util.spec_from_loader("grok_station_reply_list", _loader)
helper = importlib.util.module_from_spec(_spec)
_loader.exec_module(helper)

RID = "vs_" + "a" * 22
RID2 = "vs_" + "b" * 22
GOOD = "https://api2.cursor.sh/automations/webhook/list"


def _b64(raw):
    return base64.b64encode(raw).decode()


def _seal(station, signer, message, bot="sol"):
    signed = signer.sign(message if isinstance(message, (bytes, bytearray)) else message.encode())
    sealed = SealedBox(station.public_key).encrypt(signed)
    return json.dumps({"v": 1, "bot": bot, "sealed": _b64(sealed)}, separators=(",", ":")).encode()


def _list_message(names, rid=RID, **extra):
    body = {"action_class": "bot_list", "bots": [{"name": n} for n in names], "kind": "bot_list",
            "request_id": rid, "tier": 0}
    body.update(extra)
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()


def _answer_message(text="Tacos on Tuesday.", rid=RID):
    body = {"action_class": "answer", "request_id": rid, "text": text, "tier": 0}
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()


class IngestList(unittest.TestCase):
    def setUp(self):
        self.station = PrivateKey.generate()
        self.sol = SigningKey.generate()
        self.luna = SigningKey.generate()
        self.keys = {"killed": False, "keys": {"sol": _b64(bytes(self.sol.verify_key)),
                                               "luna": _b64(bytes(self.luna.verify_key))}}
        self.known = {"sol", "luna"}
        self.seen = set()
        self.pending = {"request_id": RID, "persona": "sol", "nonce": "n0", "expires_ms": int(time.time() * 1000) + 60000,
                        "pii_mode": True, "kind": "bot_list"}
        self.limiter = botreply.Limiter(6, 30)
        self.now = int(time.time() * 1000)

    def _go(self, raw, pending=None, seen=None, bot_known=None, source="sol", limiter=None, now_ms=None):
        looked = self.pending if pending is None else pending
        return botreply.ingest(raw, self.station, self.keys, self.known if bot_known is None else bot_known,
                               (lambda _rid: looked), self.seen if seen is None else seen,
                               self.limiter if limiter is None else limiter, now_ms=self.now if now_ms is None else now_ms,
                               source=source)

    def _list(self, names=("Kai", "Mira"), signer=None, bot="sol", **kw):
        raw = _seal(self.station, self.sol if signer is None else signer, _list_message(list(names), **kw.pop("msg", {})), bot=bot)
        return self._go(raw, **kw)

    def _verdict(self, raw, **kw):
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw, **kw)
        return cm.exception.verdict

    def test_clean_list_names(self):
        got = self._list()
        self.assertEqual(got["names"], ["Kai", "Mira"])
        self.assertEqual((got["kind"], got["action_class"], got["tier"], got["bot"]), ("bot_list", "bot_list", 0, "sol"))
        spaced = self._list(names=(" Kai ",))
        self.assertEqual(spaced["names"], ["Kai"])
        self.assertIsNone(botreply.list_name_clean("Kai\u200b"))
        self.assertIsNone(botreply.list_name_clean("Kai\n"))

    def test_schema_allowlist(self):
        # Schema rejects still pass the per-bot rate check, which sits before unseal. One list of
        # cases needs more than the default 6 tokens.
        self.limiter = botreply.Limiter(30, 60)
        raw = _seal(self.station, self.sol, _list_message(["Kai"], note="nope"))
        self.assertEqual(self._verdict(raw), "schema")
        raw = _seal(self.station, self.sol, _list_message(["Kai"], kind="note"))
        # kind other than bot_list is rejected at dispatch, still schema, before any pending use
        self.assertEqual(self._verdict(raw, pending=None), "schema")
        raw = _seal(self.station, self.sol, _list_message(["N%d" % i for i in range(25)]))
        self.assertEqual(self._verdict(raw), "schema")
        raw = _seal(self.station, self.sol, _list_message(["Kai!"]))
        self.assertEqual(self._verdict(raw), "schema")
        raw = _seal(self.station, self.sol, _list_message(["Kai", "kai"]))
        self.assertEqual(self._verdict(raw), "schema")
        raw = _seal(self.station, self.sol, _list_message(["Kai", " Kai "]))
        self.assertEqual(self._verdict(raw), "schema")
        raw = _seal(self.station, self.sol, json.dumps({"bots": [{"name": "Kai", "id": "x"}], "kind": "bot_list",
                                                       "request_id": RID, "tier": 0, "action_class": "bot_list"}).encode())
        self.assertEqual(self._verdict(raw), "schema")

    def test_lint_hit_rejects_the_whole_list(self):
        raw = _seal(self.station, self.sol, _list_message(["Kai", "localhost"]))
        self.assertEqual(self._verdict(raw), "lint")
        self.limiter = botreply.Limiter(6, 30)
        raw = _seal(self.station, self.sol, _list_message(["Kai"]))
        with self.assertRaises(botreply.Reject) as cm:
            botreply.ingest(raw, self.station, self.keys, self.known, (lambda _rid: self.pending), set(),
                            self.limiter, names=("kai",), now_ms=self.now, source="sol")
        self.assertEqual(cm.exception.verdict, "lint")

    def test_wrong_signer_and_non_source(self):
        raw = _seal(self.station, self.luna, _list_message(["Kai"]), bot="sol")
        self.assertEqual(self._verdict(raw), "signature")
        raw = _seal(self.station, self.luna, _list_message(["Kai"]), bot="luna")
        pending = dict(self.pending, persona="luna")
        self.assertEqual(self._verdict(raw, pending=pending, source="sol"), "source")

    def test_answer_and_list_do_not_cross(self):
        raw = _seal(self.station, self.sol, _answer_message())
        self.assertEqual(self._verdict(raw), "request")  # open pending is a list
        answer_pending = {k: v for k, v in self.pending.items() if k != "kind"}
        raw = _seal(self.station, self.sol, _list_message(["Kai"]))
        self.assertEqual(self._verdict(raw, pending=answer_pending), "request")

    def test_expired_replay_and_rate(self):
        raw = _seal(self.station, self.sol, _list_message(["Kai"]))
        self.assertEqual(self._verdict(raw, now_ms=self.pending["expires_ms"] + 1), "expired")
        self.assertEqual(self._verdict(raw, seen={RID}), "duplicate")
        lim = botreply.Limiter(1, 30)
        self.assertEqual(self._list(limiter=lim)["names"], ["Kai", "Mira"])
        raw = _seal(self.station, self.sol, _list_message(["Sol"]))
        self.assertEqual(self._verdict(raw, limiter=lim), "rate")

    def test_check_order(self):
        # schema before duplicate
        raw = _seal(self.station, self.sol, _list_message(["Kai"], note="x"))
        self.assertEqual(self._verdict(raw, seen={RID}, pending=None, source="luna"), "schema")
        # lint before duplicate
        raw = _seal(self.station, self.sol, _list_message(["localhost"]))
        self.assertEqual(self._verdict(raw, seen={RID}, pending=None), "lint")
        # duplicate before a missing pending
        raw = _seal(self.station, self.sol, _list_message(["Kai"]))
        self.assertEqual(self._verdict(raw, seen={RID}, pending=None), "duplicate")
        # pending (persona) before source
        raw = _seal(self.station, self.sol, _list_message(["Kai"]))
        self.assertEqual(self._verdict(raw, pending=dict(self.pending, persona="luna"), source="luna"), "request")
        # source last
        self.assertEqual(self._verdict(raw, source="luna"), "source")


class LimitsList(unittest.TestCase):
    def test_one_open_and_one_per_minute_persists(self):
        path = os.path.join(tempfile.mkdtemp(), "limits.json")
        days = ["2026-10-22"]
        lim = handoff.Limits(path, lambda: days[0])
        a, b, c = "vs_" + "a" * 22, "vs_" + "b" * 22, "vs_" + "c" * 22
        self.assertIsNone(lim.take_list(a, now=1000))
        self.assertEqual(lim.take_list(b, now=1030), "too soon")
        self.assertEqual(lim.take_list(b, now=1000), "outstanding")
        lim.release_list()
        self.assertEqual(lim.take_list(c, now=1030), "too soon")
        self.assertIsNone(lim.take_list(c, now=1060))
        saved = json.load(open(path))
        self.assertEqual(saved.get("total", 0), 0)
        self.assertEqual(saved.get("bots", {}), {})
        self.assertEqual(saved["list_ask"]["out"], c)
        lim2 = handoff.Limits(path, lambda: "2026-10-22")
        self.assertEqual(lim2.take_list("vs_" + "d" * 22, now=1070), "too soon")
        days[0] = "2026-10-23"
        lim3 = handoff.Limits(path, lambda: days[0])
        self.assertEqual(lim3.take_list("vs_" + "e" * 22, now=1070), "too soon")
        # an answer reservation must not drop the list gap
        self.assertIsNone(lim3.take("sol", "r1", now=1070))
        self.assertEqual(json.load(open(path))["list_ask"]["last"], 1060.0)
        self.assertEqual(json.load(open(path))["bots"]["sol"]["n"], 1)
        # a future last is too soon, and last 0 is a first ask
        fresh = handoff.Limits(os.path.join(tempfile.mkdtemp(), "fresh.json"), lambda: "2026-10-22")
        fresh.take_list("vs_" + "f" * 22, now=5000)
        fresh.release_list()
        doc = json.load(open(fresh.path))
        doc["list_ask"]["last"] = 9000
        json.dump(doc, open(fresh.path, "w"))
        self.assertEqual(fresh.take_list("vs_" + "g" * 22, now=1000), "too soon")


class Merge(unittest.TestCase):
    def setUp(self):
        import relay
        self.relay = relay
        self.tmp = tempfile.mkdtemp()
        self.snap = (relay.WEBHOOKS_FILE, relay.PERSONAS_FILE, relay.MINTED_FILE, dict(relay._personas))
        relay.WEBHOOKS_FILE = os.path.join(self.tmp, "webhooks.json")
        relay.PERSONAS_FILE = os.path.join(self.tmp, "personas.json")
        relay.MINTED_FILE = os.path.join(self.tmp, "voice-tokens.json")
        relay._personas.update(mtime=None, map={}, voices=None, hosts=[], names=[])
        self._personas([])
        self._clock = 1_700_000_000
        self._time = relay.time.time
        relay.time.time = lambda: self._clock

    def tearDown(self):
        r = self.relay
        r.time.time = self._time
        r.WEBHOOKS_FILE, r.PERSONAS_FILE, r.MINTED_FILE = self.snap[0], self.snap[1], self.snap[2]
        r._personas.clear()
        r._personas.update(self.snap[3])

    def _personas(self, items):
        with open(self.relay.PERSONAS_FILE, "w") as f:
            json.dump({"personas": items}, f)
        self.relay._personas["mtime"] = None

    def _files(self):
        paths = {
            "personas-overlay.json": b'{"added": {}, "hidden": [], "renamed": {}, "voices": {}}\n',
            "webhooks.json": b'{"sol": {"saved_at": 1, "token": "token-token", "url": "%s"}}\n' % GOOD.encode(),
            "bot_keys.json": b'{"keys": {}, "killed": false}\n',
            "voice-tokens.json": b'{"agents": {}}\n',
        }
        out = {}
        for name, content in paths.items():
            path = os.path.join(self.tmp, name)
            with open(path, "wb") as f:
                f.write(content)
            out[path] = content
        return out

    def test_merge_idempotent_configured_missing_and_cap(self):
        files = self._files()
        first = self.relay.merge_imported(["Kai", "Mira"], "sol")
        self.assertEqual(first, {"n": 2, "new": 2, "missing": 0, "dropped": 0})
        doc = json.load(open(os.path.join(self.tmp, "imported.json")))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.tmp, "imported.json")).st_mode), 0o600)
        kai_first = doc["rows"]["kai"]["first_seen"]
        self._clock += 5
        again = self.relay.merge_imported(["Kai", "Mira"], "sol")
        self.assertEqual(again, {"n": 2, "new": 2, "missing": 0, "dropped": 0})
        doc2 = json.load(open(os.path.join(self.tmp, "imported.json")))
        self.assertEqual(doc2["rows"]["kai"]["first_seen"], kai_first)
        self.assertEqual(doc2["rows"]["kai"]["last_seen"], self._clock)
        self.assertNotEqual(doc2["updated_at"], doc["updated_at"])
        self.assertEqual(set(doc2["rows"]), {"kai", "mira"})
        for path, content in files.items():
            self.assertEqual(open(path, "rb").read(), content)
        self._personas([{"slug": "sol", "name": "Kai", "voice": "ara", "prompt": "Helper.", "aliases": [], "facts": []}])
        self._clock += 5
        configured = self.relay.merge_imported(["Kai", "Mira"], "sol")
        self.assertEqual(configured["new"], 1)  # Mira still new; Kai is configured
        view = self.relay.imported_public()
        states = {row["key"]: row["state"] for row in view["rows"]}
        self.assertEqual(states["kai"], "configured")
        self.assertEqual(states["mira"], "new")
        for path, content in files.items():
            self.assertEqual(open(path, "rb").read(), content)
        self._clock += 5
        self.relay.merge_imported(["Kai"], "sol")
        kept = json.load(open(os.path.join(self.tmp, "imported.json")))
        self.assertEqual(kept["rows"]["mira"]["state"], "missing")
        self.assertIn("mira", kept["rows"])
        self.assertTrue(self.relay.dismiss_imported("mira"))
        self.assertNotIn("mira", json.load(open(os.path.join(self.tmp, "imported.json")))["rows"])
        self.assertIsNone(self.relay.dismiss_imported("mira"))
        self.assertFalse(self.relay.dismiss_imported("../kai"))
        # cap: 24 + 24 + 16 fill the store; a later new name is dropped and older rows go missing
        self.relay._save_imported({"source": "sol", "updated_at": 1, "rows": {}})
        batches = [[f"A{i}" for i in range(24)], [f"B{i}" for i in range(24)], [f"C{i}" for i in range(16)]]
        for batch in batches:
            self._clock += 1
            self.relay.merge_imported(batch, "sol")
        full = json.load(open(os.path.join(self.tmp, "imported.json")))
        self.assertEqual(len(full["rows"]), 64)
        self._clock += 1
        dropped = self.relay.merge_imported(["D0"], "sol")
        self.assertEqual(dropped["dropped"], 1)
        self.assertEqual(dropped["n"], 0)
        after = json.load(open(os.path.join(self.tmp, "imported.json")))
        self.assertEqual(len(after["rows"]), 64)
        self.assertTrue(all(row["state"] == "missing" for row in after["rows"].values()))
        self.assertNotIn("d0", after["rows"])


class Offer(unittest.TestCase):
    def test_tool_hidden_without_a_routed_source(self):
        import relay
        roster = {"sol": {"name": "Sol", "slug": "sol"}}
        self.assertNotIn("get_bot_list", [t["name"] for t in relay.tools_for(roster, "grok")])
        self.assertNotIn("get_bot_list", [t["name"] for t in relay.tools_for(roster, "sol")])
        desc = relay.LIST_TOOL["description"].lower()
        self.assertNotRegex(desc, r"\b(send|email|post|buy|order)\b")
        self.assertEqual(relay.LIST_TOOL["parameters"]["required"], [])


class SealRoundTrip(unittest.TestCase):
    def test_seal_list_round_trip(self):
        station = PrivateKey.generate()
        signer = SigningKey.generate()
        seed = _b64(bytes(signer))
        pub = botreply.station_pub_b64(station)
        raw = helper.build_list_outer("sol", RID, ["Kai", " Mira "], seed, pub)
        keys = {"killed": False, "keys": {"sol": _b64(bytes(signer.verify_key))}}
        pending = {"request_id": RID, "persona": "sol", "nonce": "n0", "expires_ms": int(time.time() * 1000) + 60000,
                   "kind": "bot_list", "pii_mode": True}
        got = botreply.ingest(raw, station, keys, {"sol"}, (lambda _rid: pending), set(), botreply.Limiter(6, 30),
                              source="sol")
        self.assertEqual(got["names"], ["Kai", "Mira"])
        old_in, old_env = sys.stdin, os.environ.get("GROK_STATION_BOT_KEY")
        sys.stdin = io.StringIO("Kai\n\nMira\n")
        os.environ["GROK_STATION_BOT_KEY"] = seed
        try:
            rc = helper.main(["seal-list", "--bot", "sol", "--station-pub", pub, "--request-id", RID])
        finally:
            sys.stdin = old_in
            if old_env is None:
                os.environ.pop("GROK_STATION_BOT_KEY", None)
            else:
                os.environ["GROK_STATION_BOT_KEY"] = old_env
        self.assertEqual(rc, 0)
        sys.stdin = io.StringIO("Kai\nKai\n")
        os.environ["GROK_STATION_BOT_KEY"] = seed
        try:
            buf = io.StringIO()
            old_out = sys.stdout
            sys.stdout = buf
            rc = helper.main(["seal-list", "--bot", "sol", "--station-pub", pub, "--request-id", RID])
        finally:
            sys.stdout = old_out
            sys.stdin = old_in
            os.environ.pop("GROK_STATION_BOT_KEY", None)
        self.assertEqual(rc, 1)
        self.assertIn("400 bad request", buf.getvalue())


class ImportApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import relay
        self.relay = relay
        r = relay
        self.tmp = tempfile.mkdtemp()
        self.snap = {
            "WEBHOOKS_FILE": r.WEBHOOKS_FILE, "MINTED_FILE": r.MINTED_FILE, "PENDING_DIR": r.PENDING_DIR,
            "SECRETS_DIR": r.SECRETS_DIR, "REPLIES_DIR": r.REPLIES_DIR, "PERSONAS_FILE": r.PERSONAS_FILE,
            "REPLY_WAIT": r.REPLY_WAIT, "ADMIN_TOKEN": r.ADMIN_TOKEN, "HANDOFF": r.HANDOFF, "_limits": r._limits,
            "log": r.log, "voices": set(r.VOICES),
            "personas": {k: (dict(v) if isinstance(v, dict) else v) for k, v in r._personas.items()},
            "hooks": {"mtime": r._hooks.get("mtime"), "map": dict(r._hooks.get("map") or {})},
            "seen": set(r._reply_seen),
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
        r.HANDOFF = True
        r.REPLY_WAIT = 4
        r.VOICES.clear()
        r.VOICES.update({"ara", "rex", "luna"})
        r._personas.update(mtime=None, map={}, voices=None, hosts=[], names=[])
        r._hooks.update(mtime=None, map={})
        r._limits = handoff.Limits(os.path.join(self.tmp, "limits.json"), r.today)
        self.lines = []
        r.log = lambda *a: self.lines.append(" ".join(map(str, a)))
        with open(r.PERSONAS_FILE, "w") as f:
            json.dump({"personas": [
                {"slug": "sol", "name": "Sol", "voice": "ara", "prompt": "Helper.", "aliases": [], "facts": []},
                {"slug": "luna", "name": "Luna", "voice": "rex", "prompt": "Helper.", "aliases": [], "facts": []},
            ]}, f)
        self.signer = SigningKey.generate()
        app = web.Application()
        app.add_routes(r.ADMIN_ROUTES)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        if getattr(self, "client", None) is not None:
            await self.client.close()
        r, s = self.relay, self.snap
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
        r._reply_seen.clear()
        r._reply_seen.update(s["seen"])

    def headers(self, token="admin-secret", origin=None):
        h = {"Authorization": "Bearer " + token} if token is not None else {}
        if origin is not None:
            h["Origin"] = origin
        return h

    async def _pair(self, slug="sol"):
        pub = _b64(bytes(self.signer.verify_key))
        res = await self.client.put(f"/admin/bots/{slug}/key", headers=self.headers(),
                                    json={"public_key": pub, "actor": "admin"})
        self.assertEqual(res.status, 200, await res.text())

    async def _main(self):
        return (await (await self.client.get("/admin/bots", headers=self.headers())).json())["list_source"]

    async def _add(self, slug):
        res = await self.client.post("/admin/bots", headers=self.headers(),
                                     json={"slug": slug, "name": slug.title(), "voice": "ara", "actor": "admin"})
        self.assertEqual(res.status, 201, await res.text())

    async def _remove(self, slug):
        res = await self.client.delete(f"/admin/bots/{slug}", headers=self.headers(), json={"actor": "admin"})
        self.assertEqual(res.status, 200, await res.text())

    async def test_first_added_bot_becomes_main(self):
        self.assertEqual(await self._main(), "")
        await self._add("kai")
        self.assertEqual(await self._main(), "kai")
        await self._add("ivy")
        self.assertEqual(await self._main(), "kai")  # a later add does not move it

    async def test_set_main_without_pairing(self):
        c = self.client
        res = await c.put("/admin/bots/list-source", headers=self.headers(), json={"list_source": "luna", "actor": "admin"})
        self.assertEqual(res.status, 200, await res.text())
        self.assertEqual(await self._main(), "luna")
        res = await c.put("/admin/bots/list-source", headers=self.headers(), json={"list_source": "nobody", "actor": "admin"})
        self.assertEqual(res.status, 400)
        self.assertEqual(await self._main(), "luna")
        self.assertNotIn("get_bot_list", [t["name"] for t in self.relay.tools_for(self.relay.persona_list(), "grok")])
        say = self.relay.persona_instructions(None, self.relay.persona_list(), datetime.datetime.now())
        self.assertIn("main bot", say)
        self.assertIn("Bots page", say)

    async def test_removing_main_clears_or_picks_the_last_one(self):
        await self._add("kai")
        self.assertEqual(await self._main(), "kai")
        await self._remove("kai")  # sol and luna remain
        self.assertEqual(await self._main(), "")
        await self._add("ivy")
        self.assertEqual(await self._main(), "ivy")  # no main: the next add takes it
        await self._remove("sol")
        await self._remove("ivy")  # only luna remains
        self.assertEqual(await self._main(), "luna")

    async def test_admin_gate_and_fetch(self):
        c, r = self.client, self.relay
        self.assertEqual((await c.get("/admin/bots/imported")).status, 401)
        self.assertEqual((await c.get("/admin/bots/imported", headers=self.headers(origin="https://evil.example"))).status, 403)
        self.assertEqual((await c.post("/admin/bots/import", headers=self.headers(), json={"actor": "admin"})).status, 400)
        listed = await (await c.get("/admin/bots", headers=self.headers())).json()
        self.assertEqual(listed["list_source"], "")
        self.assertEqual(listed["imported"]["rows"], [])
        seen = {"bodies": []}

        async def hook(req):
            body = await req.json()
            seen["bodies"].append(body)
            seed = _b64(bytes(self.signer))
            outer = helper.build_list_outer(body["persona"], body["request_id"], ["Kai", "Mira"], seed,
                                            listed["station_pub"])
            async with self.client.post("/voice/reply", data=outer, headers={"Content-Type": "application/json"}) as resp:
                seen["status"] = resp.status
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/hook", hook)
        srv = TestServer(app)
        await srv.start_server()
        try:
            await self._pair("sol")
            url = str(srv.make_url("/hook"))
            self.assertTrue(r.write_webhooks({"sol": {"url": url, "token": "list-token-aaaa", "saved_at": 10}}))
            saved = await c.put("/admin/bots/list-source", headers=self.headers(),
                                json={"list_source": "sol", "actor": "admin"})
            self.assertEqual(saved.status, 200, await saved.text())
            self.assertIn("get_bot_list", [t["name"] for t in r.tools_for(r.persona_list(), "grok")])
            self.assertNotIn("get_bot_list", [t["name"] for t in r.tools_for(r.persona_list(), "sol")])
            before = open(r.overlay_path(), "rb").read()
            keys_before = open(r.bot_keys_path(), "rb").read()
            hooks_before = open(r.WEBHOOKS_FILE, "rb").read()
            res = await c.post("/admin/bots/import", headers=self.headers(), json={"actor": "admin"})
            body = await res.json()
            self.assertEqual(res.status, 200, body)
            self.assertEqual(body, {"ok": True, "n": 2, "new": 2, "missing": 0, "dropped": 0})
            self.assertEqual(set(seen["bodies"][0]), {"v", "request_id", "persona", "question", "reply_by", "kind"})
            self.assertEqual(seen["bodies"][0]["kind"], "bot_list")
            self.assertEqual(seen["bodies"][0]["question"], "list bots")
            self.assertNotIn("tier_max", seen["bodies"][0])
            self.assertEqual(seen["status"], 200)
            view = await (await c.get("/admin/bots/imported", headers=self.headers())).json()
            names = {row["name"]: row["state"] for row in view["imported"]["rows"]}
            self.assertEqual(names, {"Kai": "new", "Mira": "new"})
            self.assertEqual(open(r.overlay_path(), "rb").read(), before)
            self.assertEqual(open(r.bot_keys_path(), "rb").read(), keys_before)
            self.assertEqual(open(r.WEBHOOKS_FILE, "rb").read(), hooks_before)
            limits = json.load(open(r._limits.path))
            self.assertEqual(limits.get("bots", {}), {})
            self.assertEqual(limits.get("total", 0), 0)
            blob = "\n".join(self.lines)
            audit_path = r.audit_path()
            audit = open(audit_path).read() if os.path.isfile(audit_path) else ""
            for secret in ("Kai", "Mira", "list bots", "list-token-aaaa", url):
                self.assertNotIn(secret, blob)
                self.assertNotIn(secret, audit)
            self.assertIn("import ok n=2 new=2 missing=0", blob)
            self.assertIn(" import ", audit)
            again = await c.post("/admin/bots/import", headers=self.headers(), json={"actor": "admin"})
            self.assertEqual(again.status, 429)
            self.assertEqual((await again.json())["error"], "I just asked; give it a minute.")
            gone = await c.delete("/admin/bots/imported/" + "kai", headers=self.headers(), json={"actor": "admin"})
            self.assertEqual(gone.status, 200, await gone.text())
            left = json.load(open(os.path.join(self.tmp, "imported.json")))
            self.assertNotIn("kai", left["rows"])
            self.assertIn("mira", left["rows"])
            self.assertNotIn("kai", audit + blob)
        finally:
            await srv.close()

    def test_admin_forward_is_explicit(self):
        src = open(os.path.join(ROOT, "admin", "grok_admin.py"), encoding="utf-8").read()
        for path in ("/api/bots/import", "/api/bots/imported", "/api/bots/list-source", "/admin/bots/import"):
            self.assertIn(path, src)
        self.assertIn("timeout=40", src)


if __name__ == "__main__":
    unittest.main()


class ReviewFixes(Merge):
    """Security review conditions: hidden names, slug collisions, lookalikes, empty lists, guest lines."""

    def _hide(self, slug):
        with open(os.path.join(self.tmp, "personas-overlay.json"), "w") as f:
            json.dump({"added": {}, "hidden": [slug], "renamed": {}, "voices": {}}, f)

    def test_hidden_bot_name_is_new_and_not_counted(self):
        self._files()
        self._personas([{"slug": "sol", "name": "Kai", "voice": "ara", "prompt": "Helper.", "aliases": [], "facts": []}])
        self._hide("sol")
        out = self.relay.merge_imported(["Kai"], "sol")
        self.assertEqual(out["new"], 1)
        rows = {r["key"]: r for r in self.relay.imported_public()["rows"]}
        self.assertEqual(rows["kai"]["state"], "new")
        self.assertNotIn("hidden", json.dumps(rows))
        line = self.relay.list_spoken("Sol", out, "ok")
        self.assertNotIn("Kai", line)

    def test_empty_list_is_noop_error(self):
        self._files()
        self.relay.merge_imported(["Kai", "Mira"], "sol")
        path = os.path.join(self.tmp, "imported.json")
        before = open(path, "rb").read()
        self.assertEqual(self.relay.merge_imported([], "sol"), {"empty": True})
        self.assertEqual(open(path, "rb").read(), before)
        self.assertEqual(self.relay.list_spoken("Sol", None, "no-names"), "Sol returned no names.")

    def test_guest_line_counts_only(self):
        out = {"n": 2, "new": 1, "missing": 0, "dropped": 0}
        for guest in (True, False):
            line = self.relay.voice_list_line({"guest": guest}, "Sol", out, "ok")
            self.assertIn("2 bots", line)
            self.assertNotIn("Kai", line)


class LookalikeNames(unittest.TestCase):
    def test_homoglyphs_and_invisibles_rejected(self):
        for bad in ("K\u0430i", "\uff2bai", "Ka\u200bi", "K\u00e1i", "Kai\u202e", "\u039aai", "Ka\u0131", ""):
            self.assertIsNone(botreply.list_name_clean(bad), repr(bad))
        self.assertEqual(botreply.list_name_clean("Kai"), "Kai")


class SetupPrefill(unittest.TestCase):
    def test_setup_uses_strict_slug_and_stays_editable(self):
        js = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        self.assertIn('$("#aslug", d).value = botIdFromName(preset.name);', js)
        self.assertIn("slugEdited = true", js)
        self.assertIn("A non-ASCII letter is a dash", js)


class SlugCollision(unittest.IsolatedAsyncioTestCase):
    async def test_imported_name_cannot_overwrite_existing_bot(self):
        import relay
        tmp = tempfile.mkdtemp()
        snap = (relay.WEBHOOKS_FILE, relay.PERSONAS_FILE, relay.MINTED_FILE, dict(relay._personas))
        relay.WEBHOOKS_FILE = os.path.join(tmp, "webhooks.json")
        relay.PERSONAS_FILE = os.path.join(tmp, "personas.json")
        relay.MINTED_FILE = os.path.join(tmp, "voice-tokens.json")
        relay._personas.update(mtime=None, map={}, voices=None, hosts=[], names=[])
        with open(relay.PERSONAS_FILE, "w") as f:
            json.dump({"personas": [{"slug": "sol", "name": "Sol", "voice": "ara", "prompt": "x", "aliases": [], "facts": []}]}, f)
        try:
            before = open(relay.PERSONAS_FILE, "rb").read()
            ov = relay.load_overlay_doc()
            self.assertIn("sol", relay.registry_index())
            app = web.Application()
            app.add_routes(relay.ADMIN_ROUTES)
            old_gate = relay.admin_gate
            async def gate(_req):
                return None
            relay.admin_gate = gate
            try:
                async with TestClient(TestServer(app)) as c:
                    r = await c.post("/admin/bots", json={"actor": "admin", "slug": "sol", "name": "SOL", "voice": ""})
                    self.assertEqual(r.status, 409)
            finally:
                relay.admin_gate = old_gate
            self.assertEqual(open(relay.PERSONAS_FILE, "rb").read(), before)
            self.assertEqual(relay.load_overlay_doc(), ov)
        finally:
            relay.WEBHOOKS_FILE, relay.PERSONAS_FILE, relay.MINTED_FILE = snap[0], snap[1], snap[2]
            relay._personas.clear(); relay._personas.update(snap[3])
