#!/usr/bin/env python3
"""Enroll tile header uses the persona display name, not the bot id.

A persona whose id is "juniper-2" and whose name is "Juniper" reads
"Wake word setup · Juniper". The slug is not drawn.
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


class _Font:
    def __init__(self, inner, seen):
        self.inner = inner
        self.seen = seen

    def render(self, text, antialias, color, *args, **kwargs):
        self.seen.append(text)
        return self.inner.render(text, antialias, color, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class PersonaHeader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_file = gs.PERSONAS_FILE
        self.old_personas = dict(gs._personas)
        path = os.path.join(self.tmp.name, "personas.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"personas": [{"slug": "juniper-2", "name": "Juniper"}]}, fh)
        gs.PERSONAS_FILE = path
        gs._personas["mtime"] = None
        self._timers = list(gs.TIMER_CHIPS)
        gs.TIMER_CHIPS[:] = []

    def tearDown(self):
        gs.PERSONAS_FILE = self.old_file
        gs._personas.clear()
        gs._personas.update(self.old_personas)
        gs.TIMER_CHIPS[:] = self._timers
        self.tmp.cleanup()

    def _drawn(self, msg):
        seen = []
        real = gs.font
        gs.font = lambda kind, px: _Font(real(kind, px), seen)
        try:
            surf = pygame.Surface((1920, 1080))
            gs.draw_enroll(surf, gs.Layout(1920, 1080), time.localtime(), msg, 1.0)
        finally:
            gs.font = real
        return seen

    def test_header_uses_display_name_not_bot_id(self):
        names = gs.persona_names()
        self.assertEqual(names.get(gs.name_key("juniper-2")), ("Juniper", "juniper-2"))
        seen = self._drawn({"from": "juniper-2", "voice": True, "state": "enroll",
                            "text": "“Hey Juniper”", "heard": "1/5|ready|"})
        self.assertIn("Wake word setup · Juniper", seen)
        self.assertFalse(any("juniper-2" in text for text in seen))
        # No persona still names the phrase, not the built-in assistant.
        seen = self._drawn({"from": gs.VOICE_NAME, "voice": True, "state": "enroll",
                            "text": "“Hey Nova”", "heard": "1/5|prompt|"})
        self.assertIn("Wake word setup · Hey Nova", seen)


if __name__ == "__main__":
    unittest.main()
