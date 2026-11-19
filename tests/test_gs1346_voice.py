"""0.13.46: a reply's voiceId is the voice that bot shows and speaks.

A reply that leaves voiceId out keeps the voice already synced. It does not
fall back to the station default.
"""
import asyncio
import base64
import json
import os
import shutil
import sys
import unittest

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from nacl.public import SealedBox
from nacl.signing import SigningKey

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_avatar  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_seal():
    import importlib.machinery
    import importlib.util
    path = os.path.join(ROOT, "scripts", "gs-seal-reply")
    loader = importlib.machinery.SourceFileLoader("gs1346_seal", path)
    spec = importlib.util.spec_from_loader("gs1346_seal", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class ReplyVoice(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_avatar.Avatar.asyncSetUp(self)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    asyncTearDown = test_avatar.Avatar.asyncTearDown

    def _pin_max(self):
        r = self.relay
        self.max_key = SigningKey.generate()
        self.assertTrue(self.botreply.pin_key(
            r.bot_keys_path(), "max", base64.b64encode(bytes(self.max_key.verify_key)).decode()))
        r.save_overlay({"added": {"max": {"name": "Max"}}, "voices": {}, "renamed": {},
                        "hidden": [], "list_source": ""})
        r.VOICES.update({"ara", "eve", "luna"})
        self.assertEqual(r.persona_list()["max"]["voice"], r.VOICE.lower())

    def _reply(self, rid, text, voice=None):
        doc = {"action_class": "answer", "kind": "reply", "request_id": rid, "text": text, "tier": 0}
        if voice is not None:
            doc["voiceId"] = voice
        signed = self.max_key.sign(json.dumps(doc, separators=(",", ":"), sort_keys=True).encode())
        sealed = SealedBox(self.station.public_key).encrypt(signed)
        return json.dumps({"v": 1, "bot": "max", "sealed": base64.b64encode(sealed).decode()}).encode()

    async def _ask(self, script):
        """script items are (delay, text, voice or None). voice None omits voiceId."""
        statuses = []

        async def hook(req):
            rid = (await req.json())["request_id"]

            async def send():
                for delay, text, voice in script:
                    await asyncio.sleep(delay)
                    extra = {} if voice is None else {"voice": voice}
                    body = self._reply(rid, text, extra.get("voice"))
                    resp = await self.client.post("/voice/reply", data=body,
                                                  headers={"Content-Type": "application/json"})
                    statuses.append(resp.status)
            asyncio.get_running_loop().create_task(send())
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/hook", hook)
        srv = TestServer(app)
        await srv.start_server()
        r = self.relay
        old_rate = r._reply_rate
        r._reply_rate = None
        r.write_webhooks({"max": {"url": str(srv.make_url("/hook")), "token": "hook-token-aaaa", "saved_at": 1}})
        spoken = []

        async def fake_tts(http, text, voice, max_s=r.TTS_SHORT_S):
            spoken.append(voice)
            return b"pcm"

        old_tts = r.tts_pcm
        r.tts_pcm = fake_tts
        try:
            async with ClientSession() as http:
                got = await r.ask_teammate(http, "max", "what time is dinner")
                voice = r.answer_voice(r.persona_list(), "max")
                await r.tts_pcm(http, got[0], voice, r.TTS_REPLY_S)
        finally:
            r.tts_pcm = old_tts
            r._reply_rate = old_rate
            await srv.close()
        return got, statuses, spoken, voice

    async def test_a_reply_voice_is_shown_and_spoken_and_a_later_reply_keeps_it(self):
        self._pin_max()
        got, statuses, spoken, voice = await self._ask([(0.05, "Dinner is at 6.", "eve")])
        self.assertEqual(statuses, [200])
        self.assertEqual(got, ("Dinner is at 6.", "ok"))
        shown = self.relay.persona_list()["max"]["voice"]
        self.assertEqual(shown, "eve")
        self.assertEqual(voice, "eve")
        self.assertEqual(spoken, ["eve"])
        self.assertEqual(self.relay._stored_voice("max"), "eve")
        # The Bots page copies this field. It is not the station default.
        self.assertNotEqual(shown, self.relay.VOICE.lower())
        self.relay._limits = self.handoff.Limits(
            os.path.join(self.relay.PENDING_DIR, "limits-2.json"), self.relay.today)
        self.relay._LATE.clear()
        got, statuses, spoken, voice = await self._ask([(0.05, "Practice is at 7.", None)])
        self.assertEqual(statuses, [200])
        self.assertEqual(self.relay.persona_list()["max"]["voice"], "eve")
        self.assertEqual(voice, "eve")
        self.assertEqual(spoken, ["eve"])
        self.assertNotEqual(voice, "luna")
        self.relay._limits = self.handoff.Limits(
            os.path.join(self.relay.PENDING_DIR, "limits-3.json"), self.relay.today)
        self.relay._LATE.clear()
        got, statuses, spoken, voice = await self._ask([(0.05, "Still eve.", "NOT A VOICE!!!")])
        self.assertEqual(statuses, [200])
        self.assertEqual(self.relay.persona_list()["max"]["voice"], "eve")
        self.assertEqual(voice, "eve")


class SealVoice(unittest.TestCase):
    def test_the_reply_seals_voice_id_and_omits_it_when_missing(self):
        mod = _load_seal()
        with_voice = json.loads(mod.canonical_answer("vs_" + "a" * 22, "Hello.", voice="Eve"))
        self.assertEqual(with_voice["voiceId"], "eve")
        without = json.loads(mod.canonical_answer("vs_" + "a" * 22, "Hello."))
        self.assertNotIn("voiceId", without)
        args = mod._parser().parse_args(
            ["reply", "--persona", "max", "--request-id", "vs_" + "b" * 22, "--voice", "eve"])
        self.assertEqual(args.voice, "eve")

    def test_voice_id_uses_the_helper_shape_and_length_cap(self):
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        import botreply
        self.assertEqual(botreply._voice_id("Eve"), "eve")
        self.assertEqual(botreply._voice_id("a" * 32), "a" * 32)
        self.assertEqual(botreply._voice_id("a" * 33), "")
        self.assertEqual(botreply._voice_id("has_under"), "")
        self.assertEqual(botreply._voice_id("9start"), "")
        self.assertEqual(botreply._voice_id(""), "")


if __name__ == "__main__":
    unittest.main()
