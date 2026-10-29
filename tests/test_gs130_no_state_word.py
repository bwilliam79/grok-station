"""0.13.0: the active bot's zone card draws no state word ("Listening", "Thinking", "Replying") in its top-right
corner; the ring around the avatar is the only state. The name and reply start where they did."""
import os, sys, unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import grok_station as gs  # noqa: E402
import pygame  # noqa: E402


class NoStateWord(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.display.init()
        pygame.font.init()

    def test_zone_card_has_no_state_word(self):
        calls, real = [], gs.draw_state_label
        gs.draw_state_label = lambda *a, **k: calls.append(a[2]) or real(*a, **k)
        try:
            L = gs.Layout(1920, 1080) if hasattr(gs, "Layout") else None
            if L is None:
                self.skipTest("no Layout helper")
            surf = pygame.Surface((1920, 1080))
            rect = pygame.Rect(L.m, 500, 1920 - 2 * L.m, 460)
            for state in ("listening", "thinking", "speaking"):
                gs.draw_zone_voice(surf, L, rect, {"from": "Max", "voice": True, "text": "Hello."}, state)
        finally:
            gs.draw_state_label = real
        self.assertEqual(calls, [])

    def test_source_has_no_corner_label(self):
        src = open(os.path.join(ROOT, "app", "grok_station.py")).read()
        body = src[src.index("def draw_zone_voice("):]
        body = body[:body.index("\ndef ")]
        self.assertNotIn("draw_state_label(", body)
        self.assertIn("ring", body)


if __name__ == "__main__":
    unittest.main()
