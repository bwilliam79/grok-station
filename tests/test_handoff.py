#!/usr/bin/env python3
"""Teammate handoff (real-bot pilot): B4, B6, B10, B13, B14 and tier scope, against a fake bot webhook."""
import asyncio, base64, datetime, json, os, sys, tempfile, time, unittest

from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer

TMP = tempfile.mkdtemp()
os.environ.update(RELAY_TOKEN="test-token", XAI_API_KEY="test-key", ALERT_DIR=os.path.join(TMP, "none"), PERSONAS_FILE=os.path.join(TMP, "none.json"),
                  HANDOFF_PENDING=os.path.join(TMP, "pending"), HANDOFF_REPLIES=os.path.join(TMP, "replies"),
                  HANDOFF_STATE=os.path.join(TMP, "handoff.json"), REPLY_WAIT="3")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay"))
import botreply  # noqa: E402
import handoff  # noqa: E402
import relay  # noqa: E402


class Units(unittest.TestCase):
    def test_question_cleaned_capped_and_sent_on_the_webhook(self):
        """The question is cleaned to 500 chars and sent with reply_by. pii_mode and tier_max stay off the wire."""
        q = handoff.clean("what\u200b's for \u202edinner\x07 " + "x" * 800, handoff.Q_MAX)
        self.assertEqual(handoff.Q_MAX, 500)
        self.assertEqual(len(q), 500)
        self.assertFalse(any(c in q for c in "\u200b\u202e\x07"))
        rid = handoff.new_request_id()
        reply_by = "2026-10-19T21:00:00Z"
        p = handoff.payload(rid, "jules", q, reply_by)
        self.assertEqual((p["v"], p["tier_max"], p["question"], p["reply_by"]), (1, 0, q, reply_by))
        wire = json.loads(json.dumps(p))
        self.assertEqual(set(wire), {"v", "request_id", "persona", "question", "reply_by"})
        for dropped in ("source", "pii_mode", "tier_max", "test", "include_avatar"):
            self.assertNotIn(dropped, wire)
        self.assertRegex(p["request_id"], handoff.REQUEST_ID)
        flagged = json.loads(json.dumps(handoff.payload(rid, "jules", q, reply_by, test=True, include_avatar=True)))
        self.assertEqual(set(flagged), {"v", "request_id", "persona", "question", "reply_by", "test"})
        self.assertIs(flagged["test"], True)
        self.assertNotIn("include_avatar", flagged)

    def test_lint_always_blocked_and_pii_only_when_on(self):
        """Secrets, SSNs and cards are blocked with pii off. Email and the rest of PII are not.

        Money, links, VINs, a bare password word, a short digit run and a routing number are not content rules.
        """
        bears = "The Bears play the Packers Sunday at noon; tickets start around $85 on the team site."
        long_token = "k7Qm9Zp2Lx8Nv4Tc6Rb1Hs5Yw3Jd0FaB9c"  # 32 or more, mixed, high entropy
        short_token = "k7Qm9Zp2Lx8Nv4Tc6Rb1Hs5Yw3Jd0F"    # 30: under the cutoff, not a secret
        # text, pii off, pii on
        table = [
            (bears, None, None),
            ("see example.com", None, None),
            ("order 123456", None, None),
            ("costs $40", None, None),
            ("her medication list", None, None),
            ("enter the password", None, None),
            ("the password is fish", "secret", "secret"),
            ("key sk-AbcdEFGH1234wxyz", "secret", "secret"),
            ("use xai-Abcd1234efgh5678", "secret", "secret"),
            ("ghp_abcdefgh12345678", "secret", "secret"),
            ("github_pat_11AAAAAAA0aaaaaaaaaa", "secret", "secret"),
            ("AKIAIOSFODNN7EXAMPLE", "secret", "secret"),
            ("Bearer abcdefghijklmnop", "secret", "secret"),
            ("-----BEGIN PRIVATE KEY-----", "secret", "secret"),
            (long_token, "secret", "secret"),
            (short_token, None, None),
            ("one-two-three-four-five-six-seven-eight-nine", "ssn", "ssn"),
            ("ssn 123-45-6789", "ssn", "ssn"),
            ("order 123456789", "ssn", "ssn"),  # 9 digits, valid SSN shape, not a routing number
            ("four one one one one one one one one one one one one one one one", "card", "card"),
            ("4111111111111111", "card", "card"),
            ("4111111111111112", None, None),  # 16 digits, Luhn fail: not a card
            ("021000021", None, None),
            ("account number 12345678", None, None),
            ("the routing number is on the check", None, None),
            ("tacos\u200b", None, None),  # hidden chars are stripped on the reply path, not a content rule
            ("write ada@example.com please", None, "email"),
            ("a@b", None, "email"),
            ("call 312-555-0187", None, "phone"),
            ("at 203.0.113.7", None, "ip"),
            ("mac aa:bb:cc:dd:ee:ff", None, "mac"),
            ("on pantry.local", None, "host"),
            ("car 1HGCM82633A004352 ok", None, None),
            ("at 1234 Oak Street", None, "address"),
            ("Tacos on Tuesday and pasta on Wednesday.", None, None),
        ]
        for text, off, on in table:
            self.assertEqual(handoff.lint_reply(text, pii=False), off, "pii off " + text)
            self.assertEqual(handoff.lint_reply(text, pii=True), on, "pii on " + text)
        self.assertIsNone(handoff.lint_reply("on bigbox", ["bigbox"], pii=False))
        self.assertEqual(handoff.lint_reply("on bigbox", ["bigbox"], pii=True), "host")
        self.assertIsNone(handoff.lint_reply("on pantry.lan", pii=False))
        self.assertEqual(handoff.lint_reply("on pantry.lan", pii=True), "host")
        self.assertIsNone(handoff.lint_reply("ask Sam about dinner", names=["Sam"], pii=False))
        self.assertEqual(handoff.lint_reply("ask Sam about dinner", names=["Sam"], pii=True), "name")

    def test_lint_exception_fails_closed(self):
        orig = handoff._lint

        def boom(*_a, **_k):
            raise RuntimeError("lint broke")

        handoff._lint = boom
        try:
            self.assertEqual(handoff.lint_reply("hello there"), "error")
        finally:
            handoff._lint = orig

    def test_limits_off_by_default(self):
        self.assertEqual((handoff.PER_BOT_DAY, handoff.GLOBAL_DAY, handoff.GAP_S), (0, 0, 0))
        lim = handoff.Limits(os.path.join(TMP, "lim-off.json"), lambda: "d1")
        for i in range(100):                                                  # no daily cap, no gap
            self.assertIsNone(lim.take("jules", f"r{i}", now=1000 + i))
            self.assertEqual(lim.take("jules", "x", now=1000 + i), "outstanding")  # still one in flight
            lim.release("jules")

    def test_limits_env(self):
        old = {k: os.environ.get(k) for k in ("HANDOFF_PER_BOT_DAY", "HANDOFF_GLOBAL_DAY", "HANDOFF_GAP_S")}
        try:
            os.environ.update(HANDOFF_PER_BOT_DAY="2", HANDOFF_GLOBAL_DAY="3", HANDOFF_GAP_S="10")
            self.assertEqual(handoff._env_num("HANDOFF_PER_BOT_DAY", int), 2)
            self.assertEqual(handoff._env_num("HANDOFF_GAP_S", float), 10.0)
            os.environ.update(HANDOFF_PER_BOT_DAY="0", HANDOFF_GLOBAL_DAY="junk", HANDOFF_GAP_S="-5")
            for k, c in (("HANDOFF_PER_BOT_DAY", int), ("HANDOFF_GLOBAL_DAY", int), ("HANDOFF_GAP_S", float)):
                self.assertEqual(handoff._env_num(k, c), 0)
            del os.environ["HANDOFF_PER_BOT_DAY"]
            self.assertEqual(handoff._env_num("HANDOFF_PER_BOT_DAY", int), 0)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        lim = handoff.Limits(os.path.join(TMP, "lim-set.json"), lambda: "d1", per_bot_day=2, global_day=3, gap_s=10)
        self.assertIsNone(lim.take("a", "1", now=100)); lim.release("a")
        self.assertEqual(lim.take("a", "2", now=105), "too soon")
        self.assertIsNone(lim.take("a", "2", now=120)); lim.release("a")
        self.assertEqual(lim.take("a", "3", now=200), "bot daily cap")
        self.assertIsNone(lim.take("b", "4", now=200)); lim.release("b")
        self.assertEqual(lim.take("c", "5", now=200), "global daily cap")

    def test_limits(self):
        lim = handoff.Limits(os.path.join(TMP, "lim.json"), lambda: "d1", per_bot_day=8, global_day=24, gap_s=30.0)
        self.assertIsNone(lim.take("jules", "r1", now=1000))
        self.assertEqual(lim.take("jules", "r2", now=1100), "outstanding")
        lim.release("jules")
        self.assertEqual(lim.take("jules", "r2", now=1010), "too soon")
        for i in range(7):
            self.assertIsNone(lim.take("jules", f"r{i}", now=2000 + 31 * i)); lim.release("jules")
        self.assertEqual(lim.take("jules", "r9", now=9000), "bot daily cap")    # 8/day, the failed ones included
        self.assertIsNone(lim.take("hank", "h1", now=9000))                     # per bot
        lim2 = handoff.Limits(os.path.join(TMP, "lim2.json"), lambda: "d1", per_bot_day=8, global_day=24, gap_s=30.0)
        for i in range(24):
            bot = "abcdefghijklmnopqrstuvwxyz"[i // 8] * 3
            self.assertIsNone(lim2.take(bot, f"g{i}", now=1000 + 100 * i)); lim2.release(bot)
        self.assertEqual(lim2.take("zzz", "g", now=1e6), "global daily cap")
        saved = json.load(open(lim.path))
        saved["guest"] = True  # stale key from an older limits file
        json.dump(saved, open(lim.path, "w"))
        lim.release("hank")
        self.assertNotIn("guest", json.load(open(lim.path)))

    def test_take_reply_binding_scope_replay(self):
        d = tempfile.mkdtemp()
        rid = handoff.new_request_id()
        w = lambda **k: open(os.path.join(d, rid + ".json"), "w").write(json.dumps(dict(
            {"request_id": rid, "agent": "jules", "nonce": "N" * 32, "tier": 0, "action_class": "answer", "text": "Tacos."}, **k)))
        self.assertEqual(handoff.take_reply(d, rid, "jules", "N" * 32), (None, None))
        w(nonce="X" * 32); self.assertEqual(handoff.take_reply(d, rid, "jules", "N" * 32)[1], "nonce")
        w(agent="hank"); self.assertEqual(handoff.take_reply(d, rid, "jules", "N" * 32)[1], "unbound")
        w(tier=1, action_class="note"); self.assertEqual(handoff.take_reply(d, rid, "jules", "N" * 32)[1], "above-scope")
        w(text="Call 312-555-0187"); self.assertEqual(handoff.take_reply(d, rid, "jules", "N" * 32)[1], "lint:phone")
        w(text="Call 312-555-0187"); self.assertEqual(handoff.take_reply(d, rid, "jules", "N" * 32, pii=False),
                                                      ("Call 312-555-0187", None))
        w(text="ssn 123-45-6789"); self.assertEqual(handoff.take_reply(d, rid, "jules", "N" * 32, pii=False)[1], "lint:ssn")
        w(text="tacos\u200b tonight"); self.assertEqual(handoff.take_reply(d, rid, "jules", "N" * 32), ("tacos tonight", None))
        seen = set()
        w(text="**Tacos** tonight."); self.assertEqual(handoff.take_reply(d, rid, "jules", "N" * 32, seen=seen), ("Tacos tonight.", None))
        self.assertEqual(handoff.take_reply(d, rid, "jules", "N" * 32, seen=seen), (None, None))   # consumed: speaks once
        w(); self.assertEqual(handoff.take_reply(d, rid, "jules", "N" * 32, seen=seen)[1], "duplicate")

    def test_webhooks_parse_never_needs_secrets_in_repo(self):
        self.assertEqual(handoff.load_webhooks(""), {})
        hooks = handoff.load_webhooks(json.dumps({"jules": {"url": "https://example.invalid/h", "token": "t"},
                                                  "hank": {"url": "http://plain", "token": "t"},
                                                  "kai": {"url": "https://x.invalid", "token": "t", "enabled": False}}))
        self.assertEqual(list(hooks), ["jules"])
        self.assertEqual(handoff.load_webhooks(json.dumps({"kai": {"url": "http://192.0.2.5/x", "token": "t"}})), {})

    def test_router_dropped_and_route_is_the_bot(self):
        """Router mode is retired: _router is never kept, and route() is that bot's own entry."""
        hooks = handoff.load_webhooks(json.dumps({
            "jules": {"url": "https://j.example.invalid/h", "token": "jt"},
            "_router": {"url": "https://r.example.invalid/h", "token": "rt"},
            "Nope": {"url": "https://n.example.invalid/h", "token": "t"},
            "x": {"url": "https://n.example.invalid/h", "token": "t"},
            "has space": {"url": "https://n.example.invalid/h", "token": "t"},
            "a" * 25: {"url": "https://n.example.invalid/h", "token": "t"},
            "a" * 17: {"url": "https://n.example.invalid/h", "token": "t"},
            "kai-2": {"url": "https://n.example.invalid/h", "token": "kt"},
            "grok": {"url": "https://n.example.invalid/h", "token": "gt"}}))
        self.assertNotIn("_router", hooks)
        self.assertNotIn("grok", hooks)
        self.assertEqual(set(hooks), {"jules", "kai-2", "a" * 17})       # slug pattern allows a hyphen and up to 24 chars
        self.assertIs(handoff.route(hooks, "jules"), hooks["jules"])      # own entry; the router does not take it
        self.assertIsNone(handoff.route(hooks, "finn"))
        self.assertIsNone(handoff.route(hooks, "hank"))
        on = handoff.load_webhooks(json.dumps({"_router": {"url": "http://127.0.0.1:9/h", "token": "t", "enabled": True}}))
        self.assertEqual(on, {})                                          # a router entry alone is nothing
        bots = handoff.load_webhooks(json.dumps({"jules": {"url": "https://j.example.invalid/h", "token": "jt"}}))
        self.assertEqual(handoff.route(bots, "jules"), bots["jules"])
        self.assertIsNone(handoff.route(bots, "hank"))
        self.assertIsNone(handoff.route({}, "jules"))
        self.assertIsNone(handoff.route(None, "jules"))
        for raw in (
            {"_router": {"url": "http://192.0.2.5/x", "token": "t"}, "finn": {"url": "https://f.example.invalid/h", "token": "ft"}},
            {"_router": {"url": "https://r.example.invalid/h", "token": ""}},
            {"_router": {"url": "https://r.example.invalid/h"}},
            {"_router": {"url": "https://r.example.invalid/h", "token": "t", "enabled": False},
             "finn": {"url": "https://f.example.invalid/h", "token": "ft"}},
        ):
            got = handoff.load_webhooks(json.dumps(raw))
            self.assertNotIn("_router", got, raw)
        kept = handoff.load_webhooks(json.dumps({
            "_router": {"url": "http://192.0.2.5/x", "token": "t"},
            "finn": {"url": "https://f.example.invalid/h", "token": "ft"},
            "Nope": {"url": "https://n.example.invalid/h", "token": "t"}}))
        self.assertEqual(list(kept), ["finn"])
        self.assertEqual(handoff.route(kept, "finn")["token"], "ft")
        self.assertIsNone(handoff.route(kept, "jules"))
        self.assertEqual(handoff.load_webhooks("[]"), {})
        self.assertEqual(handoff.load_webhooks("not json"), {})


class RoundTrip(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for f in ("handoff.json",):
            try: os.remove(os.path.join(TMP, f))
            except OSError: pass
        # relay._limits was built from whichever file imported relay first (its HANDOFF_STATE, maybe a used one):
        # give these round trips a fresh, empty limits file and put the old object back afterwards.
        self.addCleanup(setattr, relay, "_limits", relay._limits)
        relay._limits = handoff.Limits(os.path.join(TMP, "handoff.json"), relay.today)
        # Same for the state paths read once at import (NI2): another suite may have imported relay first.
        fresh = tempfile.mkdtemp(dir=TMP)  # per test: no pending file from an earlier test (any order)
        for name, val in (("PENDING_DIR", os.path.join(fresh, "pending")), ("REPLIES_DIR", os.path.join(fresh, "replies")),
                          ("REPLY_WAIT", 3.0), ("ALERT_DIR", os.path.join(TMP, "none")),
                          ("PERSONAS_FILE", os.path.join(TMP, "none.json"))):
            self.addCleanup(setattr, relay, name, getattr(relay, name))
            setattr(relay, name, val)
        self.calls, self.mode, self.auths, self.paths, self.pendings = [], "reply", [], [], []

        async def hook(req):
            body = await req.json()
            self.calls.append(body)
            self.auths.append(req.headers.get("Authorization", ""))
            self.paths.append(req.path)
            pend_path = os.path.join(relay.PENDING_DIR, body["request_id"] + ".json")
            self.pendings.append(json.load(open(pend_path)))
            if self.mode == "fail":
                return web.Response(status=500)
            pend = json.load(open(os.path.join(relay.PENDING_DIR, body["request_id"] + ".json")))
            if self.mode == "reply":
                async def later():
                    await asyncio.sleep(0.3)
                    os.makedirs(relay.REPLIES_DIR, exist_ok=True)
                    json.dump({"request_id": body["request_id"], "agent": pend["persona"], "nonce": pend["nonce"], "tier": 0,
                               "action_class": "answer", "text": "Tacos on Tuesday."},
                              open(os.path.join(relay.REPLIES_DIR, body["request_id"] + ".json"), "w"))
                asyncio.get_running_loop().create_task(later())
            return web.json_response({"ok": True})
        app = web.Application(); app.router.add_post("/hook", hook); app.router.add_post("/bot", hook)
        self.srv = TestServer(app); await self.srv.start_server()
        relay.WEBHOOKS_FILE = os.path.join(TMP, "webhooks.json")
        try:
            os.remove(botreply.keys_path(TMP))
        except OSError:
            pass
        with open(relay.WEBHOOKS_FILE, "w") as f:
            json.dump({"jules": {"url": str(self.srv.make_url("/hook")), "token": "t"}}, f)
        relay._hooks["mtime"] = None
        self._handoff = relay.HANDOFF
        relay.HANDOFF = True
        self.lines = []
        self._log = relay.log
        relay.log = lambda *a: self.lines.append(" ".join(map(str, a)))

    async def asyncTearDown(self):
        relay.log = self._log
        relay.HANDOFF = self._handoff
        await self.srv.close()

    async def test_answer_spoken_and_logs_have_no_text(self):
        waited = []
        async def on_wait(): waited.append(1)
        async with ClientSession() as http:
            text, verdict = await relay.ask_teammate(http, "jules", "what's for dinner this week?", on_wait)
        self.assertEqual((text, verdict), ("Tacos on Tuesday.", "ok"))
        self.assertEqual(len(self.calls), 1); self.assertEqual(waited, [1])
        # 0.13.0: the pending request stays for one follow-up reply, parked for at most FOLLOWUP_S (60 s), then goes.
        rid = self.calls[0]["request_id"]
        self.assertEqual(os.listdir(relay.PENDING_DIR), [rid + ".json"])
        self.assertLessEqual(relay._LATE[rid]["hard_at"], time.time() + relay.FOLLOWUP_S)
        relay._LATE.pop(rid, None)
        line = [l for l in self.lines if "verdict=spoken" in l][0]
        self.assertNotIn("Tacos", line); self.assertNotIn("dinner", line)
        print("\nLOG", line)

    async def test_gs1312_bq0018_card_after_a_spoken_answer_reaches_the_station(self):
        """bq-0018 (fails before the fix): a bot answers, the station speaks it, then the bot posts its recipe card
        (the routine says "after the spoken reply"). The 0.13.0 follow-up park (FOLLOWUP_S) is a _LATE slot for the
        same bot, so voice_show's late_hold_show() swallows the card into it. No follow-up comes, the slot expires,
        and the card is never posted. This is the main bot relaying a Jules recipe (0.13.10 tests 3 and 4)."""
        jules_recipe = {"bot": "jules", "op": "put", "id": "orzo", "v": 1, "priority": 10, "ttl_s": 3600,
                        "card": {"v": 1, "type": "recipe", "title": "Lemon herb orzo", "servings": 4,
                                 "ingredients": ["orzo"], "steps": ["Boil the orzo."], "step": 1}}
        async with ClientSession() as http:
            text, verdict = await relay.ask_teammate(http, "jules", "a recipe for dinner?")
        self.assertEqual(verdict, "ok")
        rid = self.calls[0]["request_id"]
        self.addCleanup(relay._LATE.pop, rid, None)
        held = relay.late_hold_show(jules_recipe)
        self.assertFalse(held, "the card after a spoken answer was held in the follow-up slot instead of shown")

    async def test_post_failure_not_retried_and_counted(self):
        self.mode = "fail"
        relay._limits.gap_s = 30.0                                    # gap is opt-in (HANDOFF_GAP_S)
        async with ClientSession() as http:
            self.assertEqual((await relay.ask_teammate(http, "jules", "q"))[1], "post-failed")
            self.assertEqual(len(self.calls), 1)                      # B4: never retried
            self.assertEqual((await relay.ask_teammate(http, "jules", "q"))[1], "too-soon")   # counted: 30 s gap
        self.assertEqual(len(self.calls), 1)

    async def test_no_reply_times_out(self):
        self.mode = "silent"
        t0 = time.monotonic()
        async with ClientSession() as http:
            self.assertEqual(await relay.ask_teammate(http, "jules", "q"), (None, "timeout"))
        self.assertLess(time.monotonic() - t0, 8.0)

    async def test_timeout_alerts_and_does_not_say_still(self):
        self.mode = "silent"
        alerts, stills = [], []
        self._alert = relay.alert
        relay.alert = lambda kind, title, msg, *a: alerts.append((kind, msg))
        async def on_still(): stills.append(1)
        try:
            async with ClientSession() as http:
                self.assertEqual(await relay.ask_teammate(http, "jules", "secret question", None, on_still), (None, "timeout"))
        finally:
            relay.alert = self._alert
        self.assertEqual(stills, [])                                     # no "still asking" line
        self.assertEqual(alerts[0][0], "handoff-timeout"); self.assertIn("verdict=timeout", alerts[0][1])
        self.assertTrue(any("verdict=timeout wait_ms=" in l for l in self.lines))
        self.assertFalse(any("secret question" in l for l in self.lines))
        self.assertEqual(relay.handoff_error("Jules", "timeout"),
                         'Say exactly: "I couldn\'t reach Jules." Don\'t guess the answer.')

    async def test_checking_line_fires_once_when_the_wait_is_long_enough(self):
        self.mode = "silent"
        old = relay.CHECKING_AFTER_S
        relay.CHECKING_AFTER_S = 0
        stills = []

        async def on_still():
            stills.append(1)

        try:
            async with ClientSession() as http:
                self.assertEqual(await relay.ask_teammate(http, "jules", "q", None, on_still), (None, "timeout"))
        finally:
            relay.CHECKING_AFTER_S = old
        self.assertEqual(stills, [1])

    async def test_cancel_keeps_the_pending_request_for_a_matching_reply(self):
        self.mode = "silent"
        spoken = []
        old_sink, old_late, old_busy = relay._LATE_SINK, dict(relay._LATE), relay.stats["busy"]
        relay._LATE_SINK = spoken.append
        relay._LATE.clear()
        relay.stats["busy"] = 0
        try:
            async with ClientSession() as http:
                task = asyncio.create_task(relay.ask_teammate(http, "jules", "what's on my calendar"))
                for _ in range(50):
                    if self.calls:
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(self.calls)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            rid = self.calls[0]["request_id"]
            pend = json.load(open(os.path.join(relay.PENDING_DIR, rid + ".json")))
            self.assertEqual(pend["request_id"], rid)
            self.assertFalse(relay.late_accept("vs_" + "b" * 22, "nope", pend["nonce"]))
            self.assertEqual(spoken, [])
            self.assertTrue(relay.late_accept(rid, "Dinner at 6.", "not-the-nonce"))
            self.assertEqual(spoken, [])
            self.assertNotIn(rid, relay._LATE)
            relay._park_late(rid, "jules", "what's on my calendar", pend["nonce"], time.time() + 180)
            relay._LATE[rid]["name"] = "Jules"
            self.assertTrue(relay.late_accept(rid, "Dinner at 6.", pend["nonce"]))
            self.assertEqual(spoken[0]["text"], "Jules, about your calendar: Dinner at 6.")
        finally:
            relay._LATE_SINK = old_sink
            relay._LATE.clear()
            relay._LATE.update(old_late)
            relay.stats["busy"] = old_busy
            for name in os.listdir(relay.PENDING_DIR):
                try:
                    os.unlink(os.path.join(relay.PENDING_DIR, name))
                except OSError:
                    pass

    async def test_per_bot_path_logs_http_status_and_latency(self):
        self.assertNotIn("_router", relay.webhooks())                  # router retired: per-bot entry is used
        async with ClientSession() as http:
            self.assertEqual((await relay.ask_teammate(http, "jules", "q"))[1], "ok")
        self.assertEqual(self.paths, ["/hook"]); self.assertEqual(self.auths, ["Bearer t"])
        line = [l for l in self.lines if " http=" in l][0]
        self.assertRegex(line, r"agent=jules via=bot http=200 post_ms=\d+$")
        self.assertNotIn("127.0.0.1", line)

    def test_defaults(self):
        src = open(relay.__file__).read()
        self.assertIn('E("REPLY_WAIT", "180")', src); self.assertNotIn("STILL_AFTER", src)
        self.assertIn('E("ANSWER_BY", str(botreply.REPLY_BY_S))', src); self.assertIn('E("CHECKING_AFTER", "8")', src)
        self.assertNotIn("can't reach your \"\n", src); self.assertNotIn("full notes from the station yet", src)

    def test_hot_reload(self):
        self.assertNotIn("kai", relay.webhooks())
        time.sleep(0.01)
        d = json.load(open(relay.WEBHOOKS_FILE)); d["kai"] = {"url": "https://example.invalid/k", "token": "t"}
        with open(relay.WEBHOOKS_FILE, "w") as f:   # in place, like grok-voice-add-webhook
            json.dump(d, f)
        self.assertIn("kai", relay.webhooks())
        self.assertFalse(any("example.invalid" in l or '"t"' in l for l in self.lines))   # never logs URLs or keys
        self.assertTrue(any(l.startswith("handoff: ") and "bot webhook" in l for l in self.lines))
        self.assertFalse(any("router" in l for l in self.lines))

    def test_tool_only_for_bots_with_a_webhook_and_a_pinned_key(self):
        roster = {"jules": {"slug": "jules", "name": "Jules"}, "hank": {"slug": "hank", "name": "Hank"}}
        self.assertNotIn("check_notes", [t["name"] for t in relay.tools_for(roster, "jules")])  # webhook alone
        botreply.pin_key(botreply.keys_path(TMP), "jules", base64.b64encode(b"\x11" * 32).decode())
        self.assertIn("check_notes", [t["name"] for t in relay.tools_for(roster, "jules")])
        self.assertNotIn("check_notes", [t["name"] for t in relay.tools_for(roster, "hank")])
        relay.HANDOFF = False
        self.assertNotIn("check_notes", [t["name"] for t in relay.tools_for(roster, "jules")])

    def test_router_does_not_cover_the_roster(self):
        roster = {s: {"slug": s, "name": s.title()} for s in ("rhea", "kai", "hank", "finn", "jules")}
        url, token = "https://router.example.invalid/h", "router-token-secret"
        with open(relay.WEBHOOKS_FILE, "w") as f:
            json.dump({"_router": {"url": url, "token": token}}, f)
        relay._hooks["mtime"] = None
        self.lines.clear()
        for slug in roster:
            self.assertNotIn("check_notes", [t["name"] for t in relay.tools_for(roster, slug)])
        self.assertFalse(relay.has_route("finn") or relay.has_route("grok"))
        self.assertNotIn("_router", relay.webhooks())
        self.assertFalse(any(url in l or token in l or "router" in l for l in self.lines))
        with open(relay.WEBHOOKS_FILE, "w") as f:
            json.dump({"jules": {"url": "https://j.example.invalid/h", "token": "jt"}}, f)
        relay._hooks["mtime"] = None
        # A webhook alone is not a route. The bot's pinned verify key is what replaced the reply token.
        botreply.pin_key(botreply.keys_path(os.path.dirname(relay.WEBHOOKS_FILE)), "jules",
                         base64.b64encode(b"\x11" * 32).decode())
        self.lines.clear()
        self.assertIn("check_notes", [t["name"] for t in relay.tools_for(roster, "jules")])
        self.assertNotIn("check_notes", [t["name"] for t in relay.tools_for(roster, "hank")])
        self.assertTrue(relay.has_route("jules"))
        self.assertFalse(relay.has_route("hank"))
        self.assertTrue(any(l.startswith("handoff: 1 bot webhook(s): jules") for l in self.lines))

    async def test_per_bot_webhook_ignores_a_router_entry(self):
        relay._limits.gap_s = 30.0                                    # gap is opt-in (HANDOFF_GAP_S)
        router_url = str(self.srv.make_url("/hook"))
        bot_url = str(self.srv.make_url("/bot"))
        router_token, jules_token = "router-bearer-token", "jules-bearer-token"
        with open(relay.WEBHOOKS_FILE, "w") as f:
            json.dump({"_router": {"url": router_url, "token": router_token},
                       "jules": {"url": bot_url, "token": jules_token}}, f)
        relay._hooks["mtime"] = None
        self.lines.clear()
        q = "what's for dinner this week?"
        async with ClientSession() as http:
            text, verdict = await relay.ask_teammate(http, "jules", q)
            self.assertEqual((text, verdict), ("Tacos on Tuesday.", "ok"))
            self.assertEqual((await relay.ask_teammate(http, "jules", q))[1], "too-soon")   # 30 s gap, still per persona
            self.assertEqual(len(self.calls), 1)                                          # the limited call never POSTed
            text, verdict = await relay.ask_teammate(http, "hank", "what is on the menu?")
        self.assertEqual((text, verdict), (None, "no-webhook"))                          # hank has no entry; the router is ignored
        self.assertEqual(self.paths, ["/bot"])                                           # jules's own webhook, not the router
        self.assertEqual(self.auths, ["Bearer " + jules_token])
        self.assertNotIn(router_token, " ".join(self.auths))
        self.assertEqual([c["persona"] for c in self.calls], ["jules"])
        body = self.calls[0]
        self.assertEqual(set(body), {"v", "request_id", "persona", "question", "reply_by"})
        self.assertEqual((body["v"], body["persona"], body["question"]), (1, "jules", q))
        for dropped in ("source", "pii_mode", "tier_max", "test", "include_avatar"):
            self.assertNotIn(dropped, body)
        self.assertRegex(body["request_id"], handoff.REQUEST_ID)
        pend = self.pendings[0]
        self.assertGreater(pend["expires_ms"], time.time() * 1000 + 90000)  # accept window, not the short test wait
        rb = datetime.datetime.strptime(body["reply_by"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        ahead = rb.timestamp() - time.time()
        self.assertGreater(ahead, 150)  # about 3 min on the wire, not the old 60 s
        self.assertLess(ahead, 200)
        left = pend["expires_ms"] - time.time() * 1000
        self.assertGreater(left, 150000)
        self.assertLess(left, 200000)
        self.assertEqual(pend["question"], q)
        self.assertIs(pend["pii_mode"], True)
        self.assertEqual(pend["tier_max"], 0)
        self.assertIs(pend["test"], False)
        self.assertTrue(pend["reply_by"])
        self.assertIsInstance(pend["expires_ms"], int)
        self.assertEqual(pend["request_id"], body["request_id"])
        self.assertEqual(pend["persona"], "jules")
        blob = json.dumps(self.calls)
        self.assertNotIn(jules_token, blob)
        self.assertNotIn(router_token, blob)                                             # bearer is the header, not the body
        logged = "\n".join(self.lines)
        for secret in (router_token, jules_token, router_url, bot_url, "dinner", "menu", "Tacos"):
            self.assertNotIn(secret, logged)
        state = json.load(open(os.environ["HANDOFF_STATE"]))
        self.assertEqual(state["bots"]["jules"]["n"], 1)                                 # the rejected retry did not count twice
        self.assertEqual(state["bots"]["hank"]["n"], 1)                                  # no webhook still counts, same as before
        self.assertNotIn("_router", state["bots"])
        self.assertEqual(state["total"], 2)
        self.assertTrue(any(l.startswith("handoff: 1 bot webhook(s): jules") for l in self.lines))
        self.assertFalse(any("router" in l for l in self.lines))


if __name__ == "__main__":
    unittest.main()
