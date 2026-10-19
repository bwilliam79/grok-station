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



LONG_STATUS = ["Speak naturally, about an arm’s length away", "Didn’t hear it, a bit louder please",
               "Too loud, step back a little", "Recording… speak now", "Uploading failed, retrying the upload to the relay now "
               "and again if needed, please keep still while we wait"]
LONG_HEARD = ("could you please remind me tomorrow morning at seven thirty to call the pharmacy about the prescription refill "
              "and also add milk eggs and bread to the shopping list")


class FooterFit(unittest.TestCase):
    def check_margins(self, surf, L):
        h = surf.get_height()
        for x0, x1 in ((0, L.m - 2), (L.w - L.m + 2, L.w)):
            self.assertIsNone(ink_box(surf, (x0, L(140), x1 - x0, h - L(140)), gs.BG), "ink in side margin")

    def test_enroll_status_fits(self):
        for size in ((2560, 1706), (1920, 1080), (1280, 800)):
            L = gs.Layout(*size)
            surf = pygame.Surface(size)
            for st in LONG_STATUS:
                gs.draw_enroll(surf, L, time.localtime(), {"text": "Say “Hey Hank”", "heard": f"25/25|{st}"})
                self.check_margins(surf, L)
                f, lines, _ = gs.fit_text("body", st, L.w - 2 * L.m, L(150), L(60), L(36), lh=1.05, max_lines=2)
                self.assertLessEqual(len(lines), 2)
                self.assertTrue(all(f.size(l)[0] <= L.w - 2 * L.m for l in lines))

    def test_heard_fits(self):
        for size in ((2560, 1706), (1280, 800)):
            L = gs.Layout(*size)
            surf = pygame.Surface(size)
            for who in ("", "Grok", "Piper"):
                gs.draw_session(surf, L, time.localtime(), {"from": who, "heard": LONG_HEARD, "text": "Okay.", "state": "speaking"})
                self.check_margins(surf, L)

if __name__ == "__main__":
    unittest.main()


class TestListeningPulse(unittest.TestCase):
    def test_pulse_changes_and_loops(self):
        import grok_station as gs
        f = lambda t: gs.portrait("Grok", 180, 10, "listening", t, photo_ok=False)[0].get_buffer().raw
        self.assertNotEqual(f(0.0), f(0.5))   # ring differs mid-pulse
        self.assertEqual(f(0.0), f(1.0))      # one loop per period
        self.assertEqual(f(0.25), f(0.75))    # symmetric cosine pulse
        self.assertEqual(gs.ANIM["listening"], 1.0)
        self.assertIn("thinking", gs.ANIM)
        self.assertNotIn("speaking", gs.ANIM); self.assertNotIn("idle", gs.ANIM)


class TestListeningScreen(unittest.TestCase):
    def test_no_wake_hint(self):
        src = open(gs.__file__, encoding="utf-8").read()
        self.assertNotIn("to wake\\u201d", src); self.assertNotIn("” to wake", src)


class ListeningRing(unittest.TestCase):
    def render(self, t):
        c = pygame.Surface((400, 400), pygame.SRCALPHA)
        gs.ring_listening(c, 200, 200, 150, 20, None, t)
        return c

    def test_solid_continuous_and_pulses(self):
        import math
        widths = []
        for t in (0.0, 0.5):
            c = self.render(t)
            for k in range(360):  # every angle on the ring centerline is opaque: no gaps, no ticks
                a = math.radians(k)
                self.assertGreater(c.get_at((int(200 + 160 * math.cos(a)), int(200 + 160 * math.sin(a)))).a, 200)
            widths.append(sum(c.get_at((200 + x, 200)).a > 200 for x in range(140, 200)))
        self.assertGreater(widths[1], widths[0])  # thicker at the pulse peak


SIZES = ((800, 480), (1280, 720), (1920, 1080), (2560, 1706), (3840, 2160))
STAGES = [("prompt", 1, ""), ("ready", 3, ""), ("recording", 3, ""), ("loud", 3, ""), ("quiet", 3, ""), ("saved", 3, ""),
          ("uploaded", 10, ""), ("queued", 0, "2"), ("passed", 0, ""), ("failed", 0, "10"), ("error", 0, "")]


def enroll_msg(stage, i, extra, who="Finn", phrase="Hey Finn"):
    return {"from": who, "voice": True, "state": "enroll", "text": f"“{phrase}”", "heard": f"{i}/10|{stage}|{extra}"}


class EnrollScreens(unittest.TestCase):
    def margins_clear(self, surf, L):
        w, h = surf.get_size()
        for x0, x1 in ((0, L.m - 2), (w - L.m + 2, w)):
            self.assertIsNone(ink_box(surf, (x0, L(140), x1 - x0, h - L(140) - 2), gs.BG))
        self.assertIsNone(ink_box(surf, (0, h - L(40), w, L(40)), gs.BG), "ink in the bottom safe area")

    def test_all_stages_all_sizes(self):
        for size in SIZES:
            L = gs.Layout(*size)
            surf = pygame.Surface(size)
            for stage, i, extra in STAGES:
                for phrase in ("Hey Finn", "Hey Bartholomew"):  # the longest allowed-looking phrase must still fit
                    gs.draw_session(surf, L, time.localtime(), enroll_msg(stage, i, extra, phrase=phrase), "enroll", 1.0)
                    self.margins_clear(surf, L)

    def test_copy(self):
        self.assertEqual(gs.enroll_copy("Hey Finn", 1, 10, "prompt", "")[3], "10 clips · about 1 min")
        self.assertEqual(gs.enroll_copy("Hey Finn", 3, 10, "ready", "")[3], "Clip 3 of 10")
        self.assertEqual(gs.enroll_copy("Hey Finn", 3, 10, "saved", "")[3], "Clip 4 of 10")
        self.assertIn("2nd in line", gs.enroll_copy("Hey Finn", 0, 0, "queued", "2")[1])
        self.assertEqual(gs.enroll_copy("Hey Finn", 0, 0, "failed", "10")[0], "Needs 10 more clips")
        self.assertEqual(gs.enroll_copy("Hey Finn", 0, 0, "passed", "")[0], "“Hey Finn” is ready")
        self.assertEqual(gs.enroll_parse({"heard": "3/25|Recording… speak now"})[2], "custom")  # old wake build

    def test_no_persona_uses_grok_mark(self):
        L = gs.Layout(1920, 1080)
        surf = pygame.Surface((1920, 1080))
        gs.draw_enroll(surf, L, time.localtime(), enroll_msg("prompt", 1, "", who=gs.VOICE_NAME, phrase="Hey Nova"))
        self.assertEqual(gs.LAST_PORTRAIT_ARGS[0], gs.VOICE_NAME)

    def test_anim_specs(self):
        self.assertEqual(gs.anim_of(enroll_msg("ready", 1, ""))[1:], (0.9, False))
        self.assertEqual(gs.anim_of(enroll_msg("saved", 1, ""))[1:], (0.3, False))
        self.assertTrue(gs.anim_of(enroll_msg("recording", 1, ""))[2])
        self.assertIsNone(gs.anim_of(enroll_msg("loud", 1, "")))

    def coverage(self, painter, t):
        c = pygame.Surface((400, 400), pygame.SRCALPHA)
        painter(c, 200, 200, 150, 20, None, t)
        return sum(c.get_at((x, y)).a > 200 for x in range(0, 400, 2) for y in range(0, 400, 2))

    def test_ready_ring_fills(self):
        a, b, c = (self.coverage(gs.ring_ready, t) for t in (0.1, 0.5, 1.0))
        self.assertLess(a, b); self.assertLess(b, c)

    def test_saved_pulse(self):
        self.assertGreater(self.coverage(gs.ring_saved, 0.3), self.coverage(gs.ring_saved, 1.0))


class Resolution(unittest.TestCase):
    def test_canvas_rect(self):
        self.assertEqual(gs.canvas_rect(2560, 1706), pygame.Rect(0, 0, 2560, 1706))
        self.assertEqual(gs.canvas_rect(5120, 2880), pygame.Rect(640, 360, 3840, 2160))  # letterboxed
        self.assertEqual(gs.canvas_rect(640, 400), pygame.Rect(0, 0, 640, 400))          # best effort below min

    def test_letterbox_display(self):
        d = gs.Display(fb=None, size=(5120, 2880))
        self.assertEqual(d.screen.get_size(), (3840, 2160))
        self.assertEqual(d.L.w, 3840)

    def test_scale_is_proportional(self):
        for size in SIZES:
            L = gs.Layout(*size)
            self.assertAlmostEqual(L.s, min(size[0] / 1920, size[1] / 1080))
            self.assertGreater(L.m, 0)
