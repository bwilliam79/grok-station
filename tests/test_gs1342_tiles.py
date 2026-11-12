"""0.13.42: every content tile stays inside its panel at each supported size.

Painted ink is the glyph bounding rect at the blit, clipped to what the surface
actually shows. It must sit inside the panel. Two text or score inks must not overlap.
A sports score must also clear the next-game divider by the column's bottom inset.
"""
import os
import re
import sys
import time
import unittest

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
                {"time": "6:30 AM", "title": "Harbor practice"},
                {"time": "9:00 AM", "title": "Market run"},
                {"time": "12:15 PM", "title": "Lunch with the team"},
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


if __name__ == "__main__":
    unittest.main()
