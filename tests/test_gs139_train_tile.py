#!/usr/bin/env python3
"""gs-voice-train SPEC rev1.5 (Noor): the station training tile draws no buttons on any stage.

Prompt shows the muted status line 'Starting in a moment…'; failed keeps its title and reason, adds the voice
retry hint, and closes on its own after 10 s like the error stage.
"""
import os
import sys
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import grok_station as gs  # noqa: E402
import pygame  # noqa: E402


def setUpModule():
    pygame.display.init()
    pygame.font.init()


def _msg(stage, i=1, n=5, extra="", who="Juniper", phrase="Hey Juniper"):
    return {"from": who, "voice": True, "state": "enroll", "text": f"“{phrase}”", "heard": f"{i}/{n}|{stage}|{extra}"}


def _lum(c):
    out = []
    for v in c[:3]:
        v = v / 255
        out.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def _contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class _Spy:
    """Records (text, color) for every render while drawing."""

    def __init__(self):
        self.seen = []

    def __enter__(self):
        real = self.real = gs.font
        seen = self.seen

        class F:
            def __init__(self, inner):
                self.inner = inner

            def render(self, text, aa, color, *a, **k):
                seen.append((text, tuple(color)[:3]))
                return self.inner.render(text, aa, color, *a, **k)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        gs.font = lambda kind, px: F(real(kind, px))
        return self

    def __exit__(self, *a):
        gs.font = self.real


def _draw(stage, **kw):
    surf = pygame.Surface((2560, 1706))
    with _Spy() as spy:
        gs.draw_enroll(surf, gs.Layout(2560, 1706), time.localtime(), _msg(stage, **kw), 1.0)
    return spy.seen


class Rev15NoButtons(unittest.TestCase):
    def test_no_stage_draws_a_button(self):
        for stage in gs.ENROLL_STAGES:
            _draw(stage, extra="3/8" if stage == "failed" else "")
            self.assertEqual(gs.LAST_ENROLL_BUTTONS, [], stage)
            self.assertEqual(gs._enroll_actions(stage)[0], [], stage)
        labels = {t for t, _ in _draw("prompt")} | {t for t, _ in _draw("failed", extra="5")}
        for gone in ("Start", "Not now", "Record 5 more", "Cancel", "Done", "Close"):
            self.assertNotIn(gone, labels)


class Rev15PromptStatus(unittest.TestCase):
    def test_starting_line_is_muted_and_readable(self):
        seen = dict(_draw("prompt"))
        self.assertIn("Starting in a moment…", seen)
        color = seen["Starting in a moment…"]
        self.assertEqual(color, tuple(gs.MUTED)[:3])
        self.assertGreaterEqual(_contrast(color, tuple(gs.SURFACE)), 4.5)

    def test_prompt_still_auto_advances_and_keeps_its_hold(self):
        self.assertEqual(gs.state_hold("enroll", False, "1/5|prompt|"), gs.VOICE_HOLDS["enroll"])


class Rev15FailedStage(unittest.TestCase):
    HINT = "Say 'Hey Grok, train my wake word for Juniper' to try again."

    def test_title_reason_and_exact_hint(self):
        seen = _draw("failed", extra="5")
        texts = [t for t, _ in seen]
        self.assertIn("Needs 5 more clips", texts)
        self.assertTrue(any(t.startswith("A short top-up") for t in texts), texts)
        self.assertIn(self.HINT, texts)
        color = dict(seen)[self.HINT]
        self.assertGreaterEqual(_contrast(color, tuple(gs.SURFACE)), 6.8)

    def test_hint_uses_display_name(self):
        texts = [t for t, _ in _draw("failed", extra="5", who="Maple Leaf", phrase="Hey Maple")]
        self.assertIn("Say 'Hey Grok, train my wake word for Maple Leaf' to try again.", texts)

    def test_closes_after_ten_seconds_like_error(self):
        self.assertEqual(gs.state_hold("enroll", False, "0/5|failed|5"), 10)
        self.assertEqual(gs.state_hold("enroll", False, "0/5|error|"), 10)


if __name__ == "__main__":
    unittest.main()
