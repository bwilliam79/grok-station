"""0.13.42: the spoken question wraps inside the persona tile and scrolls with the reply."""
import os
import sys
import time
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import grok_station as gs  # noqa: E402
import pygame  # noqa: E402

HEARD = (
    "Who does Arkansas play this weekend in football and what time does that game start tonight "
    "and is it at home or on the road and what channel is carrying the broadcast until kickoff"
)
REPLY = "Arkansas plays Saturday night."


def _quote_bands(surf, rect):
    """Rows of the spoken question. A one-line ellipsis is a single band."""
    target = tuple(gs.MUTED)[:3]
    dense = []
    for y in range(rect.top, min(rect.bottom, surf.get_height())):
        hits = 0
        for x in range(rect.left, min(rect.right, surf.get_width())):
            color = surf.get_at((x, y))
            if all(abs(int(color[i]) - target[i]) <= 24 for i in range(3)):
                hits += 1
        if hits >= 100:
            dense.append(y)
    bands = []
    if not dense:
        return bands
    start = prev = dense[0]
    for y in dense[1:]:
        if y - prev > 3:
            if prev - start >= 8:
                bands.append((start, prev))
            start = y
        prev = y
    if prev - start >= 8:
        bands.append((start, prev))
    return bands


class WrappedQuestion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.display.init()
        pygame.font.init()

    def test_long_question_wraps_and_scrolls_inside_the_tile(self):
        slot = getattr(gs, "_HEARD_SCROLL", None)
        if slot is not None:
            slot["text"] = None
            slot["tele"] = None
        size = (1280, 800)
        surf = pygame.Surface(size)
        layout = gs.Layout(*size)
        now = 1_000.0
        wx = type("W", (), {"data": {"temp": 72, "code": 1, "day": True, "hi": 80, "lo": 60},
                            "offline": lambda self: False})()
        msg = {"from": "Otto", "voice": True, "state": "speaking", "heard": HEARD, "text": REPLY}
        gs.draw_zone(surf, layout, time.strptime("2026-06-15 10:08", "%Y-%m-%d %H:%M"),
                     wx, zone={"voice": msg, "state": "speaking"}, now_s=now)
        zone = gs.LAST_ZONE_RECT
        column = pygame.Rect(gs.panel_text_x(layout, zone), zone.top,
                             gs.panel_text_width(layout, zone), zone.h)
        bands = _quote_bands(surf, column)
        self.assertGreaterEqual(len(bands), 2, bands)
        heard = gs.LAST_HEARD
        self.assertIsNotNone(heard)
        lines = heard["lines"]
        joined = " ".join(lines)
        self.assertGreaterEqual(len(lines), 2)
        self.assertNotIn("…", joined)
        self.assertNotIn("...", joined)
        self.assertIn("Who", lines[0])
        self.assertIn("kickoff", joined)
        self.assertGreater(len(lines), 1)
        self.assertNotIn("kickoff", lines[0])
        rect = heard["rect"]
        self.assertTrue(zone.contains(rect))
        self.assertGreaterEqual(rect.left, column.left)
        self.assertLessEqual(rect.right, column.right)
        self.assertTrue(heard["tele"].scrolls)
        keep = os.environ.get("GS_ARTIFACTS", "")
        if keep:
            os.makedirs(keep, exist_ok=True)
            pygame.image.save(surf, os.path.join(keep, "long-question-1280x800.png"))
        before = _quote_bands(surf, rect)
        later = now + gs.Teleprompter.HOLD_S + 3
        gs.paint_tele(surf, later)
        after = _quote_bands(surf, rect)
        self.assertNotEqual(before, after)
        self.assertTrue(zone.contains(gs.LAST_HEARD["rect"]))

    def test_a_new_session_starts_the_question_at_the_top(self):
        """The scroller is kept across paints, and a later session does not resume it."""
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from test_gs1216_bottom_zone import Harness
        gs.reset_heard_scroll()
        now = 1000.0
        quote = "“" + HEARD + "”"
        tele = gs.heard_window(quote, 2, now)
        tele.set_geometry(8, 40, 1.0)
        tele.step(now + 8)
        self.assertGreater(tele.offset, 0.5)
        gs.reset_heard_scroll()
        again = gs.heard_window(quote, 2, now + 8)
        self.assertIsNot(again, tele)
        self.assertEqual(again.offset, 0.0)
        seen = []

        def grab():
            slot = gs._HEARD_SCROLL.get("tele")
            seen.append(None if slot is None else slot.offset)

        h = Harness(size=(1280, 800))
        h.run(
            h.state("speaking", heard=HEARD, text=REPLY),
            h.advance(6),
            h.call(grab),
            h.state("idle"),
            h.state("speaking", heard=HEARD, text=REPLY),
        )
        self.assertGreater(seen[0], 0.5, seen)
        fresh = gs._HEARD_SCROLL.get("tele")
        self.assertIsNotNone(fresh)
        self.assertLess(fresh.offset, 0.5)


if __name__ == "__main__":
    unittest.main()
