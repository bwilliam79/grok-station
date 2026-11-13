"""0.13.45: a refused sports card names the field, and a pregame card is accepted.

The helper, the relay, and the station share cards.validate_card. A scheduled
card may omit the period and the scores. Live and final may not. The reason
is the path and the problem, never the value.
"""
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
        self.assertEqual(caught.exception.detail, "sports.extra_key unexpected")
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
    def test_scheduled_tile_shows_the_start_and_hides_none(self):
        card = cards.validate_card(_sports(status="scheduled", period="Sat 11:00 AM",
                                           home_score=None, away_score=None))
        for size in ((1280, 720), (2560, 1440)):
            layout = gs.Layout(*size)
            panel = gs.bottom_panel_rect(layout)
            surf = pygame.Surface(size)
            surf.fill(gs.BG)
            drawn = []
            real_font = gs.font

            class _Face:
                def __init__(self, inner):
                    self._inner = inner

                def render(self, text, *args, **kwargs):
                    drawn.append(str(text))
                    return self._inner.render(text, *args, **kwargs)

                def __getattr__(self, name):
                    return getattr(self._inner, name)

            gs.PAINT_MARKS = []
            gs.font = lambda kind, px, _real=real_font: _Face(_real(kind, px))
            try:
                gs.draw_sports(surf, layout, card, panel)
            finally:
                gs.font = real_font
                marks = list(gs.PAINT_MARKS or [])
                gs.PAINT_MARKS = None
            self.assertIn("Sat 11:00 AM", drawn)
            self.assertIn("SCHEDULED", drawn)
            self.assertNotIn("None", drawn)
            self.assertIn("\u2013", drawn)
            self.assertTrue(marks, size)
            for rect, _ink in marks:
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
