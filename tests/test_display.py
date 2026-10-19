#!/usr/bin/env python3
"""Clock colon placement, thinking-ring phases, and partial framebuffer writes."""
import os, sys, tempfile, time, unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import grok_station as gs
import pygame


def setUpModule():
    pygame.display.init()
    pygame.font.init()


def ink_box(surf, rect, bg):
    """Inclusive pixel bounds of anything that isn't bg inside rect, or None."""
    rect = pygame.Rect(rect).clip(surf.get_rect())
    bgm = surf.map_rgb(bg)
    pa = pygame.PixelArray(surf)
    minx = miny = 10 ** 9
    maxx = maxy = -1
    try:
        for y in range(rect.top, rect.bottom):
            for x in range(rect.left, rect.right):
                if pa[x, y] != bgm:
                    if x < minx:
                        minx = x
                    if x > maxx:
                        maxx = x
                    if y < miny:
                        miny = y
                    if y > maxy:
                        maxy = y
    finally:
        del pa
    if maxx < 0:
        return None
    return minx, miny, maxx, maxy


def box_center(box):
    x0, y0, x1, y1 = box
    return (x0 + x1 + 1) / 2.0, (y0 + y1 + 1) / 2.0


class ClockTests(unittest.TestCase):
    def test_colon_centered_at_panel_size(self):
        L = gs.Layout(2560, 1706)
        for kind, px in (("display", L(320)), ("night", L(340))):
            f = gs.font(kind, px)
            for hhmm in ("7:42", "12:05", "10:10"):
                surf = pygame.Surface((1800, 1000))
                surf.fill(gs.BG)
                x, baseline = 30, 780
                gs.draw_clock(surf, kind, px, hhmm, gs.TEXT, x, baseline, True)
                hour, minute = hhmm.split(":")
                pens, pen = [], 0
                for m in f.metrics(hhmm):
                    pens.append(pen)
                    pen += m[4]
                top = baseline - f.get_ascent()
                hbr = f.render(hour, True, gs.TEXT).get_bounding_rect()
                mbr = f.render(minute, True, gs.TEXT).get_bounding_rect()
                hour_right = x + hbr.right
                min_left = x + pens[len(hour) + 1] + mbr.left
                gap_cx = (hour_right + min_left) / 2.0
                # stay off the digit fringe; the colon sits in the middle of a wide gap
                region = pygame.Rect(int(hour_right) + 8, top - 30, int(min_left) - int(hour_right) - 16, f.get_height() + 60)
                box = ink_box(surf, region, gs.BG)
                self.assertIsNotNone(box, f"{kind} {px} {hhmm} drew no colon")
                cx, cy = box_center(box)
                ref = f.render("0123456789", True, gs.TEXT).get_bounding_rect()
                digit_cy = top + ref.y + ref.h / 2.0
                self.assertLessEqual(abs(cx - gap_cx), 2, f"{kind} {hhmm} colon x {cx:.1f} vs gap {gap_cx:.1f}")
                self.assertLessEqual(abs(cy - digit_cy), 2, f"{kind} {hhmm} colon y {cy:.1f} vs digits {digit_cy:.1f}")

    def test_date_centered_on_digits(self):
        L = gs.Layout(2560, 1706)
        surf = pygame.Surface((2560, 1706))
        wx = gs.Weather()
        wx.data = None  # no weather glyph to confuse the ink search
        for hour, minute in ((7, 42), (12, 5), (10, 10)):
            surf.fill(gs.BG)
            now = time.strptime(f"2026-10-18 {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M")
            gs.draw_idle(surf, L, now, wx, colon_on=True)
            digits = gs.LAST_DIGITS_RECT
            self.assertIsNotNone(digits)
            # date sits just under the clock, on the left; weather (if any) is on the right
            window = pygame.Rect(0, digits.bottom + 2, 1500, L(180))
            box = ink_box(surf, window, gs.BG)
            self.assertIsNotNone(box, f"{hour}:{minute:02d} date ink missing")
            cx, _cy = box_center(box)
            dcx = digits.x + digits.w / 2.0
            self.assertLessEqual(abs(cx - dcx), 2, f"{hour}:{minute:02d} date x {cx:.1f} vs digits {dcx:.1f}")


class RingTests(unittest.TestCase):
    def test_thinking_ring_phases_differ(self):
        frames = [gs.portrait("Grok", 180, 10, "thinking", t, photo_ok=False)[0].get_buffer().raw
                  for t in (0.0, 0.33, 0.66)]
        self.assertNotEqual(frames[0], frames[1])
        self.assertNotEqual(frames[1], frames[2])
        self.assertNotEqual(frames[0], frames[2])
        same = gs.portrait("Grok", 180, 10, "thinking", 1.0, photo_ok=False)[0].get_buffer().raw
        self.assertEqual(frames[0], same)  # phase wraps at 1

    def test_photo_crop_is_cached(self):
        calls = []
        real = pygame.image.load

        def wrapped(path):
            calls.append(path)
            return real(path)

        pygame.image.load = wrapped
        try:
            gs._photo_cache.clear()
            gs.portrait("Grok", 100, 8, "speaking", 1.0, photo_ok=True)
            gs.portrait("Grok", 100, 8, "thinking", 0.4, photo_ok=True)
            self.assertEqual(len(calls), 1)
        finally:
            pygame.image.load = real
            gs._photo_cache.clear()


class FlushTests(unittest.TestCase):
    def test_flush_rect_writes_only_that_span(self):
        w, h = 48, 24
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            disp = gs.Display(fb=path, size=(w, h))
            pitch, bpp = disp.screen.get_pitch(), disp.screen.get_bytesize()
            self.assertEqual(bpp, 2)
            blank = b"\x11" * (pitch * h)
            with open(path, "wb") as f:
                f.write(blank)
            disp.screen.fill(pygame.Color(0, 255, 0))
            rect = pygame.Rect(5, 3, 7, 4)
            disp.flush_rect(rect)
            with open(path, "rb") as f:
                data = f.read()
            raw = bytes(disp.screen.get_buffer().raw)
            self.assertEqual(len(data), pitch * h)
            for y in range(h):
                row = y * pitch
                if rect.top <= y < rect.bottom:
                    a, b = rect.left * bpp, rect.right * bpp
                    self.assertEqual(data[row + a:row + b], raw[row + a:row + b], f"row {y} inside")
                    self.assertEqual(data[row:row + a], blank[row:row + a], f"row {y} left")
                    self.assertEqual(data[row + b:row + pitch], blank[row + b:row + pitch], f"row {y} right")
                else:
                    self.assertEqual(data[row:row + pitch], blank[row:row + pitch], f"row {y}")
            disp.fb = None  # no framebuffer: flush must not touch the file
            with open(path, "wb") as f:
                f.write(blank)
            disp.screen.fill(pygame.Color(255, 0, 0))
            disp.flush_rect(rect)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), blank)
            disp.flush_rect(None)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
