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
            "PENDING_DIR", "REPLIES_DIR", "STILL_AFTER", "REPLY_WAIT", "ALERT_DIR", "_limits",
            "MINTED_FILE")}
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
        relay.STILL_AFTER = 20.0
        relay.PERSONAS_FILE = os.path.join(self.tmp, "personas.json")
        relay.WEBHOOKS_FILE = os.path.join(self.tmp, "webhooks.json")
        relay.PENDING_DIR = os.path.join(self.tmp, "pending")
        relay.REPLIES_DIR = os.path.join(self.tmp, "replies")
        os.makedirs(relay.PENDING_DIR)
        os.makedirs(relay.REPLIES_DIR)
        with open(relay.PERSONAS_FILE, "w") as f:
            json.dump(_personas_doc(), f)
        relay.VOICES = {"ara", "rex", "luna"}
        relay._personas["mtime"] = None
        relay._hooks["mtime"] = None
        relay._hooks["map"] = {}
        relay._limits = handoff.Limits(os.path.join(self.tmp, "limits.json"), relay.today)
        relay.log = lambda *a: self.lines.append(" ".join(map(str, a)))

        self.tts_caps = []

        async def fake_tts(http, text, voice, max_s=relay.TTS_SHORT_S):
            self.tts.append((text, voice))
            self.tts_caps.append((text, max_s))
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
        self.assertEqual(relay.handoff_line("Jules", "timeout"),
                         "Jules didn't answer in time; try again or ask in the app.")
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

    async def drive(self, transcript, persona=None, t1=None, function_call=False, timeout=8):
        """One station turn against the fake upstream. Returns station events, binary frames and upstream JSON."""
        upstream, events, binaries = [], [], []
        token = TOKEN

        async def hook(req):
            body = await req.json()
            self.calls.append(body)
            pend_path = os.path.join(relay.PENDING_DIR, body["request_id"] + ".json")
            if os.path.isfile(pend_path):
                self.pendings.append(json.load(open(pend_path)))
            if self.mode == "silent":
                return web.json_response({"ok": True})
            pend = json.load(open(os.path.join(relay.PENDING_DIR, body["request_id"] + ".json")))
            text = self.reply_text

            def write():
                os.makedirs(relay.REPLIES_DIR, exist_ok=True)
                with open(os.path.join(relay.REPLIES_DIR, body["request_id"] + ".json"), "w") as f:
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
                    await s.send_json({"type": "input_audio_buffer.speech_started"})
                    await s.send_json({"type": "input_audio_buffer.speech_stopped"})
                    await s.send_json({"type": "response.created"})
                    await s.send_json({"type": "conversation.item.input_audio_transcription.completed",
                                       "transcript": transcript})
                    await s.send_json({"type": "response.audio.delta", "delta": AUDIO})
                    await s.send_json({"type": "response.text", "text": PRE})
                    await s.send_json({"type": "response.output_audio.delta", "delta": AUDIO})
                    if function_call:
                        await s.send_json({"type": "response.function_call_arguments.done", "name": "check_notes",
                                           "call_id": "c1", "arguments": json.dumps({"question": TOOL_Q})})
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
                json.dump({"jules": {"url": url, "token": token}}, f)
            relay._hooks["mtime"] = None
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
            ws = await client.ws_connect("/ws", headers={"Authorization": "Bearer test-token"}, params=params)

            async def pump():
                while not ws.closed:
                    await ws.send_bytes(b"\0\0" * 1920)
                    await asyncio.sleep(0.05)

            mic = asyncio.create_task(pump())

            def take(msg):
                if msg.type == WSMsgType.BINARY:
                    binaries.append(bytes(msg.data))
                elif msg.type == WSMsgType.TEXT:
                    events.append(json.loads(msg.data))

            async def read():
                async for msg in ws:
                    take(msg)
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
            return {"events": events, "binaries": binaries, "upstream": upstream, "url": url}
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
        out = await self.drive(DINNER, persona="jules", function_call=True)
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
        self.assertNotIn("Asking Jules\u2026", replies)  # woken as Jules: no filler
        self.assertEqual(replies[-1], TACOS)
        self.assertNotIn("Still asking Jules.", [t for t, _ in self.tts])
        self.assertEqual(self.tts[-1], (TACOS, "ara"))
        self.assertEqual(out["binaries"][-1], b"TTS:" + TACOS.encode())
        self.assertEqual(len(self.calls), 1)  # the tool call must not ask a second time
        self.assertEqual(set(self.calls[0]), {"source", "v", "request_id", "persona"})
        self.assertEqual(self.calls[0]["v"], 2)
        self.assertNotIn("question", self.calls[0])
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

    async def test_still_asking_then_the_webhook_reply(self):
        self.reply_delay = 0.45
        relay.STILL_AFTER = 0.15
        out = await self.drive(DINNER, persona="jules")
        self._assert_routed_silence(out)
        self.assertEqual([t for t, v in self.tts], ["Still working on it.", TACOS])
        self.assertTrue(all(v == "ara" for _, v in self.tts))
        still = [e for e in out["events"] if e.get("type") == "handoff_still"]
        self.assertEqual(len(still), 1)
        self.assertEqual(still[0]["name"], "Jules")
        self.assertEqual(base64.b64decode(still[0]["audio"]), b"TTS:Still working on it.")
        self.assertEqual(self._replies(out["events"])[-1], TACOS)
        self._canary(True)
        self._quiet(DINNER, TACOS, TOKEN, out["url"])

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

    async def test_timeout_speaks_the_fixed_line(self):
        self.mode = "silent"
        relay.REPLY_WAIT = 0.7
        relay.STILL_AFTER = 0.15
        out = await self.drive(DINNER, persona="jules", timeout=6)
        line = "Jules didn't answer in time; try again or ask in the app."
        self._assert_routed_silence(out)
        self.assertEqual(self._replies(out["events"]), [line])
        self.assertIn("Still working on it.", [t for t, _ in self.tts])
        self.assertEqual(self.tts[-1][0], line)
        self.assertNotIn("response.create", [e.get("type") for e in out["upstream"]])
        self._quiet(DINNER, TOKEN, out["url"])

    async def test_spoken_name_routes_mid_session(self):
        out = await self.drive(ASK_JULES)  # Grok, until the name
        updates = self._updates(out["upstream"])
        self.assertGreaterEqual(len(updates), 2)
        self.assertTrue(updates[0]["turn_detection"]["create_response"])
        self.assertFalse(updates[1]["turn_detection"]["create_response"])
        self.assertTrue(updates[0]["turn_detection"]["interrupt_response"])
        self.assertTrue(updates[1]["turn_detection"]["interrupt_response"])
        self.assertEqual(updates[1]["voice"], "ara")
        self._assert_routed_silence(out)
        self.assertEqual(self._replies(out["events"])[0], "Asking Jules\u2026")
        self.assertTrue(all(e.get("direct") is False for e in out["events"] if e.get("type") == "handoff_wait"))
        self.assertEqual(self._replies(out["events"])[-1], TACOS)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("question", self.calls[0])
        self.assertEqual(self.calls[0]["persona"], "jules")
        self.assertEqual(self.pendings[0]["question"], DINNER)
        self.assertTrue(any(l == "persona route -> jules" for l in self.lines))
        self._canary(True)  # response.created was buffered, then counted and dropped
        self._quiet(DINNER, ASK_JULES, TACOS, TOKEN, out["url"], PRE)

    async def test_single_wake_word_hands_off_to_a_bot(self):
        """Single device: only "Hey Grok" is enabled. "Hey Grok, ask Jules X" still hands off to Jules."""
        out = await self.drive(ASK_JULES, persona="grok")
        self._assert_routed_silence(out)
        self.assertEqual(self._replies(out["events"])[0], "Asking Jules\u2026")
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
        self._canary(False)


if __name__ == "__main__":
    unittest.main()
