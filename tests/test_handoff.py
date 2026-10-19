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
        self.assertEqual(handoff.load_webhooks(json.dumps({"kai": {"url": "http://192.0.2.5/x", "token": "t"}})), {})

    def test_router_kept_and_route_falls_back(self):
        hooks = handoff.load_webhooks(json.dumps({
            "jules": {"url": "https://j.example.invalid/h", "token": "jt"},
            "_router": {"url": "https://r.example.invalid/h", "token": "rt"},
            "Nope": {"url": "https://n.example.invalid/h", "token": "t"},
            "x": {"url": "https://n.example.invalid/h", "token": "t"},
            "has space": {"url": "https://n.example.invalid/h", "token": "t"},
            "a" * 17: {"url": "https://n.example.invalid/h", "token": "t"},
            "kai-2": {"url": "https://n.example.invalid/h", "token": "t"}}))
        self.assertEqual(set(hooks), {"_router", "jules"})
        self.assertEqual(hooks["_router"], {"url": "https://r.example.invalid/h", "token": "rt"})
        self.assertIs(handoff.route(hooks, "jules"), hooks["_router"])   # own entry loses to the router
        self.assertIs(handoff.route(hooks, "finn"), hooks["_router"])
        self.assertIs(handoff.route(hooks, "hank"), hooks["_router"])
        on = handoff.load_webhooks(json.dumps({"_router": {"url": "http://127.0.0.1:9/h", "token": "t", "enabled": True}}))
        self.assertEqual(list(on), ["_router"])                          # enabled defaults true; loopback URL allowed
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
        self.calls, self.mode, self.auths, self.paths = [], "reply", [], []

        async def hook(req):
            body = await req.json()
            self.calls.append(body)
            self.auths.append(req.headers.get("Authorization", ""))
            self.paths.append(req.path)
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
        app = web.Application(); app.router.add_post("/hook", hook); app.router.add_post("/bot", hook)
        self.srv = TestServer(app); await self.srv.start_server()
        relay.WEBHOOKS_FILE = os.path.join(TMP, "webhooks.json")
        with open(relay.WEBHOOKS_FILE, "w") as f:
            json.dump({"jules": {"url": str(self.srv.make_url("/hook")), "token": "t"}}, f)
        relay._hooks["mtime"] = None
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

    async def test_timeout_alerts_and_says_still_once_with_honest_error(self):
        self.mode = "silent"
        alerts, stills = [], []
        self._alert = relay.alert
        relay.alert = lambda kind, title, msg, *a: alerts.append((kind, msg))
        relay.STILL_AFTER = 0.5
        async def on_still(): stills.append(1)
        try:
            async with ClientSession() as http:
                self.assertEqual(await relay.ask_teammate(http, "jules", "secret question", None, on_still), (None, "timeout"))
        finally:
            relay.alert, relay.STILL_AFTER = self._alert, 20.0
        self.assertEqual(stills, [1])                                    # spoken once, not on every poll
        self.assertEqual(alerts[0][0], "handoff-timeout"); self.assertIn("verdict=timeout", alerts[0][1])
        self.assertTrue(any("verdict=timeout wait_ms=" in l for l in self.lines))
        self.assertFalse(any("secret question" in l for l in self.lines))
        self.assertEqual(relay.handoff_error("Jules", "timeout"),
                         'Say exactly: "Jules didn\'t answer in time; try again or ask in the app." Don\'t guess the answer.')

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
        self.assertIn('E("REPLY_WAIT", "120")', src); self.assertIn('E("STILL_AFTER", "20")', src)
        self.assertNotIn("can't reach your \"\n", src); self.assertNotIn("full notes from the station yet", src)

    def test_hot_reload(self):
        self.assertNotIn("kai", relay.webhooks())
        time.sleep(0.01)
        d = json.load(open(relay.WEBHOOKS_FILE)); d["kai"] = {"url": "https://example.invalid/k", "token": "t"}
        with open(relay.WEBHOOKS_FILE, "w") as f:   # in place, like grok-voice-add-webhook
            json.dump(d, f)
        self.assertIn("kai", relay.webhooks())
        self.assertFalse(any("example.invalid" in l or '"t"' in l for l in self.lines))   # never logs URLs or keys
        self.assertTrue(any(l.startswith("handoff: router off,") for l in self.lines))

    def test_tool_only_for_bots_with_a_webhook(self):
        roster = {"jules": {"slug": "jules", "name": "Jules"}, "hank": {"slug": "hank", "name": "Hank"}}
        self.assertIn("check_notes", [t["name"] for t in relay.tools_for(roster, "jules")])
        self.assertNotIn("check_notes", [t["name"] for t in relay.tools_for(roster, "hank")])
        relay.HANDOFF = False
        self.assertNotIn("check_notes", [t["name"] for t in relay.tools_for(roster, "jules")])

    def test_router_gives_every_roster_persona_check_notes(self):
        roster = {s: {"slug": s, "name": s.title()} for s in ("rhea", "kai", "hank", "finn", "jules")}
        url, token = "https://router.example.invalid/h", "router-token-secret"
        with open(relay.WEBHOOKS_FILE, "w") as f:
            json.dump({"_router": {"url": url, "token": token}}, f)
        relay._hooks["mtime"] = None
        self.lines.clear()
        for slug in roster:
            names = [t["name"] for t in relay.tools_for(roster, slug)]
            self.assertIn("check_notes", names)
            self.assertIn("talk_to", names)
        self.assertNotIn("check_notes", [t["name"] for t in relay.tools_for(roster, "grok")])  # not a roster persona
        self.assertTrue(relay.has_route("finn") and relay.has_route("grok"))
        reload = [l for l in self.lines if l.startswith("handoff:")]
        self.assertRegex(reload[0], r"^handoff: router on, 0 bot webhook\(s\):?$")
        self.assertFalse(any(url in l or token in l for l in self.lines))
        with open(relay.WEBHOOKS_FILE, "w") as f:
            json.dump({}, f)
        relay._hooks["mtime"] = None
        self.lines.clear()
        for slug in roster:
            self.assertNotIn("check_notes", [t["name"] for t in relay.tools_for(roster, slug)])
        self.assertFalse(relay.has_route("jules"))
        self.assertTrue(any(l.startswith("handoff: router off, 0 bot webhook(s)") for l in self.lines))

    async def test_router_post_uses_router_bearer_and_persona_caps(self):
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
            self.assertEqual((await relay.ask_teammate(http, "jules", q))[1], "limit")   # 30 s gap, still per persona
            self.assertEqual(len(self.calls), 1)                                          # the limited call never POSTed
            text, verdict = await relay.ask_teammate(http, "hank", "what is on the menu?")
        self.assertEqual((text, verdict), ("Tacos on Tuesday.", "ok"))                   # hank has no entry; router covers him
        self.assertEqual(self.paths, ["/hook", "/hook"])                                 # not jules's /bot
        self.assertEqual(self.auths, ["Bearer " + router_token, "Bearer " + router_token])
        self.assertNotIn(jules_token, " ".join(self.auths))
        self.assertEqual([c["persona"] for c in self.calls], ["jules", "hank"])
        body = self.calls[0]
        self.assertEqual(set(body), {"source", "v", "request_id", "persona", "tier_max", "reply_by", "question"})
        self.assertEqual((body["source"], body["v"], body["tier_max"], body["persona"]), ("grok-station", 1, 0, "jules"))
        self.assertEqual(body["question"], f"{handoff.BEGIN}\n{q}\n{handoff.END}")
        self.assertRegex(body["request_id"], handoff.REQUEST_ID)
        self.assertTrue(body["reply_by"])
        blob = json.dumps(self.calls)
        self.assertNotIn(jules_token, blob)
        self.assertNotIn(router_token, blob)                                             # bearer is the header, not the body
        logged = "\n".join(self.lines)
        for secret in (router_token, jules_token, router_url, bot_url, "dinner", "menu", "Tacos"):
            self.assertNotIn(secret, logged)
        state = json.load(open(os.environ["HANDOFF_STATE"]))
        self.assertEqual(state["bots"]["jules"]["n"], 1)                                 # the rejected retry did not count twice
        self.assertEqual(state["bots"]["hank"]["n"], 1)                                  # gap is per persona, not per router
        self.assertNotIn("_router", state["bots"])
        self.assertEqual(state["total"], 2)
        self.assertTrue(any(l.startswith("handoff: router on, 1 bot webhook(s): jules") for l in self.lines))


if __name__ == "__main__":
    unittest.main()
