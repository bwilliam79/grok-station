"""Relay pii_mode: default, both policy files, audit, and the spoken-reply lint around it.

The webhook carries no question and no pii_mode. An admin Test is flagged, counted, and cannot
satisfy some other request. Import relay inside the tests so collection does not bind its env first.
"""
import asyncio, json, os, stat, sys, tempfile, time, unittest

from aiohttp import web, ClientSession
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay"))


def _mods():
    import handoff
    import relay
    return handoff, relay


class Lint(unittest.TestCase):
    def test_spelled_out_ssn_and_card_blocked_with_pii_off(self):
        handoff, _relay = _mods()
        ssn = "one-two-three-four-five-six-seven-eight-nine"
        card = "four one one one one one one one one one one one one one one one"
        self.assertEqual(handoff.lint_reply(ssn, pii=False), "ssn")
        self.assertEqual(handoff.lint_reply(card, pii=False), "card")
        self.assertEqual(handoff.lint_reply("the password is swordfish", pii=False), "secret")
        self.assertIsNone(handoff.lint_reply("enter the password", pii=False))

    def test_email_passes_off_blocks_on_and_a_factual_reply_passes(self):
        handoff, _relay = _mods()
        email = "write ada@example.com please"
        factual = "Tacos on Tuesday and pasta on Wednesday."
        bears = "The Bears play the Packers Sunday at noon; tickets start around $85 on the team site."
        self.assertIsNone(handoff.lint_reply(email, pii=False))
        self.assertEqual(handoff.lint_reply(email, pii=True), "email")
        self.assertIsNone(handoff.lint_reply(factual, pii=False))
        self.assertIsNone(handoff.lint_reply(factual, pii=True))
        self.assertIsNone(handoff.lint_reply(bears, pii=True))
        self.assertIsNone(handoff.lint_reply("ask Sam about dinner", names=["Sam"], pii=False))
        self.assertEqual(handoff.lint_reply("ask Sam about dinner", names=["Sam"], pii=True), "name")

    def test_lint_exception_fails_closed(self):
        handoff, _relay = _mods()
        orig = handoff._lint

        def boom(*_a, **_k):
            raise RuntimeError("lint broke")

        handoff._lint = boom
        try:
            self.assertEqual(handoff.lint_reply("a normal sentence"), "error")
        finally:
            handoff._lint = orig


class Policy(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handoff, relay = _mods()
        self.handoff, self.relay = handoff, relay
        self.tmp = tempfile.mkdtemp()
        r = relay
        self.snap = {
            "WEBHOOKS_FILE": r.WEBHOOKS_FILE, "MINTED_FILE": r.MINTED_FILE, "AUDIT_MIRROR": r.AUDIT_MIRROR,
            "AUDIT_MIRROR_DIR": r.AUDIT_MIRROR_DIR,
            "PENDING_DIR": r.PENDING_DIR, "REPLIES_DIR": r.REPLIES_DIR, "PERSONAS_FILE": r.PERSONAS_FILE,
            "ADMIN_TOKEN": r.ADMIN_TOKEN, "HANDOFF": r.HANDOFF, "log": r.log,
            "policy": dict(r._policy), "voices": set(r.VOICES),
            "personas": {k: (dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v))
                         for k, v in r._personas.items()},
        }
        r.WEBHOOKS_FILE = os.path.join(self.tmp, "webhooks.json")
        r.MINTED_FILE = os.path.join(self.tmp, "minted", "voice-tokens.json")
        r.PENDING_DIR = os.path.join(self.tmp, "pending")
        r.REPLIES_DIR = os.path.join(self.tmp, "replies")
        r.PERSONAS_FILE = os.path.join(self.tmp, "personas.json")
        r.AUDIT_MIRROR = os.path.join(self.tmp, "voice-audit", "audit.log")
        r.AUDIT_MIRROR_DIR = ""  # legacy AUDIT_MIRROR path, not a preferred directory from the environment
        os.makedirs(os.path.dirname(r.AUDIT_MIRROR))
        r.ADMIN_TOKEN = b"admin-secret"
        r._policy.update(mtime=None, pii=True)
        r.VOICES.clear()
        r.VOICES.update({"ara"})
        r._personas.update(mtime=None, map={}, voices=None, hosts=[], names=[])
        r.log = lambda *_a: None
        app = web.Application()
        app.add_routes([web.get("/admin/policy", r.admin_policy_get), web.put("/admin/policy", r.admin_policy_put)])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        if getattr(self, "client", None) is not None:
            await self.client.close()
        r, s = self.relay, self.snap
        r.WEBHOOKS_FILE, r.MINTED_FILE, r.AUDIT_MIRROR = s["WEBHOOKS_FILE"], s["MINTED_FILE"], s["AUDIT_MIRROR"]
        r.AUDIT_MIRROR_DIR = s["AUDIT_MIRROR_DIR"]
        r.PENDING_DIR, r.REPLIES_DIR = s["PENDING_DIR"], s["REPLIES_DIR"]
        r.PERSONAS_FILE, r.ADMIN_TOKEN = s["PERSONAS_FILE"], s["ADMIN_TOKEN"]
        r.HANDOFF, r.log = s["HANDOFF"], s["log"]
        r._policy.clear()
        r._policy.update(s["policy"])
        r.VOICES.clear()
        r.VOICES.update(s["voices"])
        r._personas.clear()
        r._personas.update(s["personas"])

    def headers(self, token="admin-secret", fp="c" * 64):
        h = {"Authorization": "Bearer " + token, "X-Client-Cert-Fp": fp}
        return h

    def test_missing_invalid_and_stale_guest_default_on(self):
        r = self.relay
        self.assertIs(r.read_pii_mode(), True)
        path = r.policy_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("not json")
        r._policy.update(mtime=None, pii=False)
        self.assertIs(r.read_pii_mode(), True)
        with open(path, "w") as f:
            json.dump({"guest": False, "pii_mode": "no"}, f)
        r._policy.update(mtime=None, pii=False)
        self.assertIs(r.read_pii_mode(), True)  # non-bool and a stale guest key do not turn it off
        with open(path, "w") as f:
            json.dump({"guest": True, "pii_mode": False}, f)
        r._policy.update(mtime=None, pii=True)
        self.assertIs(r.read_pii_mode(), False)  # the bool is honored; guest is not a mode

    def test_stale_guest_on_a_persona_is_ignored(self):
        r = self.relay
        with open(r.PERSONAS_FILE, "w") as f:
            json.dump({"guest": True, "household_names": ["Sam"], "private_hostnames": ["bigbox"],
                       "personas": [{"slug": "jules", "name": "Jules", "voice": "ara", "guest": True,
                                     "prompt": "The notes helper."}]}, f)
        r._personas["mtime"] = None
        self.assertIn("jules", r.persona_list())
        self.assertEqual(r._personas["names"], ["sam"])
        self.assertEqual(r._personas["hosts"], ["bigbox"])

    async def test_put_writes_both_files_and_appends_audit(self):
        r = self.relay
        audit = r.audit_path()
        mirror_path = r.audit_mirror_path()
        os.makedirs(os.path.dirname(audit), exist_ok=True)
        with open(audit, "w") as f:
            f.write("KEEP\n")
        with open(mirror_path, "w") as f:
            f.write("KEEP\n")
        r.ensure_policy()
        self.assertEqual(json.load(open(r.policy_path())), {"pii_mode": True})
        self.assertEqual(json.load(open(r.minted_policy_path())), {"pii_mode": True})
        self.assertEqual(stat.S_IMODE(os.stat(r.policy_path()).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(r.minted_policy_path()).st_mode), 0o644)
        bare = await self.client.put("/admin/policy", json={"pii_mode": False, "actor": "admin"})
        self.assertEqual(bare.status, 401)
        bad = await self.client.put("/admin/policy", headers=self.headers(), json={"pii_mode": "no", "actor": "admin"})
        self.assertEqual(bad.status, 400)
        missing_actor = await self.client.put("/admin/policy", headers=self.headers(), json={"pii_mode": False})
        self.assertEqual(missing_actor.status, 400)
        off = await self.client.put("/admin/policy", headers=self.headers(), json={"pii_mode": False, "actor": "admin"})
        self.assertEqual(off.status, 200)
        self.assertEqual(await off.json(), {"pii_mode": False})
        for path, mode in ((r.policy_path(), 0o600), (r.minted_policy_path(), 0o644)):
            self.assertEqual(json.load(open(path)), {"pii_mode": False})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), mode)
            self.assertNotIn("guest", open(path).read())
        on = await self.client.put("/admin/policy", headers=self.headers(), json={"pii_mode": True, "actor": "admin"})
        self.assertEqual(await on.json(), {"pii_mode": True})
        text = open(audit).read()
        mirror = open(mirror_path).read()
        self.assertEqual(mirror, text)
        self.assertTrue(text.startswith("KEEP\n"))
        lines = [ln for ln in text.splitlines() if ln and ln != "KEEP"]
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z admin - pii-mode off -$")
        self.assertRegex(lines[1], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z admin - pii-mode on -$")
        got = await (await self.client.get("/admin/policy", headers=self.headers())).json()
        self.assertEqual(got, {"pii_mode": True})
        r.factory_reset_policy()
        self.assertEqual(json.load(open(r.policy_path())), {"pii_mode": True})
        self.assertEqual(json.load(open(r.minted_policy_path())), {"pii_mode": True})
        os.remove(r.policy_path())
        r._policy.update(mtime=None, pii=False)
        self.assertIs(r.read_pii_mode(), True)


class TestFlag(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handoff, relay = _mods()
        self.handoff, self.relay = handoff, relay
        self.tmp = tempfile.mkdtemp()
        r = relay
        self.snap = {
            "WEBHOOKS_FILE": r.WEBHOOKS_FILE, "MINTED_FILE": r.MINTED_FILE, "PENDING_DIR": r.PENDING_DIR,
            "REPLIES_DIR": r.REPLIES_DIR, "REPLY_WAIT": r.REPLY_WAIT, "HANDOFF": r.HANDOFF, "log": r.log,
            "_limits": r._limits, "policy": dict(r._policy), "seen": set(r._seen_rids),
            "hooks": {"mtime": r._hooks.get("mtime"), "map": dict(r._hooks.get("map") or {})},
        }
        r.WEBHOOKS_FILE = os.path.join(self.tmp, "webhooks.json")
        r.MINTED_FILE = os.path.join(self.tmp, "minted", "voice-tokens.json")
        r.PENDING_DIR = os.path.join(self.tmp, "pending")
        r.REPLIES_DIR = os.path.join(self.tmp, "replies")
        r.REPLY_WAIT = 3
        r.HANDOFF = True
        r._limits = handoff.Limits(os.path.join(self.tmp, "limits.json"), r.today)
        r._policy.update(mtime=None, pii=True)
        r.write_policy(False)
        r.log = lambda *_a: None
        self.seen = []

        async def hook(req):
            body = await req.json()
            pend = json.load(open(os.path.join(r.PENDING_DIR, body["request_id"] + ".json")))
            self.seen.append((body, pend))
            r.write_policy(True)  # live config changes; the reply is judged with the value from issue time
            async def later():
                await asyncio.sleep(0.05)
                os.makedirs(r.REPLIES_DIR, exist_ok=True)
                json.dump({"request_id": body["request_id"], "agent": pend["persona"], "nonce": pend["nonce"],
                           "tier": 0, "action_class": "answer", "text": "write ada@example.com please"},
                          open(os.path.join(r.REPLIES_DIR, body["request_id"] + ".json"), "w"))
            asyncio.get_running_loop().create_task(later())
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/hook", hook)
        self.srv = TestServer(app)
        await self.srv.start_server()
        with open(r.WEBHOOKS_FILE, "w") as f:
            json.dump({"jules": {"url": str(self.srv.make_url("/hook")), "token": "t"}}, f)
        r._hooks["mtime"] = None
        self.real = handoff.new_request_id()
        os.makedirs(r.REPLIES_DIR, exist_ok=True)
        with open(os.path.join(r.REPLIES_DIR, self.real + ".json"), "w") as f:
            json.dump({"request_id": self.real, "agent": "jules", "nonce": "N" * 32, "tier": 0,
                       "action_class": "answer", "text": "real answer"}, f)

    async def asyncTearDown(self):
        await self.srv.close()
        r, s = self.relay, self.snap
        r.WEBHOOKS_FILE, r.MINTED_FILE = s["WEBHOOKS_FILE"], s["MINTED_FILE"]
        r.PENDING_DIR, r.REPLIES_DIR = s["PENDING_DIR"], s["REPLIES_DIR"]
        r.REPLY_WAIT, r.HANDOFF, r.log, r._limits = s["REPLY_WAIT"], s["HANDOFF"], s["log"], s["_limits"]
        r._policy.clear()
        r._policy.update(s["policy"])
        r._seen_rids.clear()
        r._seen_rids.update(s["seen"])
        r._hooks.clear()
        r._hooks.update(s["hooks"])

    async def test_test_rid_is_flagged_counted_and_cannot_resolve_a_real_request(self):
        r = self.relay
        r._limits.gap_s = 30.0                                        # gap is opt-in (HANDOFF_GAP_S)
        async with ClientSession() as http:
            text, verdict = await r.ask_teammate(http, "jules", "what is for dinner?", test=True)
        self.assertEqual((text, verdict), ("write ada@example.com please", "ok"))
        body, pend = self.seen[0]
        self.assertEqual(set(body), {"v", "request_id", "persona", "question", "reply_by", "test"})
        self.assertIs(body["test"], True)
        self.assertEqual(body["v"], 1)
        self.assertEqual(body["question"], "what is for dinner?")
        for dropped in ("source", "pii_mode", "tier_max", "include_avatar"):
            self.assertNotIn(dropped, body)
        self.assertNotEqual(body["request_id"], self.real)
        self.assertEqual(pend["question"], "what is for dinner?")
        self.assertIs(pend["test"], True)
        self.assertIs(pend["pii_mode"], False)
        self.assertEqual(pend["tier_max"], 0)
        self.assertTrue(pend["reply_by"])
        self.assertIsInstance(pend["expires_ms"], int)
        self.assertTrue(os.path.isfile(os.path.join(r.REPLIES_DIR, self.real + ".json")))
        self.assertEqual(json.load(open(r._limits.path))["bots"]["jules"]["n"], 1)
        async with ClientSession() as http:
            again, why = await r.ask_teammate(http, "jules", "another question", test=True)
        self.assertEqual((again, why), (None, "too-soon"))
        self.assertEqual(len(self.seen), 1)  # the capped call never posted
        self.assertEqual(json.load(open(r._limits.path))["bots"]["jules"]["n"], 1)
        self.assertGreater(pend["expires_ms"], time.time() * 1000)
