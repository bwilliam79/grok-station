"""0.13.42: every content tile stays inside its panel at each supported size.

Painted ink is the glyph bounding rect at the blit, clipped to what the surface
actually shows. It must sit inside the panel. Two text or score inks must not overlap.
A sports score must also clear the next-game divider by the column's bottom inset.
"""
import os
import re
import struct
import sys
import tempfile
import time
import unittest
import zlib

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import pygame  # noqa: E402
import grok_station as gs  # noqa: E402

pygame.display.init()
pygame.font.init()

SIZES = ((1280, 800), (1920, 1080), (2560, 1706), (1024, 600), (800, 480))
NOW = time.strptime("2026-06-15 10:08", "%Y-%m-%d %H:%M")
HEARD = (
    "Who does Arkansas play this weekend in football and what time does that game start tonight "
    "and is it at home or on the road and what channel is carrying the broadcast until kickoff"
)


class _Wx:
    data = {"temp": 72, "code": 1, "day": True, "hi": 80, "lo": 60}

    def offline(self):
        return False


def _sports(status, schedule, badge):
    home = {"name": "Kansas City", "abbr": "KC"}
    away = {"name": "Denver", "abbr": "DEN"}
    if badge:
        home["logo_badge"] = True
        away["logo_badge"] = True
    card = {
        "v": 1, "type": "sports", "league": "NFL",
        "home": home, "away": away,
        "home_score": 27, "away_score": 14,
        "status": status, "period": "Q4" if status == "live" else "Final",
        "schedule": [{"label": "Kansas City at Denver", "when": "Sun 1:00 PM"}] if schedule else [],
    }
    return card


def _watch():
    gs.PAINT_MARKS = []
    return gs.PAINT_MARKS


def _divider(surf, panel):
    rule = gs.RULE[:3]
    hits = []
    y0, y1 = panel.top + panel.h // 3, panel.bottom - 4
    for y in range(y0, y1):
        need = got = 0
        for x in range(panel.left + 8, panel.right - 8, 2):
            need += 1
            color = surf.get_at((x, y))
            if all(abs(int(color[i]) - rule[i]) <= 8 for i in range(3)):
                got += 1
        if need and got / need > 0.85:
            hits.append(y)
    return hits[0] if hits else None


def _short_field(text):
    """A time or a score. Those stay whole. A title may still end in an ellipsis."""
    s = " ".join(str(text).split())
    if not s or len(s) > 16:
        return False
    if re.search(r"\d:\d", s) or re.fullmatch(r"\d{1,3}", s):
        return True
    return False


def _assert_panel(case, panel, marks, surf, layout, sports_row, clipped=()):
    if clipped:
        raise AssertionError("%s ellipsized %s" % (case, clipped[0]))
    texts = []
    for rect, text in marks:
        if not rect.colliderect(panel):
            continue
        if not panel.contains(rect):
            raise AssertionError("%s ink %s leaves panel %s" % (case, rect, panel))
        if text:
            texts.append(rect)
    for i, a in enumerate(texts):
        for b in texts[i + 1:]:
            overlap = a.clip(b)
            if overlap.w <= 0 or overlap.h <= 0:
                continue
            # A highlighted word is painted again on its own line. That is one phrase, not two.
            if a.contains(b) or b.contains(a):
                continue
            raise AssertionError("%s text overlap %s and %s" % (case, a, b))
    if not sports_row:
        return
    line = _divider(surf, panel)
    if line is None:
        raise AssertionError("%s has a next game but no divider" % case)
    inset = layout(16)
    for rect in texts:
        if rect.centery < line and rect.bottom > line - inset:
            raise AssertionError("%s score ink %s meets the next-game row at %s" % (case, rect, line))


class TileInk(unittest.TestCase):
    def test_every_tile_fits_its_panel(self):
        for size in SIZES:
            layout = gs.Layout(*size)
            panel = gs.bottom_panel_rect(layout)
            cases = []

            def add(name, paint, box, sports_row=False):
                cases.append((name, paint, box, sports_row))

            for status in ("live", "final"):
                for schedule in (True, False):
                    for badge in (True, False):
                        card = _sports(status, schedule, badge)
                        label = "sports-%s-%s-%s" % (status, "next" if schedule else "none", "badge" if badge else "plain")
                        add(label, lambda s, c=card, r=panel: gs.draw_sports(s, layout, c, r), panel, schedule)
            calendar = {"v": 1, "type": "calendar", "date": "2026-10-22", "events": [
                {"time": "11:15 AM", "title": "Harbor practice"},
                {"time": "12:15 PM", "title": "Lunch with the team"},
                {"time": "1:30 PM", "title": "Market run"},
                {"time": "3:40 PM", "title": "Pickup at the school"},
                {"time": "7:00 PM", "title": "League game"},
            ]}
            add("calendar", lambda s, r=panel: gs.draw_calendar(s, layout, calendar, r), panel)
            recipe = {"v": 1, "type": "recipe", "title": "Weeknight soup", "servings": 4,
                      "ingredients": ["onion", "carrots", "stock", "thyme", "pepper"],
                      "steps": ["Chop the vegetables.", "Simmer twenty minutes.", "Season and serve."],
                      "step": 2}
            screen = pygame.Rect(0, 0, *size)
            add("recipe", lambda s: gs.draw_recipe(s, layout, recipe), screen)
            for kind, card in (
                ("weather", {"kind": "weather", "icon": "clear", "now_temp": 72, "units": "\u00b0F",
                             "conditions": "Clear", "today": {"high": 80, "low": 60},
                             "tomorrow": {"high": 78, "low": 55}}),
                ("time", {"kind": "time", "zone_label": "Chicago", "hhmm": "10:08", "day": "Mon", "offset": "CDT"}),
                ("convert", {"kind": "convert", "value": "2", "from_unit": "cups", "result": "32", "to_unit": "tbsp"}),
            ):
                def paint_card(s, c=card):
                    rect = gs.draw_card(s, c, layout.m, panel.top, panel.w, panel.h)
                    paint_card.box = rect or panel
                add(kind, paint_card, None)
            row = {"kind": "update", "bot": "otto", "type": "update", "id": "a",
                   "line": "Shipped the nightly build", "detail": "Ready to try on the station",
                   "time": 1_000.0}
            add("update", lambda s, r=panel: gs._paint_update_card(s, layout, r, row, 1_100.0), panel)
            short = {"from": "Otto", "voice": True, "state": "speaking", "heard": "What's the score?",
                     "text": "Kansas City leads."}
            add("wake", lambda s, r=panel: gs.draw_zone_voice(s, layout, r, short, "speaking", now_s=1_000.0), panel)
            long = {"from": "Otto", "voice": True, "state": "speaking", "heard": HEARD,
                    "text": "Arkansas plays Saturday night at home."}
            add("question", lambda s, r=panel: gs.draw_zone_voice(s, layout, r, long, "speaking", now_s=1_000.0), panel)
            enroll = {"from": "Otto", "voice": True, "state": "enroll", "heard": "2/8|recording|",
                      "text": 'Say "hey grok" once more'}
            add("training", lambda s: gs.draw_enroll(s, layout, NOW, enroll, wx=_Wx()), None)
            home = {"kind": "home", "card": {"title": "Stocks", "items": [
                {"label": "Northwind", "value": "12.40", "trend": "up"},
                {"label": "Harbor", "value": "3.10", "trend": "down"},
            ]}}
            band = gs.updates_band_layout(layout, date_bottom=layout(200), weather_bottom=layout(200))
            if band:
                rect, scale = band["rects"][0], band["scale"]
                add("home", lambda s, r=rect, sc=scale: gs._paint_home_tile(s, layout, r, home, sc), rect)

            for name, paint, box, sports_row in cases:
                surf = pygame.Surface(size)
                surf.fill(gs.BG)
                marks = _watch()
                clipped = []
                real_ellipsize = gs.ellipsize

                def watching(face, line, width, _real=real_ellipsize, _clipped=clipped):
                    if _short_field(line) and face.size(line)[0] > width:
                        _clipped.append(line)
                    return _real(face, line, width)

                gs.ellipsize = watching
                try:
                    paint(surf)
                finally:
                    gs.ellipsize = real_ellipsize
                    gs.PAINT_MARKS = None
                used = getattr(paint, "box", None) if box is None else box
                if name == "training":
                    used = gs.LAST_ENROLL_RECT
                if name in ("weather", "time", "convert"):
                    used = getattr(paint, "box", None) or panel
                self.assertIsNotNone(used, (name, size))
                self.assertTrue(marks, (name, size))
                _assert_panel("%s %s" % (name, size), used, marks, surf, layout, sports_row, clipped)


def _painted_words(size, card):
    """(word, ink rect) for every _line that draw_sports actually blits."""
    layout = gs.Layout(*size)
    panel = gs.bottom_panel_rect(layout)
    surf = pygame.Surface(size)
    surf.fill(gs.BG)
    labels = {}
    painted = []
    real_line, real_show = gs._line, gs.show

    def watching_line(surf, role, px, text, color, width):
        img = real_line(surf, role, px, text, color, width)
        labels[id(img)] = str(text)
        return img

    def watching_show(surf, img, dest, area=None, special_flags=0):
        before = len(gs.PAINT_MARKS)
        result = real_show(surf, img, dest, area, special_flags)
        word = labels.get(id(img))
        if word and len(gs.PAINT_MARKS) > before:
            painted.append((word, gs.PAINT_MARKS[-1][0]))
        return result

    gs.PAINT_MARKS = []
    gs._line = watching_line
    gs.show = watching_show
    try:
        gs.draw_sports(surf, layout, card, panel)
    finally:
        gs._line = real_line
        gs.show = real_show
        gs.PAINT_MARKS = None
    return painted, panel


def _paint_sports(size, card):
    """Every blit from draw_sports: the word (or None), its ink, and the placed square."""
    layout = gs.Layout(*size)
    panel = gs.bottom_panel_rect(layout)
    surf = pygame.Surface(size)
    surf.fill(gs.BG)
    labels = []
    painted = []
    real_line, real_show = gs._line, gs.show

    def watching_line(surf, role, px, text, color, width):
        img = real_line(surf, role, px, text, color, width)
        labels.append((img, str(text)))
        return img

    def watching_show(surf, img, dest, area=None, special_flags=0):
        before = len(gs.PAINT_MARKS)
        result = real_show(surf, img, dest, area, special_flags)
        if len(gs.PAINT_MARKS) <= before:
            return result
        if isinstance(dest, pygame.Rect):
            dx, dy = dest.x, dest.y
        else:
            dx, dy = int(dest[0]), int(dest[1])
        word = next((text for img0, text in labels if img0 is img), None)
        placed = pygame.Rect(dx, dy, img.get_width(), img.get_height())
        painted.append((word, gs.PAINT_MARKS[-1][0], placed))
        return result

    gs.PAINT_MARKS = []
    gs._line = watching_line
    gs.show = watching_show
    try:
        gs.draw_sports(surf, layout, card, panel)
    finally:
        gs._line = real_line
        gs.show = real_show
        gs.PAINT_MARKS = None
    return painted, panel


def _png(w, h):
    """A solid red PNG pygame can load. image.save is not a readable PNG here."""
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _attach_logo(card):
    """A real logo file and no badge, so the plain case still paints a mark."""
    lid = "a" * 64
    root = tempfile.mkdtemp(prefix="gs1342-logo-")
    with open(os.path.join(root, lid), "wb") as fh:
        fh.write(_png(12, 12))
    gs._LOGO = gs.cards.LogoCache(root, [])
    for side in ("home", "away"):
        card[side]["logo_id"] = lid


class LogoSize(unittest.TestCase):
    """Team marks use SPORTS_LOGO_PX: 56 px at 1280x800, scaled at 2560x1706, clear of the score and the city."""

    def test_logos_match_the_named_size_and_clear_the_score_and_city(self):
        expect = {(1280, 800): 56, (2560, 1706): 112}
        for size, px in expect.items():
            self.assertEqual(gs.Layout(*size)(gs.SPORTS_LOGO_PX), px, size)
        for size in expect:
            px = expect[size]
            for status in ("live", "final"):
                for badge in (True, False):
                    with self.subTest(size=size, status=status, badge=badge):
                        card = _sports(status, True, badge)
                        old = gs._LOGO
                        try:
                            if not badge:
                                _attach_logo(card)
                            painted, panel = _paint_sports(size, card)
                        finally:
                            gs._LOGO = old
                        self._check(painted, panel, px)

    def _check(self, painted, panel, px):
        names = [item for item in painted if item[0] in ("Kansas City", "Denver")]
        status = [item for item in painted if item[0] in ("LIVE", "FINAL", "Q4", "OT", "2OT", "NFL")]
        logos = []
        for word, ink, placed in painted:
            if word is not None or placed.w != placed.h or placed.h < 8:
                continue
            if any(placed.centery < name[2].centery and abs(placed.centerx - name[2].centerx) < panel.w // 4
                   for name in names):
                logos.append((ink, placed))
        # The digits are font.render, not _line, so they have no word. The dash sits between them.
        name_bottom = max(item[2].bottom for item in names) if names else 0
        scores = [(ink, placed) for word, ink, placed in painted
                  if word is None and placed.top >= name_bottom and (ink, placed) not in [(a, b) for a, b in logos]]
        self.assertEqual(len(names), 2, [item[0] for item in painted])
        self.assertEqual(len(logos), 2, [(word, placed.size) for word, _ink, placed in painted])
        self.assertGreaterEqual(len(scores), 2, [(placed.size) for _ink, placed in scores])
        for ink, placed in logos:
            self.assertEqual((placed.w, placed.h), (px, px), placed)
            self.assertTrue(panel.contains(placed), (placed, panel))
            self.assertTrue(panel.contains(ink), (ink, panel))
            city = min(names, key=lambda item: abs(item[2].centerx - placed.centerx))
            self.assertLessEqual(abs(city[2].centerx - placed.centerx), 1, (placed, city[2]))
            self.assertLessEqual(placed.bottom, city[2].top, (placed, city[0], city[2]))
            self.assertFalse(placed.colliderect(city[1]) or ink.colliderect(city[1]), (placed, city[1]))
            self.assertFalse(placed.colliderect(city[2]), (placed, city[2]))
            for score_ink, score_placed in scores:
                self.assertFalse(placed.colliderect(score_ink) or ink.colliderect(score_ink),
                                 (placed, score_ink))
                self.assertFalse(placed.colliderect(score_placed), (placed, score_placed))
            for word, status_ink, status_placed in status:
                self.assertFalse(placed.colliderect(status_ink) or ink.colliderect(status_ink),
                                 (placed, word, status_ink))
                self.assertFalse(placed.colliderect(status_placed), (placed, word, status_placed))


class FinalLabel(unittest.TestCase):
    def test_a_regulation_final_paints_final_once(self):
        for size in ((1280, 800), (2560, 1706)):
            for badge in (True, False):
                painted, panel = _painted_words(size, _sports("final", True, badge))
                finals = [word for word, _rect in painted if word.casefold() == "final"]
                right = [word for word, rect in painted if rect.centerx > panel.centerx]
                with self.subTest(size=size, badge=badge):
                    self.assertEqual(finals, ["FINAL"], (finals, right))
                    self.assertFalse(any(word.casefold() in ("final", "f", "ot") for word in right), right)

    def test_an_overtime_final_paints_ot_on_the_right(self):
        card = _sports("final", False, False)
        card["period"] = "Final/OT"
        painted, panel = _painted_words((1280, 800), card)
        finals = [word for word, _rect in painted if word.casefold() == "final"]
        right = [word for word, rect in painted if rect.centerx > panel.centerx]
        self.assertEqual(finals, ["FINAL"])
        self.assertIn("OT", right)


if __name__ == "__main__":
    unittest.main()
