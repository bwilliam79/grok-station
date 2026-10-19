#!/usr/bin/env python3
"""Teammate handoff (real-bot pilot): B4, B6, B10, B13, B14 and tier scope, against a fake bot webhook."""
import asyncio, json, os, sys, tempfile, time, unittest

from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer

TMP = tempfile.mkdtemp()
os.environ.update(RELAY_TOKEN="test-token", XAI_API_KEY="test-key", ALERT_DIR=os.path.join(TMP, "none"), PERSONAS_FILE=os.path.join(TMP, "none.json"),
                  HANDOFF_PENDING=os.path.join(TMP, "pending"), HANDOFF_REPLIES=os.path.join(TMP, "replies"),
                  HANDOFF_STATE=os.path.join(TMP, "handoff.json"), REPLY_WAIT="3")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay"))
import handoff  # noqa: E402
import relay  # noqa: E402


class Units(unittest.TestCase):
    def test_question_cleaned_capped_delimited(self):
        q = handoff.clean("what\u200b's for \u202edinner\x07 " + "x" * 400, handoff.Q_MAX)
        self.assertEqual(len(q), 300)
        self.assertFalse(any(c in q for c in "\u200b\u202e\x07"))
        p = handoff.payload(handoff.new_request_id(), "jules", q, "2026-10-19T21:00:00+00:00")
        self.assertEqual((p["source"], p["v"], p["tier_max"]), ("grok-station", 1, 0))
        self.assertTrue(p["question"].startswith(handoff.BEGIN + "\n") and p["question"].endswith("\n" + handoff.END))
        self.assertRegex(p["request_id"], handoff.REQUEST_ID)

    def test_lint_one_reject_per_class_and_clean_pass(self):
        rejects = {"chars": "tacos\u200b", "digits": "order 123456", "url": "see example.com", "email": "a@b",
                   "ip": "at 203.0.113.7", "host": "on pantry.local", "vin": "car 1HGCM82633A004352 ok",
                   "phone": "call 312-555-0187", "secret": "the password is fish", "money": "costs $40",
                   "medical": "her medication list", "address": "at 1234 Oak Street"}
        for code, t in rejects.items():
            self.assertEqual(handoff.lint_reply(t), code, t)
        self.assertEqual(handoff.lint_reply("on bigbox", ["bigbox"]), "host")
        self.assertIsNone(handoff.lint_reply("Tacos on Tuesday and pasta on Wednesday."))

    def test_limits(self):
        lim = handoff.Limits(os.path.join(TMP, "lim.json"), lambda: "d1")
        self.assertIsNone(lim.take("jules", "r1", now=1000))
        self.assertEqual(lim.take("jules", "r2", now=1100), "outstanding")
        lim.release("jules")
        self.assertEqual(lim.take("jules", "r2", now=1010), "too soon")
        for i in range(7):
            self.assertIsNone(lim.take("jules", f"r{i}", now=2000 + 31 * i)); lim.release("jules")
        self.assertEqual(lim.take("jules", "r9", now=9000), "bot daily cap")    # 8/day, the failed ones included
        self.assertIsNone(lim.take("hank", "h1", now=9000))                     # per bot
        lim2 = handoff.Limits(os.path.join(TMP, "lim2.json"), lambda: "d1")
        for i in range(handoff.GLOBAL_DAY):
            bot = "abcdefghijklmnopqrstuvwxyz"[i // 8] * 3
            self.assertIsNone(lim2.take(bot, f"g{i}", now=1000 + 100 * i)); lim2.release(bot)
        self.assertEqual(lim2.take("zzz", "g", now=1e6), "global daily cap")

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


class RoundTrip(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for f in ("handoff.json",):
            try: os.remove(os.path.join(TMP, f))
            except OSError: pass
        self.calls, self.mode = [], "reply"

        async def hook(req):
            body = await req.json()
            self.calls.append(body)
            if self.mode == "fail":
                return web.Response(status=500)
            pend = json.load(open(os.path.join(relay.PENDING_DIR, body["request_id"] + ".json")))  # tile-ingest's bind check
            if self.mode == "reply":
                async def later():
                    await asyncio.sleep(0.3)
                    os.makedirs(relay.REPLIES_DIR, exist_ok=True)
                    json.dump({"request_id": body["request_id"], "agent": pend["persona"], "nonce": pend["nonce"], "tier": 0,
                               "action_class": "answer", "text": "Tacos on Tuesday."},
                              open(os.path.join(relay.REPLIES_DIR, body["request_id"] + ".json"), "w"))
                asyncio.get_running_loop().create_task(later())
            return web.json_response({"ok": True})
        app = web.Application(); app.router.add_post("/hook", hook)
        self.srv = TestServer(app); await self.srv.start_server()
        relay.WEBHOOKS = {"jules": {"url": str(self.srv.make_url("/hook")), "token": "t"}}
        relay.HANDOFF = True
        self.lines = []
        self._log = relay.log
        relay.log = lambda *a: self.lines.append(" ".join(map(str, a)))

    async def asyncTearDown(self):
        relay.log = self._log
        await self.srv.close()

    async def test_answer_spoken_and_logs_have_no_text(self):
        waited = []
        async def on_wait(): waited.append(1)
        async with ClientSession() as http:
            text, verdict = await relay.ask_teammate(http, "jules", "what's for dinner this week?", on_wait)
        self.assertEqual((text, verdict), ("Tacos on Tuesday.", "ok"))
        self.assertEqual(len(self.calls), 1); self.assertEqual(waited, [1])
        self.assertEqual(os.listdir(relay.PENDING_DIR), [])           # pending removed: a late reply can't bind
        line = [l for l in self.lines if "verdict=spoken" in l][0]
        self.assertNotIn("Tacos", line); self.assertNotIn("dinner", line)
        print("\nLOG", line)

    async def test_post_failure_not_retried_and_counted(self):
        self.mode = "fail"
        async with ClientSession() as http:
            self.assertEqual((await relay.ask_teammate(http, "jules", "q"))[1], "post-failed")
            self.assertEqual(len(self.calls), 1)                      # B4: never retried
            self.assertEqual((await relay.ask_teammate(http, "jules", "q"))[1], "limit")   # counted: 30 s gap
        self.assertEqual(len(self.calls), 1)

    async def test_no_reply_times_out(self):
        self.mode = "silent"
        t0 = time.monotonic()
        async with ClientSession() as http:
            self.assertEqual(await relay.ask_teammate(http, "jules", "q"), (None, "timeout"))
        self.assertLess(time.monotonic() - t0, 4.5)

    def test_tool_only_for_bots_with_a_webhook(self):
        roster = {"jules": {"slug": "jules", "name": "Jules"}, "hank": {"slug": "hank", "name": "Hank"}}
        self.assertIn("check_notes", [t["name"] for t in relay.tools_for(roster, "jules")])
        self.assertNotIn("check_notes", [t["name"] for t in relay.tools_for(roster, "hank")])
        relay.HANDOFF = False
        self.assertNotIn("check_notes", [t["name"] for t in relay.tools_for(roster, "jules")])


if __name__ == "__main__":
    unittest.main()
