#!/usr/bin/env python3
"""0.13.7: "Hey Grok, train my wake word for <bot>" keeps the training tile up.

The relay opens the prompt tile (POST /enroll/open-tile) and then ends the voice session with
{"type": "done", "local": true}. Before 0.13.7 the wake client answered every local done with
post_state("idle") and the end tone, so the display dropped the tile it had just shown and the station
played the hang-up tone. A local done that hands off to the training tile now leaves the tile alone and
plays no end tone, the same as opening it from the Bots page button.
"""
import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wake"))
import grok_wake as gw  # noqa: E402


class _WS:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for ev in self.events:
            if callable(ev):
                ev()
                continue
            yield json.dumps(ev)

    def send(self, b):
        pass


class VoiceStartedTraining(unittest.TestCase):
    def setUp(self):
        self.saved = (gw.post_state, gw.RELAY_URL, gw.play_file, gw.configured_wav, dict(gw.ENROLL_STATE))
        self.posted, self.played = [], []
        gw.post_state = lambda state=None, **kw: self.posted.append(state)
        gw.play_file = lambda dev, path, **kw: self.played.append(path)
        gw.configured_wav = lambda key: "/tones/%s.wav" % key
        gw.RELAY_URL = "ws://relay.test/ws"
        gw.ENROLL_STATE.update(busy=False, pause=False, discard=False, prompt=None)

    def tearDown(self):
        gw.post_state, gw.RELAY_URL, gw.play_file, gw.configured_wav, state = self.saved
        gw.ENROLL_STATE.clear()
        gw.ENROLL_STATE.update(state)

    def _session(self, events):
        mod = types.ModuleType("websockets.sync.client")
        mod.connect = lambda url, **kw: _WS(events)
        sys.modules.update({"websockets": types.ModuleType("websockets"),
                            "websockets.sync": types.ModuleType("websockets.sync"),
                            "websockets.sync.client": mod})
        s = gw.Session("default", persona="grok")
        while s.active:
            pass

    def _open_tile(self):
        code, _ = gw.open_enroll_tile({"persona": "grok", "name": "Grok"})
        self.assertEqual(code, 200)

    def test_tile_opened_by_voice_survives_the_local_done(self):
        self._session([{"type": "ready"}, self._open_tile, {"type": "done", "local": True}])
        self.assertIn("enroll", self.posted)
        after = self.posted[self.posted.index("enroll") + 1:]
        self.assertNotIn("idle", after)  # the display keeps the training tile
        self.assertIsNotNone(gw.ENROLL_STATE.get("prompt"))
        self.assertNotIn("/tones/END_TONE.wav", self.played)  # no hang-up tone

    def test_plain_local_done_still_goes_idle_with_end_tone(self):
        self._session([{"type": "ready"}, {"type": "done", "local": True}])
        self.assertIn("idle", self.posted)
        self.assertIn("/tones/END_TONE.wav", self.played)


if __name__ == "__main__":
    unittest.main()
