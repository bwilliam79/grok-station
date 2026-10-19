#!/usr/bin/env python3
"""Relay multi-turn: the real session() handler against a fake xAI realtime server."""
import asyncio, base64, importlib, json, os, sys, tempfile, unittest

from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestServer, TestClient

TMP = tempfile.mkdtemp()
os.environ.update(RELAY_TOKEN="test-token", XAI_API_KEY="test-key", STATE_FILE=os.path.join(TMP, "usage.json"),
                  ALERT_DIR=os.path.join(TMP, "none"), PERSONAS_FILE=os.path.join(TMP, "none.json"), NO_SPEECH="1.0")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay"))
import relay  # noqa: E402

AUDIO = base64.b64encode(b"\0\0" * 240).decode()


def fake_xai(transcripts):
    """Each binary chunk from the Pi that follows a buffer clear (or the first) triggers one scripted turn."""
    log = {"updates": 0, "clears": 0, "turns": 0, "cancels": 0}

    async def ws(req):
        s = web.WebSocketResponse()
        await s.prepare(req)
        armed = True
        async for m in s:
            ev = json.loads(m.data)
            t = ev["type"]
            if t == "session.update":
                log["updates"] += 1
                await s.send_json({"type": "session.updated"})
            elif t == "input_audio_buffer.clear":
                log["clears"] += 1
                armed = True
            elif t == "response.cancel":
                log["cancels"] += 1
            elif t == "input_audio_buffer.append" and armed and log["turns"] < len(transcripts):
                armed = False
                text = transcripts[log["turns"]]
                log["turns"] += 1
                if text is None:
                    continue  # the Pi streams but nobody speaks
                for e in ({"type": "input_audio_buffer.speech_started"}, {"type": "input_audio_buffer.speech_stopped"},
                          {"type": "response.created"},
                          {"type": "conversation.item.input_audio_transcription.completed", "transcript": text}):
                    await s.send_json(e)
                if log["turns"] > 1 and relay.is_end_phrase(text):
                    continue  # the relay ends it; it would cancel this response
                await s.send_json({"type": "response.output_audio.delta", "delta": AUDIO})
                await s.send_json({"type": "response.output_audio_transcript.done", "transcript": "ok"})
                await s.send_json({"type": "response.done"})
        return s
    app = web.Application()
    app.router.add_get("/rt", ws)
    return app, log


class MultiTurn(unittest.IsolatedAsyncioTestCase):
    async def run_session(self, transcripts, listen=True, **cfg):
        old = {k: getattr(relay, k) for k in cfg}
        for k, v in cfg.items():
            setattr(relay, k, v)
        try:
            xapp, xlog = fake_xai(transcripts)
            xs = TestServer(xapp); await xs.start_server()
            relay.URL = str(xs.make_url("/rt")).replace("http", "ws")
            rapp = web.Application(); rapp.router.add_get("/ws", relay.session)
            client = TestClient(TestServer(rapp)); await client.start_server()
            if os.path.exists(relay.STATE):
                os.remove(relay.STATE)
            events = []
            ws = await client.ws_connect("/ws", headers={"Authorization": "Bearer test-token"})
            loop = asyncio.get_running_loop()
            t0 = loop.time()

            async def mic():
                while not ws.closed:
                    await ws.send_bytes(b"\0\0" * 1920)
                    await asyncio.sleep(0.05)
            m = asyncio.create_task(mic())
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                ev = json.loads(msg.data)
                events.append(ev["type"])
                if ev["type"] == "turn_done" and listen:
                    await ws.send_json({"type": "listening"})
            m.cancel()
            dt = loop.time() - t0
            await client.close(); await xs.close()
            return events, xlog, relay.load_usage(), dt
        finally:
            for k, v in old.items():
                setattr(relay, k, v)

    async def test_two_turns_then_silence(self):
        ev, x, u, _ = await self.run_session(["what time is it", "and tomorrow?", None])
        self.assertEqual(ev.count("turn_done"), 2)
        self.assertEqual(ev[-1], "timeout")          # NO_SPEECH close of the unanswered follow-up
        self.assertEqual(x["updates"], 1)            # one xAI session, same persona/voice across turns
        self.assertEqual(x["clears"], 2)             # echo dropped before each follow-up
        self.assertEqual(u["sessions"], 1)           # one wake = one session against the daily cap
        self.assertGreater(u["seconds"], 0.5)        # all turns' wall time counts against daily minutes

    async def test_thanks_ends(self):
        ev, x, _, _ = await self.run_session(["turn the question", "thanks, that's all"])
        self.assertEqual(ev.count("turn_done"), 1)
        self.assertEqual(ev[-1], "done")
        self.assertEqual(x["cancels"], 1)

    async def test_thanks_on_first_turn_is_a_question(self):
        ev, _, _, _ = await self.run_session(["thanks", None])
        self.assertEqual(ev.count("turn_done"), 1)   # only follow-ups can end the session

    async def test_total_cap(self):
        ev, _, _, dt = await self.run_session(["q"] * 50, MAX_TOTAL=3.0, NO_SPEECH=0.5)
        self.assertIn(ev[-1], ("timeout", "done"))   # cap hit, or no room left for another follow-up
        self.assertGreater(ev.count("turn_done"), 1)
        self.assertLess(dt, 4.5)

    async def test_turn_cap_without_listening(self):
        ev, _, _, dt = await self.run_session(["q"], listen=False, MAX_SESSION=2.0)  # old Pi: never says listening
        self.assertEqual(ev[-2:], ["turn_done", "timeout"])
        self.assertLess(dt, 3.5)

    async def test_single_turn_mode(self):
        ev, _, _, _ = await self.run_session(["q", "again"], MULTI_TURN=False)
        self.assertNotIn("turn_done", ev)
        self.assertEqual(ev[-1], "done")

    def test_end_phrases(self):
        for t in ("Thanks", "thank you so much", "That's all", "that’s it.", "no thanks", "bye", "okay thanks Grok"):
            self.assertTrue(relay.is_end_phrase(t), t)
        for t in ("thanks, and what about tomorrow's weather in the morning", "what's the time", "", "nothing works here why"):
            self.assertFalse(relay.is_end_phrase(t), t)


if __name__ == "__main__":
    unittest.main()
