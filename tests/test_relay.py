#!/usr/bin/env python3
"""Relay multi-turn: the real session() handler against a fake xAI realtime server."""
import asyncio, base64, importlib, json, os, sys, tempfile, unittest

from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestServer, TestClient

TMP = tempfile.mkdtemp()
os.environ.update(RELAY_TOKEN="test-token", XAI_API_KEY="test-key", ALERT_DIR=os.path.join(TMP, "none"), PERSONAS_FILE=os.path.join(TMP, "none.json"), NO_SPEECH="1.0")
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
    async def run_session(self, transcripts, listen=True, expect_reject=False, **cfg):
        old = {k: getattr(relay, k) for k in cfg}
        for k, v in cfg.items():
            setattr(relay, k, v)
        try:
            xapp, xlog = fake_xai(transcripts)
            xs = TestServer(xapp); await xs.start_server()
            relay.URL = str(xs.make_url("/rt")).replace("http", "ws")
            rapp = web.Application(); rapp.router.add_get("/ws", relay.session)
            client = TestClient(TestServer(rapp)); await client.start_server()
            events = []
            if expect_reject:
                r = await client.get("/ws", headers={"Authorization": "Bearer test-token"})
                await client.close(); await xs.close()
                return [r.status], xlog, dict(relay.stats), 0
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
            return events, xlog, dict(relay.stats), dt
        finally:
            for k, v in old.items():
                setattr(relay, k, v)

    async def test_two_turns_then_silence(self):
        ev, x, u, _ = await self.run_session(["what time is it", "and tomorrow?", None])
        self.assertEqual(ev.count("turn_done"), 2)
        self.assertEqual(ev[-1], "timeout")          # NO_SPEECH close of the unanswered follow-up
        self.assertEqual(x["updates"], 1)            # one xAI session, same persona/voice across turns
        self.assertEqual(x["clears"], 2)             # echo dropped before each follow-up
        self.assertGreater(u["seconds"], 0.5)        # in-memory stats only

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

    async def test_no_daily_cap(self):
        self.assertFalse(hasattr(relay, "MAX_SESSIONS_DAY"))
        self.assertFalse(hasattr(relay, "MAX_MINUTES_DAY"))
        self.assertFalse(hasattr(relay, "load_usage"))
        relay.stats.update(sessions=10_000, seconds=1e7)   # far past any old daily limit
        ev, _, u, _ = await self.run_session(["q"], MULTI_TURN=False)
        self.assertEqual(ev[-1], "done")                    # no 429
        self.assertEqual(u["sessions"], 10_001)

    def test_end_phrases(self):
        for t in ("Thanks", "thank you so much", "That's all", "that’s it.", "no thanks", "bye", "okay thanks Grok",
                  "goodbye Hank", "bye Hank", "ok thanks Hank", "thanks, that's it", "Thanks Hank, bye.",
                  "alright, that's all for now", "see you later"):
            self.assertTrue(relay.is_end_phrase(t), t)
        for t in ("thanks, and what about tomorrow's weather in the morning", "what's the time", "", "nothing works here why",
                  "goodbye what is the weather tomorrow", "thanks for checking the garage door"):
            self.assertFalse(relay.is_end_phrase(t), t)

    def test_utterance_ending_in_goodbye_ends(self):
        for t in ("Alright, thanks, goodbye.", "ok bye", "goodbye Rhea", "That was really helpful, thanks so much, bye!",
                  "Great, I'll check it later. Goodbye Hank.", "okay see you later", "thanks bye bye"):
            self.assertTrue(relay.is_end_phrase(t), t)
            self.assertTrue(relay.ends_with_goodbye(t), t)
        for t in ("goodbye what is the weather tomorrow", "what does goodbye mean in french please",
                  "is the bypass road open", "thanks for checking the garage door"):
            self.assertFalse(relay.is_end_phrase(t), t)

    def test_local_exit_phrases(self):
        ringing = {"a": {"ringing": True}}
        for phrase, kind in (
            ("goodbye", "goodbye"),
            ("stop", "stop"),
            ("never mind", "never mind"),
            ("nevermind", "never mind"),
            ("cancel", "cancel"),
            ("that's all", "that's all"),
            ("Hey Rhea, goodbye", "goodbye"),
            ("hey kai stop", "stop"),
        ):
            self.assertEqual(relay.local_exit_phrase(phrase), kind, phrase)
            if kind != "stop":
                self.assertTrue(relay.session_local_exit(phrase, ringing), phrase)
            else:
                self.assertTrue(relay.session_local_exit(phrase, {}), phrase)
        self.assertFalse(relay.session_local_exit("stop", ringing))
        self.assertTrue(relay.session_local_exit("goodbye", ringing))
        for phrase in ("stop the timer", "goodbye what is the weather", "thanks", "cancel the pasta"):
            self.assertEqual(relay.local_exit_phrase(phrase), "", phrase)
            self.assertFalse(relay.session_local_exit(phrase, {}), phrase)
            self.assertFalse(relay.session_local_exit(phrase, ringing), phrase)

    def test_latest_utterance_strips_earlier_turns(self):
        q1 = "What is on my calendar today?"
        self.assertEqual(relay.latest_utterance([q1], q1 + " Goodbye Hank."), "Goodbye Hank.")
        self.assertTrue(relay.is_end_phrase(relay.latest_utterance([q1], q1 + " Goodbye Hank.")))
        self.assertEqual(relay.latest_utterance([q1], "Goodbye Hank."), "Goodbye Hank.")
        self.assertEqual(relay.latest_utterance([], q1), q1)
        self.assertEqual(relay.latest_utterance([q1], q1), q1)  # a true repeat is kept
        q2 = "And tomorrow?"
        self.assertEqual(relay.latest_utterance([q1, q1 + " " + q2], q1 + " " + q2 + " thanks"), "thanks")


class PortraitDelete(unittest.TestCase):
    def test_symlink_is_unlinked_and_outside_files_stay(self):
        portraits = os.path.join(tempfile.mkdtemp(), "portraits")
        os.makedirs(portraits)
        outside = os.path.join(os.path.dirname(portraits), "outside.png")
        with open(outside, "wb") as fh:
            fh.write(b"keep")
        link = os.path.join(portraits, "kai.png")
        os.symlink(outside, link)
        real = os.path.join(portraits, "mira.png")
        with open(real, "wb") as fh:
            fh.write(b"png")
        old = relay.PORTRAIT_WRITE
        relay.PORTRAIT_WRITE = portraits
        try:
            relay._delete_written_portrait("kai")
            relay._delete_written_portrait("mira")
            relay._delete_written_portrait("../outside")
            self.assertFalse(os.path.lexists(link))
            self.assertFalse(os.path.exists(real))
            self.assertTrue(os.path.isfile(outside))
            self.assertEqual(open(outside, "rb").read(), b"keep")
        finally:
            relay.PORTRAIT_WRITE = old


class PortraitDeleteNix(unittest.TestCase):
    def test_a_directory_named_like_the_png_is_left_and_does_not_raise(self):
        portraits = os.path.join(tempfile.mkdtemp(), "portraits")
        os.makedirs(os.path.join(portraits, "kai.png"))
        old = relay.PORTRAIT_WRITE
        relay.PORTRAIT_WRITE = portraits
        try:
            relay._delete_written_portrait("kai")  # must not raise
            self.assertTrue(os.path.isdir(os.path.join(portraits, "kai.png")))
        finally:
            relay.PORTRAIT_WRITE = old

    def test_a_symlinked_portraits_folder_cannot_redirect_the_delete(self):
        base = tempfile.mkdtemp()
        elsewhere = os.path.join(base, "elsewhere")
        os.makedirs(elsewhere)
        victim = os.path.join(elsewhere, "kai.png")
        open(victim, "wb").write(b"keep")
        link = os.path.join(base, "portraits")
        os.symlink(elsewhere, link)
        old = relay.PORTRAIT_WRITE
        relay.PORTRAIT_WRITE = link
        try:
            relay._delete_written_portrait("kai")
            self.assertEqual(open(victim, "rb").read(), b"keep")
        finally:
            relay.PORTRAIT_WRITE = old


class AckClip(unittest.IsolatedAsyncioTestCase):
    """Asking-line cache: hit, miss, TTS failure, and invalidation when the name or voice changes."""

    async def asyncSetUp(self):
        import handoff
        self.handoff = handoff
        self.tmp = tempfile.mkdtemp()
        self.old = (relay.ACK_DIR, relay.tts_pcm, relay.WEBHOOKS_FILE, relay._limits, relay.HANDOFF)
        self.hooks = {"mtime": relay._hooks.get("mtime"), "map": dict(relay._hooks.get("map") or {})}
        relay.ACK_DIR = os.path.join(self.tmp, "ack")
        relay.WEBHOOKS_FILE = os.path.join(self.tmp, "webhooks.json")
        relay._hooks.update(mtime=None, map={})
        relay.HANDOFF = True  # webhooks() is empty while handoff is off; another test leaves it off
        relay._limits = handoff.Limits(os.path.join(self.tmp, "limits.json"), relay.today)
        self.calls = []
        self.fail = False

        async def fake(http, text, voice, max_s=relay.TTS_SHORT_S):
            self.calls.append((text, voice, max_s))
            if self.fail:
                return None
            return b"PCM:" + str(text).encode()

        relay.tts_pcm = fake

    async def asyncTearDown(self):
        relay.ACK_DIR, relay.tts_pcm, relay.WEBHOOKS_FILE, relay._limits, relay.HANDOFF = self.old
        relay._hooks.clear()
        relay._hooks.update(self.hooks)
        relay._ACK_INFLIGHT.clear()

    def _webhook(self, slug="jules"):
        with open(relay.WEBHOOKS_FILE, "w") as f:
            json.dump({slug: {"url": "https://api2.cursor.sh/hook", "token": "t"}}, f)
        relay._hooks["mtime"] = None

    async def test_miss_then_hit_and_file_mode(self):
        line = relay.asking_line("Jules")
        pcm = await relay.ack_clip(None, "jules", "Jules", "ara")
        self.assertEqual(pcm, b"PCM:" + line.encode())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2], relay.TTS_SHORT_S)
        again = await relay.ack_clip(None, "jules", "Jules", "ara")
        self.assertEqual(again, pcm)
        self.assertEqual(len(self.calls), 1)
        names = os.listdir(relay.ACK_DIR)
        self.assertEqual(names, [f"jules-{relay.ack_digest('Jules', 'ara')}.pcm"])
        self.assertEqual(os.stat(os.path.join(relay.ACK_DIR, names[0])).st_mode & 0o777, 0o640)

    async def test_rename_and_voice_change_the_key(self):
        await relay.ack_clip(None, "jules", "Jules", "ara")
        self.assertNotEqual(relay.ack_digest("Jules", "ara"), relay.ack_digest("Julie", "ara"))
        self.assertNotEqual(relay.ack_digest("Jules", "ara"), relay.ack_digest("Jules", "rex"))
        relay.prune_ack_clips("jules", "Julie", "ara")  # rename: old digest goes
        self.assertEqual(os.listdir(relay.ACK_DIR), [])
        await relay.ack_clip(None, "jules", "Julie", "ara")
        await relay.ack_clip(None, "jules", "Jules", "rex")  # a second voice, not pruned yet
        self.assertEqual(len(os.listdir(relay.ACK_DIR)), 2)
        relay.prune_ack_clips("jules", "Jules", "rex")
        self.assertEqual(os.listdir(relay.ACK_DIR), [f"jules-{relay.ack_digest('Jules', 'rex')}.pcm"])
        relay.forget_ack_clips("jules")
        self.assertEqual(os.listdir(relay.ACK_DIR), [])
        n = len(self.calls)
        await relay.ack_clip(None, "jules", "Jules", "rex")
        self.assertEqual(len(self.calls), n + 1)  # forgotten clip is rendered again

    async def test_tts_failure_is_not_cached(self):
        self.fail = True
        self.assertIsNone(await relay.ack_clip(None, "jules", "Jules", "ara"))
        self.assertFalse(os.path.isdir(relay.ACK_DIR) and os.listdir(relay.ACK_DIR))
        self.fail = False
        pcm = await relay.ack_clip(None, "jules", "Jules", "ara")
        self.assertTrue(pcm)

    def test_bad_slug_does_not_escape_the_cache_dir(self):
        self.assertFalse(relay.write_ack_cache("../x", "Jules", "ara", b"nope"))
        self.assertFalse(relay.write_ack_cache("Jules", "Jules", "ara", b"nope"))  # slug must be lowercase
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x-anything.pcm")))
        self.assertEqual(relay._ack_names("../jules"), [])

    def test_withheld_without_a_webhook_or_over_the_cap(self):
        self.assertTrue(relay.ack_withheld("jules", "what time is it"))  # no webhook: will not wait
        self.assertTrue(relay.ack_withheld("jules", "   "))  # empty question
        self._webhook()
        relay._hooks["mtime"] = None
        self.assertFalse(relay.ack_withheld("jules", "what time is it"))
        self.assertFalse(relay.ack_withheld("jules"))  # words not known yet, route exists
        relay._limits.take("jules", "out")
        self.assertTrue(relay.ack_withheld("jules", "what time is it"))
        self.assertTrue(relay.ack_withheld("jules"))  # cap, even before the transcript
        self.assertEqual(relay._limits.block_reason("jules"), "outstanding")


if __name__ == "__main__":
    unittest.main()


class HoldCap(unittest.TestCase):
    """Nix (d): held model output is dropped after about 3 s with no transcript."""
    def test_hold_expires_after_the_cap(self):
        self.assertEqual(relay.HOLD_CAP_S, 3.0)
        st = {"buf": [{"type": "response.created"}], "buf_t0": 100.0, "turn_heard": False}
        self.assertFalse(relay.hold_expired(st, 102.9))
        self.assertTrue(relay.hold_expired(st, 103.0))
        self.assertFalse(relay.hold_expired(dict(st, turn_heard=True), 110))   # transcript came: normal path
        self.assertFalse(relay.hold_expired(dict(st, drop_model=True), 110))   # routed or dropped turn
        self.assertFalse(relay.hold_expired(dict(st, buf=[]), 110))            # nothing held

    def test_cap_drops_and_never_emits(self):
        src = open(relay.__file__, encoding="utf-8").read()
        drop = src.split("async def drop_held_output():", 1)[1].split("async def caps():", 1)[0]
        self.assertIn('st["drop_model"] = True', drop)
        self.assertIn("await cancel_response()", drop)
        self.assertNotIn("turn_heard", drop.split('"""', 2)[2])  # code, not the docstring
        self.assertNotIn("emit", drop.split('"""', 2)[2])
        self.assertNotIn("_emit", src)
        self.assertIn('await pi.send_json({"type": "timeout"', drop)
        caps = src.split("async def caps():", 1)[1].split("tasks = [", 1)[0]
        self.assertIn('await drop_held_output()\n                        return "no transcript"', caps)


class UpdateScriptNoFollow(unittest.TestCase):
    """Nix (c): the computer-id read opens <persona>.json with O_NOFOLLOW."""
    def test_o_nofollow(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sh = open(os.path.join(root, "scripts", "gs-update.sh"), encoding="utf-8").read()
        block = sh.split('pj="$HOME/.config/grok-station/$persona.json"', 1)[1].split("crc=", 1)[0]
        self.assertIn("os.O_RDONLY | os.O_NOFOLLOW", block)
        self.assertIn("errno.ELOOP", block)
        self.assertNotRegex(block, r"(?<!os\.)open\(sys\.argv\[2\]")  # no plain open() of the persona file
