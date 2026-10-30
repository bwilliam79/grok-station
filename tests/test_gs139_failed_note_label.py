#!/usr/bin/env python3
"""0.13.9 (Noor rev3.7 item 3): the failed stage's restart line names the bot's display name.

draw_enroll passed `who` (the bot id, e.g. "juniper-2") to _draw_actions, so the failed tile said
"train my wake word for juniper-2". It now passes `label`. Fails on 77ba820.
"""
import json
import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import grok_station as gs
import pygame


def setUpModule():
    pygame.display.init()
    pygame.font.init()


class FailedNote(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (gs.PERSONAS_FILE, dict(gs._personas), list(gs.TIMER_CHIPS), gs._draw_actions)
        path = os.path.join(self.tmp.name, "personas.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"personas": [{"slug": "juniper-2", "name": "Juniper"}]}, fh)
        gs.PERSONAS_FILE = path
        gs._personas["mtime"] = None
        gs.TIMER_CHIPS[:] = []
        self.seen = []
        real = gs._draw_actions

        def spy(surf, L, tile, slot, col_w, stage, who=""):
            self.seen.append((stage, who, gs._enroll_note(stage, who)))
            return real(surf, L, tile, slot, col_w, stage, who)
        gs._draw_actions = spy

    def tearDown(self):
        gs.PERSONAS_FILE, personas, chips, gs._draw_actions = self.old
        gs._personas.clear()
        gs._personas.update(personas)
        gs.TIMER_CHIPS[:] = chips
        self.tmp.cleanup()

    def _draw(self, msg):
        gs.draw_enroll(pygame.Surface((1920, 1080)), gs.Layout(1920, 1080), time.localtime(), msg, 1.0)

    def test_failed_line_uses_display_name(self):
        self._draw({"from": "juniper-2", "voice": True, "state": "enroll", "text": "“Hey Juniper”", "heard": "2/5|failed|"})
        stage, who, note = self.seen[-1]
        self.assertEqual(stage, "failed")
        self.assertEqual(who, "Juniper")
        self.assertEqual(note, "Say 'Hey Grok, train my wake word for Juniper' to try again.")

    def test_no_persona_still_names_the_phrase(self):
        self._draw({"from": gs.VOICE_NAME, "voice": True, "state": "enroll", "text": "“Hey Nova”", "heard": "2/5|failed|"})
        self.assertEqual(self.seen[-1][1], "Hey Nova")


if __name__ == "__main__":
    unittest.main()
