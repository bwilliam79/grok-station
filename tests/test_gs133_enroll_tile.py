#!/usr/bin/env python3
"""Wake-word training tile: fixed avatar, ring-only state, five clips, chips, 10 s hold."""
import inspect
import math
import os
import sys
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


def _msg(stage, i=1, n=5, extra="", who="Juniper", phrase="Hey Juniper"):
    return {"from": who, "voice": True, "state": "enroll", "text": f"“{phrase}”",
            "heard": f"{i}/{n}|{stage}|{extra}"}


def _near(color, rgb, tol=48):
    return all(abs(int(color[i]) - rgb[i]) <= tol for i in range(3))


def _ring_samples(surf, box, ring_w=14, n=48):
    radius = box.w / 2.0 - ring_w / 2.0
    cx, cy = box.center
    out = []
    for k in range(n):
        ang = -math.pi / 2 + 2 * math.pi * k / n
        x = int(round(cx + radius * math.cos(ang)))
        y = int(round(cy + radius * math.sin(ang)))
        if 0 <= x < surf.get_width() and 0 <= y < surf.get_height():
            out.append(tuple(surf.get_at((x, y))[:3]))
    return out


class TrainingTile(unittest.TestCase):
    def setUp(self):
        self._chips = gs.CHIPS
        self._timers = list(gs.TIMER_CHIPS)
        gs.CHIPS = gs.ChipTray()
        gs.TIMER_CHIPS[:] = []

    def tearDown(self):
        gs.CHIPS = self._chips
        gs.TIMER_CHIPS[:] = self._timers

    def _draw(self, stage, size=(2560, 1706), **kw):
        surf = pygame.Surface(size)
        L = gs.Layout(*size)
        gs.draw_enroll(surf, L, time.localtime(), _msg(stage, **kw), 1.0)
        return surf, L

    def test_avatar_box_is_fixed_across_stages(self):
        boxes = []
        for stage in ("prompt", "ready", "recording", "quiet", "loud", "saved", "queued", "passed", "failed", "error"):
            self._draw(stage)
            boxes.append(pygame.Rect(gs.LAST_ENROLL_AVATAR))
        self.assertEqual(boxes[0], pygame.Rect(203, 926, 548, 548))
        self.assertTrue(all(b == boxes[0] for b in boxes))

    def test_retry_and_failed_rings_are_dashed(self):
        surf, _L = self._draw("prompt")
        solid = _ring_samples(surf, gs.LAST_ENROLL_AVATAR)
        self.assertGreater(sum(_near(c, gs.MUTED) for c in solid), 40)
        surf, _L = self._draw("quiet")
        dashed = _ring_samples(surf, gs.LAST_ENROLL_AVATAR)
        self.assertGreater(sum(_near(c, gs.MUTED) for c in dashed), 4)
        self.assertGreater(sum(_near(c, gs.SURFACE) for c in dashed), 4)
        self.assertLess(sum(_near(c, gs.MUTED) for c in dashed), sum(_near(c, gs.MUTED) for c in solid))
        surf, _L = self._draw("loud")
        loud = _ring_samples(surf, gs.LAST_ENROLL_AVATAR)
        self.assertGreater(sum(_near(c, gs.SURFACE) for c in loud), 4)
        surf, _L = self._draw("failed")
        failed = _ring_samples(surf, gs.LAST_ENROLL_AVATAR)
        self.assertGreater(sum(_near(c, gs.AMBER) for c in failed), 4)
        self.assertGreater(sum(_near(c, gs.SURFACE) for c in failed), 4)

    def test_top_right_of_the_tile_has_no_state_label(self):
        for stage in ("prompt", "recording", "quiet", "passed", "failed"):
            surf, _L = self._draw(stage)
            tile = gs.LAST_ENROLL_RECT
            corner = pygame.Rect(tile.right - 200, tile.top + 16, 160, 70)
            for y in range(corner.top, corner.bottom, 3):
                for x in range(corner.left, corner.right, 3):
                    self.assertEqual(tuple(surf.get_at((x, y))[:3]), tuple(gs.SURFACE)[:3], stage)

    def test_prompt_is_start_and_not_now_without_a_hint(self):
        calls = []
        real = gs.draw_voice_hint

        def wrap(*a, **k):
            calls.append(a[1] if len(a) > 1 else None)
            return real(*a, **k)

        gs.draw_voice_hint = wrap
        try:
            self._draw("prompt")
        finally:
            gs.draw_voice_hint = real
        self.assertEqual(calls, [])
        self.assertEqual([b[0] for b in gs.LAST_ENROLL_BUTTONS], ["start", "dismiss"])
        tile = gs.LAST_ENROLL_RECT
        self.assertEqual(gs.enroll_hit(tile.x + 30, tile.y + 40), "start")
        dismiss = next(r for i, r in gs.LAST_ENROLL_BUTTONS if i == "dismiss")
        self.assertEqual(gs.enroll_hit(*dismiss.center), "dismiss")
        labels = []
        real_font = gs.font

        class F:
            def __init__(self, inner):
                self.inner = inner

            def render(self, text, *a, **k):
                labels.append(text)
                return self.inner.render(text, *a, **k)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        gs.font = lambda kind, px: F(real_font(kind, px))
        try:
            self._draw("prompt")
        finally:
            gs.font = real_font
        self.assertIn("Not now", labels)
        self.assertNotIn("×", labels)
        self.assertNotIn("x", [t for t in labels if t.strip() in ("x", "×", "X")])

    def test_recording_has_stop_button_and_no_spoken_stop_hint(self):
        """0.13.7: the spoken stop is gone, so the tile no longer says Say "stop"; the Stop button stays."""
        calls = []
        real = gs.draw_voice_hint

        def wrap(*a, **k):
            calls.append(a[1] if len(a) > 1 else None)
            return real(*a, **k)

        gs.draw_voice_hint = wrap
        try:
            self._draw("recording", i=3)
        finally:
            gs.draw_voice_hint = real
        self.assertEqual(calls, [])
        self.assertEqual([b[0] for b in gs.LAST_ENROLL_BUTTONS], ["stop"])

    def test_other_tiles_arrive_as_chips(self):
        sports = {"type": "sports", "v": 1, "league": "City league", "status": "live", "period": "2nd",
                  "home": {"name": "Harbor", "abbr": "HC"}, "away": {"name": "Ridge", "abbr": "RV"},
                  "home_score": 1, "away_score": 0}
        gs.CHIPS.add({"bot": "juniper", "id": "g1", "card": sports, "expires": time.time() + 60}, time.time())
        self._draw("recording", i=3)
        self.assertEqual(len(gs.LAST_CHIP_RECTS), 1)
        chip = gs.LAST_CHIP_RECTS[0]
        self.assertLessEqual(chip.right, 2432)
        self.assertGreaterEqual(chip.top, 110)
        self.assertLessEqual(chip.bottom, 210)

    def test_hold_is_ten_seconds_only_after_the_result(self):
        self.assertEqual(gs.VOICE_HOLDS["enroll"], 30)
        for heard in ("0/5|passed|", "5/5|uploaded|", "0/5|error|"):
            self.assertEqual(gs.state_hold("enroll", False, heard), 10, heard)
        for heard in ("1/5|prompt|", "3/5|recording|", "3/5|quiet|", "0/5|queued|2"):
            self.assertEqual(gs.state_hold("enroll", False, heard), 30, heard)
        self.assertEqual(gs.state_hold("speaking", True, ""), gs.REPLY_HOLD)
        self.assertEqual(gs.state_hold("listening", False, ""), gs.VOICE_HOLDS["listening"])

    def test_copy_and_demo_use_five_clips(self):
        self.assertIn("It stays off until then.", gs.enroll_copy("Hey Juniper", 0, 5, "failed", "")[1])
        self.assertEqual(gs.enroll_copy("Hey Juniper", 0, 5, "failed", "2/5")[3], "2 of 5 test clips woke it")
        self.assertEqual(gs.enroll_copy("Hey Juniper", 0, 5, "failed", "10")[0], "Needs 10 more clips")
        self.assertEqual(gs.enroll_copy("Hey Juniper", 0, 5, "passed", "")[1], "Try it now.")
        self.assertEqual(gs.enroll_copy("Hey Juniper", 0, 5, "passed", "")[3], "All 5 clips saved ✓")
        src = inspect.getsource(gs.demo)
        self.assertIn("/5|", src)
        self.assertNotIn("/10|", src)

    def test_park_tile_moves_a_card_onto_the_chip_tray(self):
        sports = {"type": "calendar", "v": 1, "date": "2026-10-22",
                  "events": [{"time": "6:30", "title": "Practice"}]}
        tile = {"bot": "otto", "id": "day", "card": sports, "expires": time.time() + 30}
        self.assertTrue(gs.park_tile_for_enroll(tile, time.time()))
        self.assertEqual(tile, {})
        self.assertEqual(len(gs.CHIPS.visible(time.time())), 1)


if __name__ == "__main__":
    unittest.main()
