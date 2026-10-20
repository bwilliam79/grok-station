#!/usr/bin/env python3
"""Clock colon placement, thinking-ring phases, and partial framebuffer writes."""
import json, os, sys, tempfile, time, unittest

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
            gs._MARK_CACHE.clear()
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

    def test_no_persona_uses_echo_mark(self):
        L = gs.Layout(1920, 1080)
        surf = pygame.Surface((1920, 1080))
        gs._photo_cache.clear()
        gs._MARK_CACHE.clear()
        gs.draw_enroll(surf, L, time.localtime(), enroll_msg("prompt", 1, "", who=gs.VOICE_NAME, phrase="Hey Nova"))
        self.assertEqual(gs.LAST_PORTRAIT_ARGS[0], gs.VOICE_NAME)
        self.assertGreater(self._count(surf, gs.LAST_PORTRAIT_RECT, (63, 211, 182)), 20)  # Echo teal
        self.assertGreater(self._count(surf, gs.LAST_PORTRAIT_RECT, gs.SURFACE2), 10)     # surface-2 circle

    def _count(self, surf, rect, rgb, tol=28):
        n = 0
        rect = pygame.Rect(rect).clip(surf.get_rect())
        for y in range(rect.top, rect.bottom, 2):
            for x in range(rect.left, rect.right, 2):
                c = surf.get_at((x, y))
                if all(abs(c[i] - rgb[i]) <= tol for i in range(3)):
                    n += 1
        return n

    def test_no_persona_and_setup_render(self):
        """No-persona wake screen and setup screen, headless, at the small panel and at 1080p."""
        now = time.strptime("2026-10-19 19:42", "%Y-%m-%d %H:%M")
        sd = {"mode": "setup", "code": "K7P4QX", "host": "station.local", "url": "https://station.local",
              "fingerprint": ":".join(f"{(i * 37) % 256:02X}" for i in range(32))}
        for size in ((800, 480), (1920, 1080)):
            surf = pygame.Surface(size)
            L = gs.Layout(*size)
            gs.draw_enroll(surf, L, now, enroll_msg("prompt", 1, "", who=gs.VOICE_NAME, phrase="Hey Nova"))
            self.assertIsNotNone(gs.LAST_PORTRAIT_RECT)
            self.assertGreater(self._count(surf, gs.LAST_PORTRAIT_RECT, (63, 211, 182)), 8, size)
            gs.draw_setup(surf, L, now, sd)
            gs.draw_setup(surf, L, now, dict(sd, mode="reset", code="R4M9TW"))
            gs.draw_hotspot(surf, L, now, dict(sd, mode="hotspot", ssid="station-7F3A", password="hearth-4821",
                                                url="https://10.42.0.1"))

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


CARD_SIZES = ((800, 480), (1024, 768), (1280, 800), (1920, 1080), (2560, 1706), (3840, 2160))
EPOCH = 1_700_000_000
WEATHER_CARD = {"kind": "weather", "icon": "partly", "now_temp": 64, "units": "°F", "conditions": "partly cloudy",
                 "today": {"high": 71, "low": 52}, "tomorrow": {"high": 68, "low": 49}}
TIME_CARD = {"kind": "time", "zone_label": "London", "hhmm": "11:05 PM", "day": "Mon", "offset": "+6 h"}
CONVERT_CARD = {"kind": "convert", "value": "350", "from_unit": "°F", "result": "176.67", "to_unit": "°C"}


def _chip(n, left, ringing=False, label="pasta"):
    return {"id": f"t{n:05d}", "label": label, "ends_at_epoch": EPOCH + left, "ringing": ringing}


class ToolCards(unittest.TestCase):
    def setUp(self):
        gs.TIMER_CHIPS[:] = []
        gs.TIMER_EPOCH = EPOCH

    def tearDown(self):
        gs.TIMER_CHIPS[:] = []
        gs.TIMER_EPOCH = None

    def test_bad_payloads_are_ignored(self):
        huge = {"kind": "weather", "now_temp": 70, "units": "°F", "conditions": "x" * 600}
        self.assertGreater(len(json.dumps(huge)), gs._CARD_MAX)
        self.assertIsNone(gs.clean_card(huge))
        self.assertIsNone(gs.clean_card({"kind": "weather", "icon": "meteor", "place": "Chicago", "lat": 1, "lon": 2}))
        self.assertIsNone(gs.clean_card({"kind": "time", "zone_label": "London"}))
        self.assertIsNone(gs.clean_card({"kind": "convert", "value": "1", "from_unit": "m", "result": "3"}))
        self.assertIsNone(gs.clean_card("nope"))
        self.assertIsNone(gs.clean_timers({"id": "abcd"}))  # not a list: keep the previous set
        ev = gs.state_event({"state": "speaking", "text": "Hi", "card": {
            "type": "card", "kind": "weather", "icon": "sun", "now_temp": 61, "units": "°F",
            "conditions": "clear", "today": {"high": 70, "low": 40}, "tomorrow": {"high": True, "low": 30},
            "place": "Chicago", "lat": 41.8, "lon": -87.6}})
        self.assertEqual(ev["card"]["icon"], "sun")
        self.assertNotIn("tomorrow", ev["card"])  # high was a bool
        blob = json.dumps(ev["card"]).lower()
        for banned in ("place", "chicago", "lat", "lon", "41.8"):
            self.assertNotIn(banned, blob)
        ev = gs.state_event({"state": "speaking", "card": huge})
        self.assertTrue(ev["has_card"])
        self.assertIsNone(ev["card"])
        prior = {"kind": "time", "hhmm": "5:05 PM"}
        self.assertEqual(gs.merge_card(prior, True, None, True), prior)   # bad card does not wipe
        self.assertIsNone(gs.merge_card(prior, False, None, True))        # done, and no card was sent
        self.assertEqual(gs.merge_card(prior, False, None, False), prior)  # in-progress post keeps it
        with self.assertRaises(ValueError):
            gs.state_event({})
        with self.assertRaises(ValueError):
            gs.state_event({"timers": "nope"})
        with self.assertRaises(ValueError):
            gs.state_event({"state": "asleep"})
        ev = gs.state_event({"timers": [
            {"id": "abcd12", "label": "pasta!", "ends_at_epoch": EPOCH, "ringing": False},
            {"id": "NO", "ends_at_epoch": EPOCH},
            {"id": "abcd12", "label": "dup", "ends_at_epoch": EPOCH + 5},
            {"id": "zzzz99", "ends_at_epoch": True},
        ]})
        self.assertEqual(len(ev["timers"]), 1)
        self.assertEqual(ev["timers"][0]["label"], "pasta")
        ev = gs.state_event({"state": "idle", "timers": []})
        self.assertEqual(ev["timers"], [])
        self.assertEqual(ev["state"], "idle")
        posted = pygame.event.Event(gs.STATE, **gs.state_event({"card": TIME_CARD, "timers": []}))
        self.assertIsNone(posted.state)
        self.assertEqual(posted.card["hhmm"], "11:05 PM")

    def test_cards_have_no_wake_hint(self):
        with open(gs.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("to wake", src)
        self.assertNotIn("say hey", src.lower())
        for card in (WEATHER_CARD, TIME_CARD, CONVERT_CARD):
            for _role, text in gs._card_lines(gs.clean_card(card)):
                self.assertNotIn("wake", text.lower())
                self.assertNotIn("hey", text.lower())

    def _draw(self, size, painter, args):
        seen = []
        real = gs.font

        def wrapped(kind, px):
            seen.append(int(px))
            return real(kind, px)

        surf = pygame.Surface(size)
        gs.font = wrapped
        try:
            painter(surf, gs.Layout(*size), *args)
        finally:
            gs.font = real
        self.assertTrue(seen)
        self.assertGreaterEqual(min(seen), 14, size)
        return surf

    def _assert_safe(self, size, *rects):
        side, top, bot = gs.safe_insets(*size)
        safe = pygame.Rect(side, top, size[0] - 2 * side, size[1] - top - bot)
        for rect in rects:
            self.assertIsNotNone(rect, size)
            self.assertTrue(safe.contains(rect), f"{size} {rect} outside {safe}")

    def _no_overlap(self, a, b, what):
        if a and b:
            self.assertFalse(a.colliderect(b), f"{what}: {a} vs {b}")

    def test_cards_and_timers_at_every_size(self):
        now = time.strptime("2026-10-19 17:05", "%Y-%m-%d %H:%M")
        running = [_chip(1, 125, label="pasta"), _chip(2, 90, label="tea"), _chip(3, 600, label="eggs"),
                   _chip(4, 3700, label="rice"), _chip(5, 20, label="soup")]
        for size in CARD_SIZES:
            for card in (WEATHER_CARD, TIME_CARD, CONVERT_CARD):
                gs.TIMER_CHIPS[:] = []
                self._draw(size, gs.draw_session, (now, {"from": "Grok", "voice": True, "heard": "a question",
                                                          "text": "Here you go.", "state": "speaking", "card": card}))
                self._assert_safe(size, gs.LAST_CARD_RECT)
                self._no_overlap(gs.LAST_CARD_RECT, gs.LAST_TOPBAR_RECT, f"card/topbar {size}")
                self.assertIsNone(gs.LAST_TIMER_RECT)
                # 4:3 stacks the card under the avatar; wider aspects stay side by side
                if size == (1024, 768):
                    self.assertGreaterEqual(gs.LAST_CARD_RECT.top, gs.LAST_PORTRAIT_RECT.bottom - 2)
                if size in ((800, 480), (2560, 1706)):
                    self.assertGreaterEqual(gs.LAST_CARD_RECT.left, gs.LAST_PORTRAIT_RECT.right - 2)
                self._no_overlap(gs.LAST_CARD_RECT, gs.LAST_PORTRAIT_RECT, f"card/portrait {size}")
            for chips in (running[:1], running, [_chip(1, -5, ringing=True, label="eggs")]):
                gs.TIMER_CHIPS[:] = [dict(c) for c in chips]
                self._draw(size, gs.draw_session, (now, {"from": "Grok", "voice": True, "heard": "",
                                                          "text": "Here you go.", "state": "speaking", "card": TIME_CARD}))
                self._assert_safe(size, gs.LAST_CARD_RECT, gs.LAST_TIMER_RECT)
                self._no_overlap(gs.LAST_TIMER_RECT, gs.LAST_TOPBAR_RECT, f"strip/topbar {size}")
                self._no_overlap(gs.LAST_TIMER_RECT, gs.LAST_CARD_RECT, f"strip/card {size}")
                self._no_overlap(gs.LAST_TIMER_RECT, gs.LAST_PORTRAIT_RECT, f"strip/portrait {size}")
            wx = gs.Weather()
            wx.data, wx.ok_at = {"temp": 64, "code": 2, "day": True, "hi": 71, "lo": 52}, EPOCH
            gs.TIMER_CHIPS[:] = [dict(c) for c in running]
            self._draw(size, gs.draw_idle, (now, wx, True))
            self._assert_safe(size, gs.LAST_TIMER_RECT)
            for other in (gs.LAST_CLOCK_RECT, gs.LAST_DATE_RECT, gs.LAST_WEATHER_RECT):
                self._no_overlap(gs.LAST_TIMER_RECT, other, f"idle {size}")
            off = gs.Weather()
            off.ok_at = off.started = 0
            old_loc = gs.LAT, gs.LON
            gs.LAT, gs.LON = "41.0", "-87.0"  # offline badge only exists once a location is configured
            try:
                self._draw(size, gs.draw_idle, (now, off, True))
            finally:
                gs.LAT, gs.LON = old_loc
            self._assert_safe(size, gs.LAST_TIMER_RECT)
            self._no_overlap(gs.LAST_TIMER_RECT, gs.LAST_OFFLINE_RECT, f"offline {size}")
            self._no_overlap(gs.LAST_TIMER_RECT, gs.LAST_CLOCK_RECT, f"offline clock {size}")
            gs.draw_night(pygame.Surface(size), gs.Layout(*size), now, True)
            self.assertIsNone(gs.LAST_TIMER_RECT)  # the night clock does not carry the strip

    def test_timer_strip_states_and_local_repaint(self):
        size = (1280, 800)
        now = time.strptime("2026-10-19 17:05", "%Y-%m-%d %H:%M")
        L = gs.Layout(*size)
        seen = []
        real = gs.font

        class RecordingFont:
            def __init__(self, inner):
                self._inner = inner

            def render(self, text, *a, **k):
                seen.append(str(text))
                return self._inner.render(text, *a, **k)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        def wrapped(kind, px):
            return RecordingFont(real(kind, px))

        gs.font = wrapped
        try:
            surf = pygame.Surface(size)
            gs.TIMER_CHIPS[:] = [_chip(i, left, label=label) for i, (left, label) in
                                 enumerate(((20, "soup"), (90, "tea"), (125, "pasta"), (600, "eggs"), (50, "rice")), 1)]
            gs.draw_idle(surf, L, now, gs.Weather())
            self.assertIn("+1", seen)
            self.assertIn("02:05", seen)
            seen.clear()
            gs.TIMER_CHIPS[:] = [_chip(1, 3700, label="rice")]
            gs.draw_timer_strip(surf, L)
            self.assertIn("1:01:40", seen)
            seen.clear()
            gs.TIMER_EPOCH = EPOCH  # even second: ringing chip is the amber fill
            gs.TIMER_CHIPS[:] = [_chip(1, -5, ringing=True, label="eggs")]
            gs.draw_timer_strip(surf, L)
            self.assertIn("Done", seen)
            rect = gs.LAST_TIMER_RECT
            even = surf.copy()
            gs.TIMER_EPOCH = EPOCH + 1
            gs.draw_timer_strip(surf, L)
            spot = None
            for y in range(rect.top + 2, rect.bottom - 2, 2):
                for x in range(rect.left, rect.right, 3):
                    c = even.get_at((x, y))
                    d = surf.get_at((x, y))
                    if c.r > 200 and c.g > 140 and c.b < 120 and d.r < 80:
                        spot = (c, d)
                        break
                if spot:
                    break
            self.assertIsNotNone(spot, "ringing chip did not pulse between amber and the amber well")
        finally:
            gs.font = real
        gs.TIMER_EPOCH = EPOCH
        gs.TIMER_CHIPS[:] = [_chip(1, 125, label="pasta")]
        surf = pygame.Surface(size)
        gs.draw_idle(surf, L, now, gs.Weather())
        rect = pygame.Rect(gs.LAST_TIMER_RECT)
        before = bytes(surf.get_buffer().raw)
        gs.TIMER_EPOCH = EPOCH + 61

        class Disp:
            screen = surf
            flushed = None
            def flush_rect(self, r):
                self.flushed = pygame.Rect(r)

        disp = Disp()
        gs.repaint_timer_strip(disp)
        after = bytes(surf.get_buffer().raw)
        self.assertEqual(disp.flushed, rect)
        pitch = surf.get_pitch()
        self.assertNotEqual(before, after)
        for y in range(size[1]):
            if rect.top <= y < rect.bottom:
                continue
            self.assertEqual(before[y * pitch:(y + 1) * pitch], after[y * pitch:(y + 1) * pitch], f"row {y}")
