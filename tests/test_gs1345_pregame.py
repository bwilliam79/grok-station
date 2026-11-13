"""0.13.45: a refused sports card names the field, and a pregame card is accepted.

The helper, the relay, and the station share cards.validate_card. A scheduled
card may omit the period and the scores. Live and final may not. The reason
is the path and the problem, never the value.
"""
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import pygame  # noqa: E402
import cards  # noqa: E402
import grok_station as gs  # noqa: E402

pygame.display.init()
pygame.font.init()


def _team(name, abbr):
    return {"name": name, "abbr": abbr}


def _sports(**kw):
    card = {
        "type": "sports", "v": 1, "league": "NCAAF",
        "home": _team("Arkansas", "ARK"), "away": _team("Texas", "TEX"),
        "home_score": 0, "away_score": 0, "status": "live", "period": "Q1",
        "schedule": [],
    }
    card.update(kw)
    return card


def _load_helper():
    import importlib.machinery
    import importlib.util
    saved = list(sys.path)
    loader = importlib.machinery.SourceFileLoader(
        "gsr_1345", os.path.join(ROOT, "scripts", "gs-seal-reply"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("gsr_1345", loader))
    try:
        loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    return mod


class PregameSchema(unittest.TestCase):
    def test_scheduled_accepts_a_blank_period_and_null_scores(self):
        missing = _sports(status="scheduled")
        del missing["period"]
        missing["home_score"] = None
        missing["away_score"] = None
        cleaned = cards.validate_card(missing)
        self.assertEqual(cleaned["status"], "scheduled")
        self.assertEqual(cleaned["period"], "")
        self.assertIsNone(cleaned["home_score"])
        self.assertIsNone(cleaned["away_score"])
        self.assertNotIn("starts_at", cleaned)

    def test_starts_at_is_one_offset_instant_and_only_on_a_scheduled_game(self):
        good = cards.validate_card(_sports(status="scheduled", starts_at="2026-11-15T19:15:00-06:00"))
        self.assertEqual(good["starts_at"], "2026-11-15T19:15:00-06:00")
        zulu = cards.validate_card(_sports(status="scheduled", starts_at="2026-11-15T19:15:00Z"))
        self.assertEqual(zulu["starts_at"], "2026-11-15T19:15:00Z")
        for bad in ("7:15 PM", "2026-11-15T19:15:00", "2026-11-15T19:15:00-0600",
                    "2026-11-15T19:15:00.0-06:00", "2026-02-31T19:15:00-06:00", "", None, 3):
            card = _sports(status="scheduled", starts_at=bad)
            with self.assertRaises(cards.CardError) as caught:
                cards.validate_card(card)
            self.assertEqual(caught.exception.detail, "sports.starts_at invalid", bad)
        live = _sports(starts_at="2026-11-15T19:15:00-06:00")
        with self.assertRaises(cards.CardError) as caught:
            cards.validate_card(live)
        self.assertEqual(caught.exception.detail, "sports.field unexpected")
        self.assertNotIn("starts_at", caught.exception.detail)
        blank = cards.validate_card(_sports(status="scheduled", period="", home_score=None, away_score=None))
        self.assertEqual(blank["period"], "")
        labeled = _sports(status="scheduled", period="Sat 11:00 AM")
        del labeled["home_score"]
        del labeled["away_score"]
        kept = cards.validate_card(labeled)
        self.assertEqual(kept["period"], "Sat 11:00 AM")
        self.assertIsNone(kept["home_score"])

    def test_live_and_final_name_the_field_and_hide_the_value(self):
        secret = "Sat 11:00 AM extra words"
        cases = (
            (_sports(period=""), "sports.period empty"),
            (_sports(period=None), "sports.period empty"),
            (_sports(away_score=None), "sports.away_score not int"),
            (_sports(status="final", period=""), "sports.period empty"),
        )
        for card, detail in cases:
            with self.assertRaises(cards.CardError) as caught:
                cards.validate_card(card)
            self.assertEqual(getattr(caught.exception, "detail", None), detail)
            self.assertNotIn("Sat", caught.exception.detail)
        extra = _sports()
        extra["extra_key"] = secret
        with self.assertRaises(cards.CardError) as caught:
            cards.validate_card(extra)
        self.assertEqual(caught.exception.detail, "sports.field unexpected")
        self.assertNotIn("extra_key", caught.exception.detail)
        self.assertNotIn(secret, caught.exception.detail)
        self.assertNotIn("http", cards.show_refused_line("rhea", "schema", "sports.period empty"))

    def test_show_refused_line_drops_a_bad_detail(self):
        line = cards.show_refused_line("rhea", "schema", "sports.period " + "Sat 11:00 AM")
        self.assertEqual(line, "show refused bot=rhea check=schema -")
        self.assertNotIn("Sat", line)


class HelperShow(unittest.TestCase):
    def test_show_refuses_before_any_post(self):
        home = _load_helper()
        posted = []
        home._run_post = lambda *a, **k: posted.append(1)
        body = {"v": 1, "op": "put", "id": "score", "priority": 10, "ttl_s": 600,
                "card": _sports(period="")}
        home.read_stdin = lambda: json.dumps(body)
        err = io.StringIO()
        with mock.patch.object(home.sys, "stderr", err):
            rc = home.main(["show", "--persona", "rhea"])
        self.assertEqual(rc, 1)
        self.assertEqual(posted, [])
        self.assertEqual(err.getvalue(), "refused: schema sports.period empty\n")

    def test_a_400_detail_is_printed_and_a_value_is_not(self):
        home = _load_helper()
        raw = b'{"error":"schema","reason":"schema","detail":"sports.away_score not int"}'
        self.assertEqual(home.detail_from_body(raw), "sports.away_score not int")
        self.assertEqual(home.show_failure_line("schema", "sports.away_score not int"),
                         "refused: schema sports.away_score not int")
        leaked = b'{"detail":"sports.period Sat 11:00 AM"}'
        self.assertIsNone(home.detail_from_body(leaked))


class PregameTile(unittest.TestCase):
    def _draw(self, card, size, now):
        layout = gs.Layout(*size)
        panel = gs.bottom_panel_rect(layout)
        surf = pygame.Surface(size)
        surf.fill(gs.BG)
        drawn = []
        real_font = gs.font

        class _Face:
            def __init__(self, inner):
                self._inner = inner

            def render(self, text, antialias, color, *args, **kwargs):
                drawn.append((str(text), tuple(color)))
                return self._inner.render(text, antialias, color, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        gs.PAINT_MARKS = []
        gs.font = lambda kind, px, _real=real_font: _Face(_real(kind, px))
        try:
            gs.draw_sports(surf, layout, card, panel, now=now)
        finally:
            gs.font = real_font
            marks = list(gs.PAINT_MARKS or [])
            gs.PAINT_MARKS = None
        return drawn, marks, panel

    def test_scheduled_tile_shows_today_another_day_or_tbd_and_no_score(self):
        zone = datetime.timezone(datetime.timedelta(hours=-6))
        now = datetime.datetime(2026, 11, 15, 12, 0, tzinfo=zone)
        today = cards.validate_card(_sports(
            status="scheduled", period="Sat 11:00 AM", home_score=None, away_score=None,
            starts_at="2026-11-15T19:15:00-06:00"))
        later = cards.validate_card(_sports(
            status="scheduled", home_score=None, away_score=None,
            starts_at="2026-11-16T19:15:00-06:00"))
        unknown = cards.validate_card(_sports(
            status="scheduled", period="Sat 11:00 AM", home_score=None, away_score=None))
        for size in ((1280, 720), (2560, 1440)):
            for card, word, ink in (
                (today, "7:15 PM", gs.TEXT),
                (later, "Mon 7:15 PM", gs.TEXT),
                (unknown, "Time TBD", gs.MUTED),
            ):
                drawn, marks, panel = self._draw(card, size, now)
                words = [text for text, _color in drawn]
                self.assertIn("SCHEDULED", words, (size, word))
                self.assertIn(word, words, size)
                self.assertNotIn("Sat 11:00 AM", words, size)
                self.assertNotIn("\u2013", words, size)
                self.assertFalse(any(text.isdigit() for text in words), (size, words))
                self.assertEqual(dict(drawn)[word], tuple(ink), (size, word))
                self.assertTrue(marks, size)
                for rect, _paint in marks:
                    self.assertTrue(panel.contains(rect), (size, rect, panel))


class FeedKind(unittest.TestCase):
    def test_a_kind_that_cleans_to_empty_drops_only_that_row(self):
        directory = tempfile.mkdtemp(prefix="gs1345-feed-")
        path = os.path.join(directory, "update-feed.json")
        doc = {"items": [
            {"bot": "otto", "id": "a", "type": "update", "line": "Kept aside",
             "time": 1000, "seq": 1, "kind": "<" * 19, "detail": "Ready"},
            {"bot": "otto", "id": "b", "type": "update", "line": "Still here",
             "time": 1001, "seq": 2, "kind": "Mail", "detail": "Ready"},
        ], "rules": {}, "seq": 2}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        feed = cards.UpdateFeed(path)
        ids = sorted(uid for (_bot, uid) in feed.items)
        self.assertEqual(ids, ["b"])
        self.assertEqual(feed.items[("otto", "b")]["line"], "Still here")


if __name__ == "__main__":
    unittest.main()
