"""0.13.45: a punctuated yes starts the offered timer, and a spoken timer never goes to the bot.

These cases return None or hand off on 7a5625c. REPLY_WAIT stays 180.
"""
import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("RELAY_TOKEN", "test-token")
os.environ.setdefault("XAI_API_KEY", "test-key")
os.environ.setdefault("NO_SPEECH", "1.0")
_TMP = tempfile.mkdtemp()
os.environ.setdefault("ALERT_DIR", os.path.join(_TMP, "alerts"))
os.environ.setdefault("PERSONAS_FILE", os.path.join(_TMP, "personas.json"))
sys.path.insert(0, os.path.join(ROOT, "relay"))
sys.path.insert(0, os.path.join(ROOT, "wake"))
import botreply  # noqa: E402
import cards  # noqa: E402
import handoff  # noqa: E402
import grok_station as gs  # noqa: E402
import grok_wake as gw  # noqa: E402
import relay  # noqa: E402


YES_LINES = (
    "Yes, please.",
    "Okay, please.",
    "Yes, please. Thank you.",
    "Sure, go ahead.",
    "Yeah, set it.",
    "Start it.",
    "Yes please set a timer.",
    "Yes, set a timer for the mac and cheese.",
)


class OfferWords(unittest.TestCase):
    def test_punctuated_yes_is_yes(self):
        for line in YES_LINES:
            self.assertEqual(cards.offer_answer(line), "yes", line)
        self.assertEqual(cards.offer_answer("yes please set a timer for the soup"), "yes")

    def test_no_thanks_is_no_and_a_command_is_not_an_answer(self):
        self.assertEqual(cards.offer_answer("No, thanks."), "no")
        self.assertIsNone(cards.offer_answer("next step"))
        self.assertIsNone(cards.offer_answer("no, set a timer for the pasta"))
        self.assertIsNone(cards.local_timer_command("no, set a timer for the pasta"))

    def test_a_yes_that_names_a_length_stays_yes(self):
        self.assertEqual(cards.offer_answer("yes, make it 12 minutes"), "yes")
        st = {"recipe_offer": {"seconds": 600, "minutes": 10, "label": "Orzo", "until": 1000},
              "recipe": {"id": "orzo01", "step": 1}}
        self.assertEqual(relay.classify_offer_reply("yes, make it 12 minutes", st, 100), "yes")
        self.assertEqual(st["recipe_offer"]["seconds"], 600)
        self.assertEqual(st["recipe_offer"]["minutes"], 10)

    def test_a_comma_before_please_still_sets_the_timer(self):
        for line in ("Set a timer for 10 minutes, please", "Set a timer for 10 minutes, please.",
                     "Start a timer for 30 seconds, please"):
            hit = cards.local_timer_command(line)
            self.assertIsNotNone(hit, line)
            self.assertIn(hit["seconds"], (600, 30), line)

    def test_the_yes_window_is_15_seconds_and_the_wait_stays_180(self):
        self.assertEqual(relay.OFFER_WINDOW_S, 15)
        with open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('REPLY_WAIT = float(E("REPLY_WAIT", "180"))', src)


def _step(_cmd):
    return {"ok": True, "say": "Boil the orzo for 10 minutes.", "closed": False,
            "bot": "jules", "id": "orzo01", "step": 1, "dish": "Orzo"}


class JulesTimer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lines = []
        self.calls = []
        self.snap = {k: getattr(relay, k) for k in (
            "HANDOFF", "URL", "log", "tts_pcm", "PERSONAS_FILE", "WEBHOOKS_FILE",
            "PENDING_DIR", "REPLIES_DIR", "REPLY_WAIT", "post_recipe_cmd")}
        self.limits = relay._limits
        self.hooks = {"mtime": relay._hooks.get("mtime"), "map": dict(relay._hooks.get("map") or {})}
        self.personas = dict(relay._personas)
        relay.HANDOFF = True
        relay.REPLY_WAIT = self.snap["REPLY_WAIT"]
        relay.PERSONAS_FILE = os.path.join(self.tmp, "personas.json")
        relay.WEBHOOKS_FILE = os.path.join(self.tmp, "webhooks.json")
        relay.PENDING_DIR = os.path.join(self.tmp, "pending")
        relay.REPLIES_DIR = os.path.join(self.tmp, "replies")
        os.makedirs(relay.PENDING_DIR)
        os.makedirs(relay.REPLIES_DIR)
        with open(relay.PERSONAS_FILE, "w") as fh:
            json.dump({"personas": [
                {"slug": "jules", "name": "Jules", "voice": "ara", "prompt": "The notes helper."},
            ]}, fh)
        relay._personas["mtime"] = None
        relay._hooks["mtime"] = None
        relay._hooks["map"] = {}
        relay._limits = handoff.Limits(os.path.join(self.tmp, "limits.json"), relay.today)
        relay.log = lambda *a: self.lines.append(" ".join(map(str, a)))
        relay.tts_pcm = self._no_tts
        botreply.pin_key(relay.bot_keys_path(), "jules", base64.b64encode(b"\x33" * 32).decode())
        relay.stats["busy"] = 0
        relay.reset_offered_steps()
        os.environ["GROK_TIMER_STATE"] = os.path.join(self.tmp, "timers.json")

    async def asyncTearDown(self):
        for k, v in self.snap.items():
            setattr(relay, k, v)
        relay._limits = self.limits
        relay._hooks.clear()
        relay._hooks.update(self.hooks)
        relay._personas.clear()
        relay._personas.update(self.personas)
        relay.stats["busy"] = 0

    async def _no_tts(self, *args, **kwargs):
        return None

    async def _run(self, lines, *, answer=True):
        from aiohttp import web, WSMsgType
        from aiohttp.test_utils import TestClient, TestServer

        posted = []
        real_post = relay.post_recipe_cmd

        def post(cmd):
            posted.append(cmd)
            return _step(cmd)

        relay.post_recipe_cmd = post
        calls = self.calls
        marks = {}

        async def hook(req):
            body = await req.json()
            calls.append(body)
            if not marks.get("asked"):
                marks["asked"] = time.monotonic()
            if not answer:
                return web.json_response({"ok": True})
            with open(os.path.join(relay.PENDING_DIR, body["request_id"] + ".json")) as fh:
                pend = json.load(fh)
            path = os.path.join(relay.REPLIES_DIR, body["request_id"] + ".json")
            with open(path, "w") as fh:
                json.dump({"request_id": body["request_id"], "agent": pend["persona"], "nonce": pend["nonce"],
                           "tier": 0, "action_class": "answer", "text": "On it."}, fh)
            return web.json_response({"ok": True})

        xapp = web.Application()
        pending = list(lines)

        async def xai(req):
            s = web.WebSocketResponse()
            await s.prepare(req)
            armed = True
            async for m in s:
                if m.type != WSMsgType.TEXT:
                    continue
                ev = json.loads(m.data)
                if ev.get("type") == "session.update":
                    await s.send_json({"type": "session.updated"})
                elif ev.get("type") == "input_audio_buffer.clear":
                    armed = True
                elif ev.get("type") == "input_audio_buffer.append" and armed and pending:
                    armed = False
                    heard = pending.pop(0)
                    await s.send_json({"type": "input_audio_buffer.speech_started"})
                    await s.send_json({"type": "input_audio_buffer.speech_stopped"})
                    await s.send_json({"type": "response.created"})
                    await s.send_json({"type": "conversation.item.input_audio_transcription.completed",
                                       "transcript": heard})
                    await s.send_json({"type": "response.done"})
            return s

        xapp.router.add_get("/rt", xai)
        happ = web.Application()
        happ.router.add_post("/hook", hook)
        hs = TestServer(happ)
        xs = TestServer(xapp)
        client = ws = mic = None
        events = []
        try:
            await hs.start_server()
            with open(relay.WEBHOOKS_FILE, "w") as fh:
                json.dump({"jules": {"url": str(hs.make_url("/hook")), "token": "hook-token"}}, fh)
            relay._hooks["mtime"] = None
            await xs.start_server()
            relay.URL = str(xs.make_url("/rt")).replace("http", "ws")
            rapp = web.Application()
            rapp.router.add_get("/ws", relay.session)
            client = TestClient(TestServer(rapp))
            await client.start_server()
            t0 = time.monotonic()
            ws = await client.ws_connect("/ws", headers={"Authorization": "Bearer test-token"},
                                         params={"persona": "jules", "timers": "[]"})

            async def pump():
                while not ws.closed:
                    await ws.send_bytes(b"\0\0" * 1920)
                    await asyncio.sleep(0.05)

            mic = asyncio.create_task(pump())
            turns = 0
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                ev = json.loads(msg.data)
                ev["_at"] = time.monotonic()
                events.append(ev)
                if ev.get("type") == "turn_done":
                    turns += 1
                    if turns >= len(lines):
                        break
                    await ws.send_json({"type": "listening"})
                elif ev.get("type") in ("done", "timeout", "error"):
                    break
        finally:
            relay.post_recipe_cmd = real_post
            if mic:
                mic.cancel()
            if ws is not None and not ws.closed:
                await ws.close()
            if client is not None:
                await client.close()
            await xs.close()
            await hs.close()
            relay.stats["busy"] = 0
        return events, posted, t0, marks

    def _sets(self, events):
        return [e for e in events if e.get("type") == "timer_set"]

    async def test_yes_please_sets_the_offer_and_does_not_ask_jules(self):
        events, posted, _t0, _marks = await self._run(["next step", "Yes, please."])
        sets = self._sets(events)
        self.assertEqual(len(sets), 1, events)
        self.assertEqual(sets[0]["seconds"], 600)
        self.assertTrue(any(ln == "recipe offer yes" for ln in self.lines), self.lines)
        self.assertEqual(self.calls, [])
        self.assertFalse(any(e.get("type") == "heard" for e in events))
        self.assertEqual(posted, ["next"])

    async def test_set_a_timer_is_local_for_a_webhook_persona(self):
        events, _posted, _t0, _marks = await self._run(["Set a timer for 10 minutes."])
        sets = self._sets(events)
        self.assertEqual(len(sets), 1, events)
        self.assertEqual(sets[0]["seconds"], 600)
        self.assertTrue(any(ln == "timer local" for ln in self.lines), self.lines)
        self.assertFalse(any("for 10 minutes" in ln for ln in self.lines))
        self.assertEqual(self.calls, [])
        self.assertFalse(any(e.get("type") == "heard" for e in events))

    async def test_an_unrelated_question_during_the_offer_still_hands_off(self):
        events, _posted, _t0, _marks = await self._run(["next step", "what's for dinner"])
        self.assertEqual(len(self.calls), 1, self.lines)
        self.assertEqual(self.calls[0]["persona"], "jules")
        self.assertEqual(self.calls[0]["question"], "what's for dinner")
        self.assertFalse(any(e.get("type") == "timer_set" for e in events))

    async def test_a_silent_bot_times_out_and_the_display_leaves_thinking(self):
        relay.REPLY_WAIT = 0.6
        events, _posted, t0, marks = await self._run(["what's for dinner"], answer=False)
        replies = [e.get("text") for e in events if e.get("type") == "reply"]
        self.assertEqual(replies, ["Jules couldn't reply in time."])
        types = [e.get("type") for e in events]
        self.assertIn("handoff_wait", types)
        self.assertIn("turn_done", types)
        done = next(e for e in events if e.get("type") == "turn_done")
        asked = marks.get("asked", t0)
        self.assertLess(done["_at"] - asked, relay.REPLY_WAIT + 1.5)
        phase = "listening"
        handoff = None
        audio = False
        for ev in events:
            t = ev.get("type")
            if t == "handoff_wait":
                handoff = gw.handoff_wait_text(ev)
                phase = "thinking"
            elif t == "handoff_done":
                handoff = None
            elif t == "reply" and audio:
                phase = "speaking"
            elif t == "turn_done":
                phase = "listening"
            elif t == "heard":
                phase = "thinking"
        self.assertEqual(phase, "listening")
        self.assertIsNone(handoff)
        held = type("S", (), {"active": True, "handoff_text": " ", "t_first_audio": None})()
        self.assertTrue(gw.handoff_keepalive_due(held))
        held.handoff_text = None
        self.assertFalse(gw.handoff_keepalive_due(held))
        self.assertEqual(gs.state_hold("thinking", False), gs.VOICE_HOLDS["thinking"])
        self.assertLess(gs.VOICE_HOLDS["thinking"], 180)


if __name__ == "__main__":
    unittest.main()
