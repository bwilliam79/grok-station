#!/usr/bin/env python3
"""Persona route: a webhook persona gets no general-model speech. The relay asks the bot itself.

A fake upstream emits response.created, response.audio.delta and response.text on every turn, which is what the
realtime model would do if it were allowed to answer. On a routed turn none of that may reach the station, and the
relay must not send response.create.
"""
import asyncio, base64, contextlib, json, os, sys, tempfile, unittest

from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestServer, TestClient

TMP = tempfile.mkdtemp()
os.environ.update(RELAY_TOKEN="test-token", XAI_API_KEY="test-key", ALERT_DIR=os.path.join(TMP, "none"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay"))
import botreply  # noqa: E402
import handoff  # noqa: E402
import relay  # noqa: E402

AUDIO = base64.b64encode(b"\0\0" * 240).decode()
MODEL_PCM = b"\0\0" * 240
PRE = "MODEL-PREANSWER"
TOKEN = "webhook-key-do-not-log"
DINNER = "what's for dinner"
TACOS = "Tacos on Tuesday."
BEARS = "The Bears play the Packers Sunday at noon; tickets start around $85 on the team site."
SECRET = "the password is fish"
ASK_JULES = "ask Jules what's for dinner"
TOOL_Q = "tool question should not be posted"


def _personas_doc():
    return {"personas": [
        {"slug": "jules", "name": "Jules", "voice": "ara", "prompt": "The notes helper."},
        {"slug": "hank", "name": "Hank", "voice": "rex", "prompt": "The household helper."},
    ]}


class PersonaRoute(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.snap = {k: getattr(relay, k) for k in (
            "HANDOFF", "URL", "log", "tts_pcm", "VOICES", "PERSONAS_FILE", "WEBHOOKS_FILE",
            "PENDING_DIR", "REPLIES_DIR", "REPLY_WAIT", "ALERT_DIR", "_limits",
            "MINTED_FILE", "ACK_DIR")}
        self.personas_snap = dict(relay._personas)
        self.hooks_snap = {"mtime": relay._hooks.get("mtime"), "map": dict(relay._hooks.get("map") or {})}
        self.alerted_snap = set(relay.alerted)
        self.busy = relay.stats["busy"]
        self.addCleanup(self._restore)

        self.lines, self.tts, self.calls, self.pendings = [], [], [], []
        self.reply_text, self.reply_delay, self.mode = TACOS, 0, "reply"
        self.alert_dir = os.path.join(self.tmp, "alerts")
        os.makedirs(self.alert_dir)
        relay.ALERT_DIR = self.alert_dir
        relay.alerted.clear()
        relay.stats["busy"] = 0
        relay.HANDOFF = True
        relay.PERSONAS_FILE = os.path.join(self.tmp, "personas.json")
        relay.WEBHOOKS_FILE = os.path.join(self.tmp, "webhooks.json")
        relay.PENDING_DIR = os.path.join(self.tmp, "pending")
        relay.REPLIES_DIR = os.path.join(self.tmp, "replies")
        relay.ACK_DIR = os.path.join(self.tmp, "ack")
        os.makedirs(relay.PENDING_DIR)
        os.makedirs(relay.REPLIES_DIR)
        os.makedirs(relay.ACK_DIR)
        with open(relay.PERSONAS_FILE, "w") as f:
            json.dump(_personas_doc(), f)
        relay.VOICES = {"ara", "rex", "luna"}
        relay._personas["mtime"] = None
        relay._hooks["mtime"] = None
        relay._hooks["map"] = {}
        relay._limits = handoff.Limits(os.path.join(self.tmp, "limits.json"), relay.today)
        relay.log = lambda *a: self.lines.append(" ".join(map(str, a)))

        self.tts_caps = []
        self.fail_asking = False
        self.ack_before_post = False
        self.list_names = None

        async def fake_tts(http, text, voice, max_s=relay.TTS_SHORT_S):
            self.tts.append((text, voice))
            self.tts_caps.append((text, max_s))
            if self.fail_asking and str(text).startswith("Asking"):
                return None
            return b"TTS:" + str(text).encode()

        relay.tts_pcm = fake_tts

    def _restore(self):
        for k, v in self.snap.items():
            setattr(relay, k, v)
        relay._personas.clear()
        relay._personas.update(self.personas_snap)
        relay._hooks.clear()
        relay._hooks.update(self.hooks_snap)
        relay._policy.update(mtime=None, pii=True)
        relay.alerted.clear()
        relay.alerted.update(self.alerted_snap)
        relay.stats["busy"] = self.busy

    def test_fixed_lines_and_registry_lint_stays_strict(self):
        self.assertEqual(relay.asking_line("Jules"), "Asking Jules\u2026")
        self.assertEqual(relay.handoff_line("Jules", "timeout"), "I couldn't reach Jules.")
        self.assertEqual(relay.handoff_line("Jules", "timeout", reached=True), "Jules couldn't reply in time.")
        self.assertEqual(relay.handoff_line("Jules", "reject"),
                         "Jules's answer couldn't be read out here; ask in the app.")
        self.assertIsNotNone(relay.lint("see example.com", []))
        self.assertIsNotNone(relay.lint("order 123456", []))
        self.assertIn("kitchen timers", relay.SAFETY)
        self.assertIn("don't read out passwords, keys, codes, SSNs or card numbers", relay.SAFETY)
        self.assertIn("personal details like addresses, phone numbers or emails", relay.SAFETY)
        off = relay.safety_text(False)
        self.assertIn("kitchen timers", off)
        self.assertIn("don't read out passwords, keys, codes, SSNs or card numbers", off)
        self.assertNotIn("personal details", off)
        self.assertNotIn("shared family room", relay.SAFETY)
        self.assertNotIn("shared family room", off)
        roster = {"jules": {"name": "Jules", "aliases": []}, "hank": {"name": "Hank", "aliases": ["Henry"]}}
        self.assertEqual(relay.addressed_persona(ASK_JULES, roster), ("jules", DINNER))
        self.assertEqual(relay.addressed_persona(DINNER, roster), (None, None))
        self.assertEqual(relay.addressed_persona("Hank, what time is it", roster)[0], "hank")
        with open(relay.WEBHOOKS_FILE, "w") as f:
            json.dump({"jules": {"url": "https://api2.cursor.sh/h", "token": "t"}}, f)
        relay._hooks["mtime"] = None
        self.assertFalse(relay.persona_routed("jules"))  # a webhook alone is not a route
        self._pin("jules")
        self.assertTrue(relay.persona_routed("jules"))
        self.assertFalse(relay.persona_routed("hank"))
        self.assertFalse(relay.persona_routed("grok"))
        self.assertFalse(relay.persona_routed(""))

    def _quiet(self, *parts):
        blob = "\n".join(self.lines)
        for p in parts:
            self.assertNotIn(p, blob)
        if os.path.isdir(relay.ALERT_DIR):
            for name in os.listdir(relay.ALERT_DIR):
                raw = open(os.path.join(relay.ALERT_DIR, name)).read()
                for p in parts:
                    self.assertNotIn(p, raw)

    def _canary(self, fired):
        self.assertEqual(sum(l == "canary persona-route model-output" for l in self.lines), 1 if fired else 0)
        docs = []
        for name in os.listdir(self.alert_dir):
            if name.endswith(".json"):
                docs.append(json.load(open(os.path.join(self.alert_dir, name))))
        got = [d for d in docs if d.get("message") == "canary persona-route model-output"]
        self.assertEqual(got, [])  # logged only; never pushed to ntfy

    def _pin(self, slug="jules"):
        botreply.pin_key(botreply.keys_path(self.tmp), slug, base64.b64encode(b"\x33" * 32).decode())

    async def drive(self, transcript, persona=None, t1=None, function_call=False, tool_first=False, timeout=8,
                    call=None, expect_ack=False, follow=None, slug="jules", timers=None):
        """One station turn against the fake upstream. Returns station events, binary frames and upstream JSON."""
        upstream, events, binaries, order = [], [], [], []
        self.ack_before_post = False
        token = TOKEN
        spec = call
        if spec is None and function_call:
            spec = {"name": "check_notes", "arguments": {"question": TOOL_Q}}

        async def hook(req):
            # The ack is logged before ask_teammate posts. Recorded here, before this handler awaits.
            self.ack_before_post = any(l.startswith("ack clip ") for l in self.lines)
            body = await req.json()
            self.calls.append(body)
            pend_path = os.path.join(relay.PENDING_DIR, body["request_id"] + ".json")
            if os.path.isfile(pend_path):
                self.pendings.append(json.load(open(pend_path)))
            if self.mode == "silent":
                return web.json_response({"ok": True})
            if self.mode == "deny":
                return web.Response(status=401)
            pend = json.load(open(os.path.join(relay.PENDING_DIR, body["request_id"] + ".json")))
            text = self.reply_text

            def write():
                os.makedirs(relay.REPLIES_DIR, exist_ok=True)
                path = os.path.join(relay.REPLIES_DIR, body["request_id"] + ".json")
                if getattr(self, "list_names", None) is not None:
                    # Same merge the sealed list reply does, then the counts file ask_bot_list waits on.
                    counts = relay.merge_imported(list(self.list_names), pend["persona"])
                    if counts.get("empty"):
                        doc = {"request_id": body["request_id"], "agent": pend["persona"], "nonce": pend["nonce"],
                               "action_class": "bot_list", "empty": True}
                    else:
                        doc = {"request_id": body["request_id"], "agent": pend["persona"], "nonce": pend["nonce"],
                               "action_class": "bot_list", "n": counts["n"], "new": counts["new"],
                               "missing": counts["missing"], "dropped": counts["dropped"]}
                    with open(path, "w") as f:
                        json.dump(doc, f)
                    return
                with open(path, "w") as f:
                    json.dump({"request_id": body["request_id"], "agent": pend["persona"], "nonce": pend["nonce"],
                               "tier": 0, "action_class": "answer", "text": text}, f)

            if self.reply_delay:
                async def later():
                    await asyncio.sleep(self.reply_delay)
                    write()
                self._pending_reply = asyncio.get_running_loop().create_task(later())
            else:
                write()
            return web.json_response({"ok": True})

        scripts = [transcript] + ([follow] if follow else [])
        cursor = {"i": 0}

        async def xai(req):
            s = web.WebSocketResponse()
            await s.prepare(req)
            armed = True
            async for m in s:
                if m.type != WSMsgType.TEXT:
                    continue
                ev = json.loads(m.data)
                upstream.append(ev)
                if ev.get("type") == "session.update":
                    await s.send_json({"type": "session.updated"})
                elif ev.get("type") == "input_audio_buffer.clear":
                    armed = True
                elif ev.get("type") == "input_audio_buffer.append" and armed:
                    armed = False
                    i = cursor["i"]
                    if i >= len(scripts):
                        continue
                    cursor["i"] = i + 1
                    said = scripts[i]
                    await s.send_json({"type": "input_audio_buffer.speech_started"})
                    await s.send_json({"type": "input_audio_buffer.speech_stopped"})
                    await s.send_json({"type": "response.created"})
                    late = getattr(self, "late_transcript", None)
                    if late is not None:
                        # Model audio first, then the transcript late (seconds) or never ("never").
                        # The tail runs as a task so this loop keeps reading what the relay sends (the cancel).
                        await s.send_json({"type": "response.output_audio.delta", "delta": AUDIO})
                        await s.send_json({"type": "response.text", "text": PRE})

                        async def tail(late=late, said=said):
                            try:
                                if late == "never":
                                    await asyncio.sleep(1.0)  # the model is still talking when the cap hits
                                else:
                                    await asyncio.sleep(late)
                                    await s.send_json({"type": "conversation.item.input_audio_transcription.completed",
                                                       "transcript": said})
                                    await s.send_json({"type": "response.output_audio.delta", "delta": AUDIO})
                                await s.send_json({"type": "response.done"})
                            except Exception:
                                pass  # the relay already closed the session

                        asyncio.ensure_future(tail())
                        continue
                    if i == 0 and spec and tool_first:
                        await s.send_json({"type": "response.function_call_arguments.done", "name": spec["name"],
                                           "call_id": "c1", "arguments": json.dumps(spec.get("arguments") or {})})
                    await s.send_json({"type": "conversation.item.input_audio_transcription.completed",
                                       "transcript": said})
                    await s.send_json({"type": "response.audio.delta", "delta": AUDIO})
                    await s.send_json({"type": "response.text", "text": PRE})
                    await s.send_json({"type": "response.output_audio.delta", "delta": AUDIO})
                    if i == 0 and spec and not tool_first:
                        await s.send_json({"type": "response.function_call_arguments.done", "name": spec["name"],
                                           "call_id": "c1", "arguments": json.dumps(spec.get("arguments") or {})})
                    await s.send_json({"type": "response.done"})
            return s

        happ = web.Application()
        happ.router.add_post("/hook", hook)
        hs = TestServer(happ)
        xapp = web.Application()
        xapp.router.add_get("/rt", xai)
        xs = TestServer(xapp)
        client = ws = mic = None
        try:
            await hs.start_server()
            url = str(hs.make_url("/hook"))
            with open(relay.WEBHOOKS_FILE, "w") as f:
                json.dump({slug: {"url": url, "token": token}}, f)
            relay._hooks["mtime"] = None
            self._pin(slug)
            await xs.start_server()
            relay.URL = str(xs.make_url("/rt")).replace("http", "ws")
            rapp = web.Application()
            rapp.router.add_get("/ws", relay.session)
            client = TestClient(TestServer(rapp))
            await client.start_server()
            params = {}
            if persona:
                params["persona"] = persona
            if t1 is not None:
                params["t1"] = t1 if isinstance(t1, str) else json.dumps(t1)
            if timers is not None:
                params["timers"] = timers if isinstance(timers, str) else json.dumps(timers)
            ws = await client.ws_connect("/ws", headers={"Authorization": "Bearer test-token"}, params=params)

            async def pump():
                while not ws.closed:
                    await ws.send_bytes(b"\0\0" * 1920)
                    await asyncio.sleep(0.05)

            mic = asyncio.create_task(pump())

            def take(msg):
                if msg.type == WSMsgType.BINARY:
                    order.append("bin")
                    binaries.append(bytes(msg.data))
                elif msg.type == WSMsgType.TEXT:
                    ev = json.loads(msg.data)
                    order.append(ev.get("type"))
                    events.append(ev)

            follow_sent = False

            async def read():
                nonlocal follow_sent
                async for msg in ws:
                    take(msg)
                    if (msg.type == WSMsgType.TEXT and events and events[-1].get("type") == "turn_done"
                            and follow and not follow_sent):
                        follow_sent = True
                        await ws.send_json({"type": "listening"})
                        continue
                    if msg.type == WSMsgType.TEXT and events and events[-1].get("type") in ("turn_done", "done", "timeout", "error"):
                        # The filler is sent before queued model frames are read. Drain so a late forward fails the test.
                        while True:
                            try:
                                nxt = await asyncio.wait_for(ws.receive(), 0.25)
                            except asyncio.TimeoutError:
                                return
                            if nxt.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                                return
                            take(nxt)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        return

            try:
                await asyncio.wait_for(read(), timeout)
            except asyncio.TimeoutError:
                kinds = [e.get("type") for e in events]
                ups = [e.get("type") for e in upstream]
                raise AssertionError(f"no end event in {timeout}s; station={kinds} upstream={ups} log={self.lines[-6:]}")
            if expect_ack and not self.ack_before_post:
                raise AssertionError(f"ack was not logged before the webhook; log={self.lines[-8:]}")
            return {"events": events, "binaries": binaries, "upstream": upstream, "url": url, "order": order}
        finally:
            if mic:
                mic.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await mic
            if ws is not None and not ws.closed:
                await ws.close()
            if client is not None:
                await client.close()
            await xs.close()
            await hs.close()

    def _updates(self, upstream):
        return [e["session"] for e in upstream if e.get("type") == "session.update"]

    def _replies(self, events):
        return [e.get("text", "") for e in events if e.get("type") == "reply"]

    def _acks(self, events):
        return [e for e in events if e.get("type") == "ack"]

    def _assert_no_ack(self, out):
        """0.11.8: a bot's own wake word goes straight to it. No Asking clip, no Asking TTS."""
        self.assertEqual(self._acks(out["events"]), [], [e.get("type") for e in out["events"]])
        self.assertNotIn("ack", out["order"])
        self.assertFalse(any(str(t).startswith("Asking") for t, _ in self.tts))
        self.assertNotIn(MODEL_PCM, out["binaries"])

    def _assert_one_ack(self, out, name="Jules", before_post=True):
        acks = self._acks(out["events"])
        self.assertEqual(len(acks), 1, [e.get("type") for e in out["events"]])
        self.assertEqual(acks[0].get("name"), name)
        self.assertEqual(acks[0].get("slug"), name.lower())
        line = relay.asking_line(name)
        self.assertEqual(base64.b64decode(acks[0]["audio"]), b"TTS:" + line.encode())
        self.assertEqual(out["order"].count("ack"), 1)
        self.assertLess(out["order"].index("speech_stopped"), out["order"].index("ack"))
        self.assertLess(out["order"].index("ack"), out["order"].index("handoff_wait"))
        if "bin" in out["order"]:
            self.assertLess(out["order"].index("ack"), out["order"].index("bin"))
        self.assertNotIn(MODEL_PCM, out["binaries"])
        if before_post:
            self.assertTrue(self.ack_before_post)
        self.assertNotIn(line, self._replies(out["events"]))  # clip, not a reply line that flips the screen
        return acks[0]

    def _assert_routed_silence(self, out):
        types = [e.get("type") for e in out["upstream"]]
        self.assertNotIn("response.create", types)
        blob = json.dumps(out["events"])
        self.assertNotIn(PRE, blob)
        self.assertNotIn("response_started", [e.get("type") for e in out["events"]])
        self.assertNotIn("reply_delta", [e.get("type") for e in out["events"]])
        self.assertNotIn(MODEL_PCM, out["binaries"])
        self.assertTrue(out["binaries"])
        self.assertTrue(all(b.startswith(b"TTS:") for b in out["binaries"]))

    async def test_long_reply_is_not_cut_to_six_seconds(self):
        # 14:55 UTC regression: a 186-char reply was TTS-capped at 6 s and the follow-up opened mid-sentence
        out = await self.drive(DINNER, persona="jules", function_call=True)
        self.assertIn((TACOS, relay.TTS_REPLY_S), self.tts_caps)
        self.assertGreaterEqual(relay.TTS_REPLY_S, 60)
        fillers = [c for t, c in self.tts_caps if t != TACOS]
        self.assertTrue(all(c == relay.TTS_SHORT_S for c in fillers))

    async def test_one_cancel_per_model_response(self):
        # a cancel with no response in flight is xAI invalid_request_error
        out = await self.drive(DINNER, persona="jules", function_call=True)
        types = [e.get("type") for e in out["upstream"]]
        self.assertLessEqual(types.count("response.cancel"), 1)
        self.assertFalse(any("xai error" in l for l in self.lines))

    async def test_wake_route_drops_model_and_speaks_webhook(self):
        out = await self.drive(DINNER, persona="jules", function_call=True, expect_ack=False)
        updates = self._updates(out["upstream"])
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["turn_detection"],
                         {"type": "server_vad", "create_response": False, "interrupt_response": True})
        self.assertEqual(updates[0]["voice"], "ara")
        self.assertIn("don't read out passwords, keys, codes, SSNs or card numbers", updates[0]["instructions"])
        self.assertIn("personal details like addresses, phone numbers or emails", updates[0]["instructions"])
        self.assertNotIn("shared family room", updates[0]["instructions"])
        self._assert_routed_silence(out)
        replies = self._replies(out["events"])
        self._assert_no_ack(out)  # woken as Jules: straight to Jules, no Asking (0.11.8)
        self.assertEqual(replies[-1], TACOS)
        self.assertNotIn("Still asking Jules.", [t for t, _ in self.tts])
        self.assertEqual(self.tts[-1], (TACOS, "ara"))
        self.assertEqual(out["binaries"][-1], b"TTS:" + TACOS.encode())
        self.assertEqual(len(self.calls), 1)  # the tool call must not ask a second time
        self.assertEqual(set(self.calls[0]), {"v", "request_id", "persona", "question", "reply_by"})
        self.assertEqual(self.calls[0]["v"], 1)
        self.assertEqual(self.calls[0]["question"], DINNER)
        self.assertEqual(self.pendings[0]["question"], DINNER)
        self.assertIs(self.pendings[0]["pii_mode"], True)
        self.assertEqual(self.pendings[0]["tier_max"], 0)
        self.assertIs(self.pendings[0]["test"], False)
        self.assertNotIn(TOOL_Q, json.dumps(self.calls))
        self.assertNotIn(TOOL_Q, json.dumps(self.pendings))
        self.assertEqual([e["text"] for e in out["events"] if e["type"] == "heard"], [DINNER])
        self.assertTrue(any(e.get("type") == "handoff_wait" and e.get("name") == "Jules" for e in out["events"]))
        self.assertTrue(all(e.get("direct") is True for e in out["events"] if e.get("type") == "handoff_wait"))
        self.assertTrue(any(e.get("type") == "handoff_done" for e in out["events"]))
        self._canary(True)
        self._quiet(DINNER, TACOS, TOKEN, out["url"], PRE, TOOL_Q)

    async def test_a_slow_reply_does_not_say_still(self):
        """The ack replaces the old cue. A slow reply still does not speak "still asking"; the wait itself is unchanged."""
        self.reply_delay = 0.45
        out = await self.drive(DINNER, persona="jules", expect_ack=False)
        self._assert_routed_silence(out)
        self._assert_no_ack(out)
        self.assertEqual([t for t, _v in self.tts if t.startswith("Still ")], [])
        self.assertFalse(any(e.get("type") == "handoff_still" for e in out["events"]))
        self.assertTrue(any(e.get("type") == "handoff_wait" and e.get("direct") is True for e in out["events"]))
        self.assertEqual(self._replies(out["events"])[-1], TACOS)
        # The reply still arrives inside the wait. Nothing is inserted between the ack and handoff_wait.
        self.assertEqual([t for t in out["order"] if t in ("ack", "handoff_still", "handoff_wait")],
                         ["handoff_wait"])
        self._canary(True)
        self._quiet(DINNER, TACOS, TOKEN, out["url"])

    async def test_check_notes_acks_once(self):
        """check_notes on a bot woken by its own word: no ack (0.11.8)."""
        out = await self.drive(DINNER, persona="jules", function_call=True, tool_first=True, expect_ack=False)
        self._assert_routed_silence(out)
        replies = self._replies(out["events"])
        self._assert_no_ack(out)
        self.assertEqual([t for t, _ in self.tts if t.startswith("Asking ")], [])
        self.assertFalse(any(t.startswith("Still ") for t, _ in self.tts))
        self.assertEqual(replies[-1], TACOS)
        self.assertEqual(self.calls[0]["question"], TOOL_Q)
        self.assertTrue(any(e.get("type") == "handoff_wait" for e in out["events"]))
        self.assertTrue(all(e.get("direct") is True for e in out["events"] if e.get("type") == "handoff_wait"))
        self._quiet(DINNER, TOOL_Q, TACOS, TOKEN, out["url"])

    def _set_pii(self, on):
        relay.MINTED_FILE = os.path.join(self.tmp, "minted", "voice-tokens.json")
        relay._policy.update(mtime=None, pii=True)
        relay.write_policy(bool(on))

    async def test_pii_on_speaks_a_factual_reply_and_blocks_email(self):
        """Guest mode is gone. A factual reply is spoken. An email is blocked only because pii_mode is on."""
        self.reply_text = BEARS
        out = await self.drive(DINNER, persona="jules")
        self._assert_routed_silence(out)
        self.assertEqual(self._replies(out["events"])[-1], BEARS)
        self.assertFalse(any("lint:" in l for l in self.lines))
        self._quiet(BEARS, "$85", DINNER, TOKEN, out["url"])
        relay._limits = handoff.Limits(os.path.join(self.tmp, "limits-email.json"), relay.today)
        self.reply_text = "write ada@example.com please"
        self.lines.clear()
        out = await self.drive(DINNER, persona="jules")
        self._assert_routed_silence(out)
        self.assertEqual(self._replies(out["events"])[-1], relay.handoff_line("Jules", "reject"))
        self.assertNotIn("ada@example.com", json.dumps(out["events"]))
        self.assertTrue(any("verdict=reject (lint:email)" in l for l in self.lines))
        self._quiet("ada@example.com", DINNER, TOKEN, out["url"])

    async def test_pii_off_speaks_email_and_drops_the_personal_clause(self):
        self._set_pii(False)
        self.reply_text = "write ada@example.com please"
        out = await self.drive(DINNER, persona="jules")
        updates = self._updates(out["upstream"])
        self.assertFalse(updates[0]["turn_detection"]["create_response"])
        self.assertIn("don't read out passwords, keys, codes, SSNs or card numbers", updates[0]["instructions"])
        self.assertNotIn("personal details", updates[0]["instructions"])
        self.assertNotIn("shared family room", updates[0]["instructions"])
        self._assert_routed_silence(out)
        self.assertEqual(self._replies(out["events"])[-1], "write ada@example.com please")
        self.assertTrue(any("verdict=spoken" in l for l in self.lines))
        self.assertIs(self.pendings[0]["pii_mode"], False)
        self._quiet("ada@example.com", DINNER, TOKEN, out["url"])

    async def test_pii_off_still_rejects_a_secret(self):
        self._set_pii(False)
        self.reply_text = SECRET
        out = await self.drive("tell me the code", persona="jules")
        self._assert_routed_silence(out)
        self.assertEqual(self._replies(out["events"])[-1], relay.handoff_line("Jules", "reject"))
        self.assertNotIn(SECRET, json.dumps(out["events"]))
        self.assertTrue(any("verdict=reject (lint:secret)" in l for l in self.lines))
        self._quiet(SECRET, "tell me the code", TOKEN, out["url"])

    async def test_post_failure_cues_the_fail_tone_and_turns_the_icon_red(self):
        self.mode = "deny"
        out = await self.drive(DINNER, persona="jules", timeout=6)
        types = [e.get("type") for e in out["events"]]
        self.assertLess(types.index("comms_fail"), types.index("reply"))
        self.assertIn("Couldn't reach Jules", self._replies(out["events"])[-1])
        self.assertEqual(relay.icon_view(), {"paired": True, "ok": False})

    async def test_timeout_speaks_the_fixed_line(self):
        self.mode = "silent"
        relay.REPLY_WAIT = 0.7
        out = await self.drive(DINNER, persona="jules", timeout=6)
        line = "Jules couldn't reply in time."
        self._assert_routed_silence(out)
        self.assertEqual(self._replies(out["events"]), [line])
        types = [e.get("type") for e in out["events"]]
        self.assertLess(types.index("comms_fail"), types.index("reply"))
        self.assertFalse(relay.icon_view()["ok"])
        self._assert_no_ack(out)  # own wake word: no Asking; the timeout line is the reply
        self.assertFalse(any(t.startswith("Still ") for t, _ in self.tts))
        self.assertEqual(self.tts[-1][0], line)
        self.assertNotIn("response.create", [e.get("type") for e in out["upstream"]])
        self._quiet(DINNER, TOKEN, out["url"])

    async def test_spoken_name_routes_mid_session(self):
        out = await self.drive(ASK_JULES, expect_ack=True)  # Grok, until the name
        updates = self._updates(out["upstream"])
        self.assertGreaterEqual(len(updates), 2)
        self.assertTrue(updates[0]["turn_detection"]["create_response"])
        self.assertFalse(updates[1]["turn_detection"]["create_response"])
        self.assertTrue(updates[0]["turn_detection"]["interrupt_response"])
        self.assertTrue(updates[1]["turn_detection"]["interrupt_response"])
        self.assertEqual(updates[1]["voice"], "ara")
        self._assert_routed_silence(out)
        self._assert_one_ack(out)
        self.assertTrue(all(e.get("direct") is False for e in out["events"] if e.get("type") == "handoff_wait"))
        self.assertEqual(self._replies(out["events"])[-1], TACOS)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["question"], DINNER)
        self.assertEqual(set(self.calls[0]), {"v", "request_id", "persona", "question", "reply_by"})
        self.assertEqual(self.calls[0]["persona"], "jules")
        self.assertEqual(self.pendings[0]["question"], DINNER)
        self.assertTrue(any(l == "persona route -> jules" for l in self.lines))
        self._canary(True)  # response.created was buffered, then counted and dropped
        self._quiet(DINNER, ASK_JULES, TACOS, TOKEN, out["url"], PRE)

    async def test_single_wake_word_hands_off_to_a_bot(self):
        """Single device: only "Hey Grok" is enabled. "Hey Grok, ask Jules X" still hands off to Jules."""
        out = await self.drive(ASK_JULES, persona="grok", expect_ack=True)
        self._assert_routed_silence(out)
        self._assert_one_ack(out)
        self.assertEqual(self.calls[0]["persona"], "jules")
        self.assertEqual(self.pendings[0]["question"], DINNER)
        self.assertEqual(self._replies(out["events"])[-1], TACOS)

    async def test_persona_without_a_webhook_keeps_the_model(self):
        out = await self.drive("what time is it", persona="hank")
        updates = self._updates(out["upstream"])
        self.assertTrue(updates[0]["turn_detection"]["create_response"])
        self.assertTrue(updates[0]["turn_detection"]["interrupt_response"])
        self.assertIn(PRE, self._replies(out["events"]))
        self.assertIn(MODEL_PCM, out["binaries"])
        self.assertFalse(any(t.startswith("Asking ") for t, _ in self.tts))
        self.assertEqual(self._acks(out["events"]), [])
        self.assertEqual(self.calls, [])
        self.assertNotIn("response.create", [e.get("type") for e in out["upstream"]])
        self._canary(False)

    async def test_grok_keeps_the_model(self):
        out = await self.drive("what time is it")
        updates = self._updates(out["upstream"])
        self.assertTrue(updates[0]["turn_detection"]["create_response"])
        self.assertIn(PRE, self._replies(out["events"]))
        self.assertTrue(all(b == MODEL_PCM for b in out["binaries"]))
        self.assertEqual(self.calls, [])
        self.assertFalse(any("Asking " in (e.get("text") or "") for e in out["events"]))
        self.assertEqual(self._acks(out["events"]), [])
        self._canary(False)

    async def test_talk_to_route_acks_once(self):
        """The talk_to tool is a route. The ack is at the top of the handoff, before the webhook."""
        out = await self.drive(
            "what's for dinner",
            call={"name": "talk_to", "arguments": {"persona": "jules", "question": DINNER}},
            tool_first=True, expect_ack=True)
        self._assert_routed_silence(out)
        self._assert_one_ack(out)
        self.assertEqual(self.calls[0]["persona"], "jules")
        self.assertEqual(self.calls[0]["question"], DINNER)
        self.assertTrue(all(e.get("direct") is True for e in out["events"] if e.get("type") == "handoff_wait"))
        self.assertNotIn("response.create", [e.get("type") for e in out["upstream"]])

    async def test_limit_and_direct_cap_do_not_ack(self):
        """A handoff that will not wait (cap) does not say Asking, including a woken persona."""
        relay._limits.take("jules", "already-out")
        out = await self.drive(ASK_JULES)
        self.assertEqual(self._acks(out["events"]), [])
        self.assertFalse(any(str(t).startswith("Asking") for t, _ in self.tts))
        self.assertEqual(self.calls, [])
        self.assertIn("lot of questions", self._replies(out["events"])[-1])
        relay._limits = handoff.Limits(os.path.join(self.tmp, "limits-direct.json"), relay.today)
        relay._limits.take("jules", "already-out")
        self.tts.clear()
        self.calls.clear()
        out = await self.drive(DINNER, persona="jules")
        self.assertEqual(self._acks(out["events"]), [])
        self.assertFalse(any(str(t).startswith("Asking") for t, _ in self.tts))
        self.assertEqual(self.calls, [])

    async def test_cached_clip_is_not_rendered_again(self):
        relay.write_ack_cache("jules", "Jules", "ara", b"CACHED-PCM")
        out = await self.drive(ASK_JULES, expect_ack=True)  # a Grok route still acks
        self.assertFalse(any(str(t).startswith("Asking") for t, _ in self.tts))
        self.assertEqual(base64.b64decode(self._acks(out["events"])[0]["audio"]), b"CACHED-PCM")
        self.assertEqual(self._replies(out["events"])[-1], TACOS)
        self.assertLess(out["order"].index("ack"), out["order"].index("bin"))

    async def test_ack_tts_failure_still_hands_off(self):
        self.fail_asking = True
        out = await self.drive(DINNER, persona="jules")
        self.assertEqual(self._acks(out["events"]), [])
        self.assertFalse(self.ack_before_post)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["question"], DINNER)
        self.assertEqual(self._replies(out["events"])[-1], TACOS)
        self.assertEqual(os.listdir(relay.ACK_DIR), [])

    async def test_goodbye_to_a_bot_does_not_ack(self):
        """After a handoff the bot stays on, but a goodbye ends the session. It never says Asking or reaches the bot."""
        out = await self.drive("okay, goodbye", persona="jules")
        self.assertEqual(self._acks(out["events"]), [])
        self.assertFalse(any(str(t).startswith("Asking") for t, _ in self.tts))
        self.assertEqual(self.calls, [])
        self.assertEqual(out["events"][-1].get("type"), "done")

    async def test_local_exit_on_wake_does_not_call_the_bot(self):
        """A bare goodbye or stop after a persona wake ends locally. No heard flash, no webhook."""
        with open(relay.PERSONAS_FILE, "w") as f:
            json.dump({"personas": [
                {"slug": "rhea", "name": "Rhea", "voice": "ara", "prompt": "Rhea helps."},
            ]}, f)
        relay._personas["mtime"] = None
        for said in ("goodbye", "Hey Rhea, goodbye", "stop"):
            self.calls.clear()
            self.tts.clear()
            out = await self.drive(said, persona="rhea", slug="rhea", timers=[])
            self.assertEqual(self.calls, [], said)
            kinds = [e.get("type") for e in out["events"]]
            self.assertNotIn("heard", kinds, said)
            self.assertNotIn("response_started", kinds, said)
            self.assertEqual(out["events"][-1].get("type"), "done", said)
            self.assertIs(out["events"][-1].get("local"), True, said)
            self.assertEqual(self._acks(out["events"]), [])
            self.assertFalse(any(str(t).startswith("Asking") for t, _ in self.tts), said)

    async def _held(self, late):
        old = (relay.HOLD_CAP_S, relay.NO_SPEECH)
        relay.HOLD_CAP_S, relay.NO_SPEECH = 0.3, 1.5
        self.late_transcript = late
        try:
            return await self.drive("what is the weather", timeout=8)
        finally:
            relay.HOLD_CAP_S, relay.NO_SPEECH = old
            self.late_transcript = None

    async def test_held_reply_is_dropped_when_no_transcript_comes(self):
        """Nix (d): a held reply never plays without its transcript. Cap expiry drops it."""
        out = await self._held("never")
        self.assertEqual(out["binaries"], [])
        kinds = [e.get("type") for e in out["events"]]
        self.assertNotIn("response_started", kinds)
        self.assertEqual(kinds[-1], "timeout")
        self.assertTrue(any("hold cap: dropping" in l for l in self.lines), self.lines[-8:])
        self.assertIn("response.cancel", [e.get("type") for e in out["upstream"]])

    async def test_late_transcript_does_not_replay_dropped_audio(self):
        out = await self._held(1.0)  # transcript arrives 0.7 s after the cap
        self.assertEqual(out["binaries"], [])
        kinds = [e.get("type") for e in out["events"]]
        self.assertNotIn("response_started", kinds)
        self.assertNotIn("heard", kinds)  # the session ended at the cap; the late words go nowhere
        self.assertEqual(kinds[-1], "timeout")

    async def test_grok_noise_does_not_ack(self):
        """No transcript worth routing: Grok does not say Asking. Nor does a woken bot."""
        out = await self.drive("")
        self.assertEqual(self._acks(out["events"]), [])
        self.assertEqual(self.calls, [])


_KAI = [{"slug": "kai", "name": "Kai", "aliases": []},
        {"slug": "mira", "name": "Mira", "aliases": [], "main": True},
        {"slug": "sol", "name": "Sol", "aliases": []}]


class ConnectPhrase(unittest.TestCase):
    def test_variants_match_and_other_sentences_pass_through(self):
        for phrase in (
            "Hey Grok, can you connect me with Kai?",
            "hey grok can you connect me with kai",
            "HEY GROK, Could you connect me with Kai.",
            "please connect me to Kai",
            "can you please connect me with Kai!",
            "put me through to Kai",
            "Hey Grok put me through to kai?",
        ):
            hit = relay.connect_request(phrase, _KAI)
            self.assertEqual(hit["kind"], "switch", phrase)
            self.assertEqual(hit["slug"], "kai", phrase)
            self.assertEqual(hit["lines"][0][0], "Connecting you with Kai.")
            self.assertEqual(hit["lines"][1], ("Hey, this is Kai. How can I help you?", "bot"))
        for phrase in ("what's for dinner", "ask Kai what's for dinner", "connect the printer",
                       "can you help me", "put me through", ""):
            self.assertIsNone(relay.connect_request(phrase, _KAI), phrase)

    def test_unknown_lists_bots_and_an_empty_station_says_so(self):
        hit = relay.connect_request("connect me with Remy", _KAI)
        self.assertEqual(hit["kind"], "unknown")
        self.assertEqual(hit["lines"][0][0],
                         "I don't see a bot called Remy. Your bots are Kai, Mira and Sol.")
        self.assertIsNone(relay.connect_request("what time is it", []))
        empty = relay.connect_request("Hey Grok, connect me with Sol", [])
        self.assertEqual(empty["lines"][0][0],
                         "I don't see a bot called Sol. You don't have any other bots yet.")

    def test_grok_and_main_bot(self):
        same = relay.connect_request("connect me with Grok", _KAI, current="grok")
        self.assertEqual(same["kind"], "same")
        self.assertEqual(same["lines"][0][0], "You're already talking to Grok.")
        back = relay.connect_request("Hey Grok, can you connect me with Grok?", _KAI, current="kai")
        self.assertEqual(back["kind"], "switch")
        self.assertEqual(back["slug"], "grok")
        self.assertEqual(back["lines"][1][1], "current")
        main = relay.connect_request("put me through to the main bot", _KAI)
        self.assertEqual(main["slug"], "mira")
        none = relay.connect_request("connect me with the main bot",
                                     [{"slug": "kai", "name": "Kai"}])
        self.assertIn("I don't see a main bot.", none["lines"][0][0])
        self.assertIn("Your bots are Kai.", none["lines"][0][0])
        already = relay.connect_request("connect me to Kai", _KAI, current="kai")
        self.assertEqual(already["kind"], "same")
        self.assertEqual(already["lines"][0][0], "You're already talking to Kai.")


class ImportTeamPhrase(unittest.TestCase):
    def test_team_phrases_match_and_other_sentences_do_not(self):
        for phrase in (
            "Hey Grok, import the rest of my team",
            "hey grok, import the rest of my team",
            "import my team",
            "import the rest of my bots",
            "Hey Grok, can you import the rest of my team?",
            "please import my team",
            "could you please import the rest of my bots!",
            "import my team please",
            "Hey Rhea, import the rest of my team",
            "hey kai import my team",
            "Hey Grok, update my team list",
            "hey grok, update my team list",
            "update my team list",
            "Hey Grok, can you update my team list?",
            "please update my team list",
            "Hey Rhea, update my team list",
            "could you please update my team list!",
            "update my team list please",
            "hey kai update my team list",
        ):
            self.assertTrue(relay.import_team_request(phrase), phrase)
        for phrase in (
            "import my teammates' notes",
            "can you import the file",
            "import the rest of my team now",
            "update my team list now",
            "update my teammates' list",
            "update the team list",
            "connect me with Kai",
            "what's for dinner",
            "",
        ):
            self.assertFalse(relay.import_team_request(phrase), phrase)


class ConnectSession(PersonaRoute):
    def _kai_roster(self):
        with open(relay.PERSONAS_FILE, "w") as f:
            json.dump({"personas": [
                {"slug": "kai", "name": "Kai", "voice": "ara", "prompt": "Kai helps."},
                {"slug": "mira", "name": "Mira", "voice": "rex", "prompt": "Mira helps."},
            ]}, f)
        relay._personas["mtime"] = None

    async def test_connect_switches_and_the_followup_goes_to_that_bot(self):
        self._kai_roster()
        out = await self.drive("Hey Grok, can you connect me with Kai?", follow="what time is it", slug="kai")
        replies = self._replies(out["events"])
        self.assertIn("Connecting you with Kai.", replies)
        self.assertIn("Hey, this is Kai. How can I help you?", replies)
        self.assertEqual([(t, v) for t, v in self.tts if t.startswith("Hey, this is Kai")],
                         [("Hey, this is Kai. How can I help you?", "ara")])
        personas = [e for e in out["events"] if e.get("type") == "persona"]
        self.assertTrue(any(e.get("slug") == "kai" and e.get("name") == "Kai" for e in personas))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["persona"], "kai")
        self.assertEqual(self.calls[0]["question"], "what time is it")
        self.assertNotIn(MODEL_PCM, out["binaries"])
        self.assertTrue(any(e.get("type") == "session.update" and e.get("session", {}).get("voice") == "ara"
                            for e in out["upstream"]))

    async def test_unknown_connect_is_spoken_and_does_not_ask_a_bot(self):
        self._kai_roster()
        out = await self.drive("can you connect me with Sol?")
        self.assertEqual(self.calls, [])
        self.assertIn("I don't see a bot called Sol. Your bots are Kai and Mira.", self._replies(out["events"]))
        self.assertFalse(any(e.get("type") == "persona" and e.get("slug") == "sol" for e in out["events"]))


class ImportTeamSession(PersonaRoute):
    def _mira_main(self):
        with open(relay.PERSONAS_FILE, "w") as f:
            json.dump({"personas": [
                {"slug": "mira", "name": "Mira", "voice": "rex", "prompt": "Mira helps."},
            ]}, f)
        relay._personas["mtime"] = None
        with open(os.path.join(os.path.dirname(relay.WEBHOOKS_FILE), "personas-overlay.json"), "w") as f:
            json.dump({"list_source": "mira"}, f)

    async def test_import_asks_the_main_bot_and_does_not_speak_names(self):
        self._mira_main()
        self.list_names = ["Kai", "Noor"]
        out = await self.drive("Hey Grok, import the rest of my team", slug="mira")
        replies = self._replies(out["events"])
        self.assertIn("Asking Mira for your team.", replies)
        self.assertIn("Mira sent 2 bots: 2 new, 0 missing. Finish setup on the admin page.", replies)
        self.assertEqual(self.calls[0]["kind"], "bot_list")
        self.assertEqual(self.calls[0]["question"], "list bots")
        self.assertEqual(self.calls[0]["persona"], "mira")
        stored = json.load(open(relay.imported_path()))
        self.assertEqual(stored["rows"]["kai"]["state"], "new")
        self.assertEqual(stored["rows"]["noor"]["state"], "new")
        spoken = "\n".join(replies)
        self.assertNotIn("Kai", spoken)
        self.assertNotIn("Noor", spoken)
        self.assertNotIn("Kai", "\n".join(self.lines))
        self.assertNotIn("Noor", "\n".join(self.lines))

    async def test_import_through_a_non_main_bot_wake_word(self):
        """Item 7: "Hey Rhea, import the rest of my team" asks the main bot, not Rhea."""
        with open(relay.PERSONAS_FILE, "w") as f:
            json.dump({"personas": [
                {"slug": "mira", "name": "Mira", "voice": "rex", "prompt": "Mira helps."},
                {"slug": "rhea", "name": "Rhea", "voice": "ara", "prompt": "Rhea helps."},
            ]}, f)
        relay._personas["mtime"] = None
        with open(os.path.join(os.path.dirname(relay.WEBHOOKS_FILE), "personas-overlay.json"), "w") as f:
            json.dump({"list_source": "mira"}, f)
        self.list_names = ["Kai"]
        for said in ("import the rest of my team",):  # one import per test: the relay rate-limits repeats
            self.calls.clear()
            out = await self.drive(said, persona="rhea", slug="mira")
            self.assertIn("Asking Mira for your team.", self._replies(out["events"]), said)
            self.assertEqual([c["kind"] for c in self.calls], ["bot_list"], said)
            self.assertEqual(self.calls[0]["persona"], "mira", said)

    async def test_import_without_a_main_bot_does_not_post(self):
        out = await self.drive("import my team")
        self.assertIn("Set up your main bot first.", self._replies(out["events"]))
        self.assertEqual(self.calls, [])

    async def test_import_the_rest_of_my_bots(self):
        self._mira_main()
        self.list_names = ["Kai"]
        out = await self.drive("import the rest of my bots", slug="mira")
        self.assertIn("Asking Mira for your team.", self._replies(out["events"]))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["kind"], "bot_list")


if __name__ == "__main__":
    unittest.main()
