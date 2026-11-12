"""0.13.42 review: a null update is scoped by kind, and filler is a null only for type update.

A filler line that also carries detail or expires_at is a real update. The seal
helper and the station validator agree on that table.
"""
import datetime as dt
import importlib.machinery
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import cards  # noqa: E402

T0 = 1_800_000_000.0
WHEN = "2026-11-12T21:00:00Z"
FRESH = dt.datetime.fromtimestamp(T0, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def station_doc(update):
    return {"v": 1, "op": "update", "id": "u1", "update": update,
            "kind": "show", "nonce": "n" * 22, "exp_ms": 1}


def helper_doc(update):
    return {"v": 1, "op": "update", "id": "u1", "update": update}


def _seal():
    saved = list(sys.path)
    loader = importlib.machinery.SourceFileLoader(
        "gsr_review4", os.path.join(ROOT, "scripts", "gs-seal-reply"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("gsr_review4", loader))
    try:
        loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    return mod


class KindScopedNull(unittest.TestCase):
    def test_a_null_with_kind_clears_only_that_kind(self):
        feed = cards.UpdateFeed()
        feed.put("otto", "mail", {"type": "update", "kind": "Email", "line": "Invoice paid", "time": T0})
        feed.put("otto", "cal", {"type": "update", "kind": "Calendar", "line": "Dentist Friday", "time": T0})
        feed.put("otto", "ask", {"type": "needs_you", "line": "Sign the form", "time": T0})
        got = cards.validate_update(station_doc(
            {"type": "update", "kind": "Calendar", "line": "Nothing open.", "time": WHEN}))
        self.assertTrue(got["update"]["null"])
        self.assertEqual(got["update"].get("kind"), "Calendar")
        feed.put("otto", "none", got["update"])
        rows = {r["id"]: r for r in feed.ordered(T0 + 1)[0] + feed.ordered(T0 + 1)[1]}
        self.assertIn("mail", rows)
        self.assertEqual(rows["mail"]["line"], "Invoice paid")
        self.assertNotIn("cal", rows)
        self.assertIn("ask", rows)
        self.assertEqual(rows["ask"]["type"], "needs_you")

    def test_a_null_without_kind_clears_every_type_update(self):
        feed = cards.UpdateFeed()
        feed.put("otto", "mail", {"type": "update", "kind": "Email", "line": "Invoice paid", "time": T0})
        feed.put("otto", "cal", {"type": "update", "kind": "Calendar", "line": "Dentist Friday", "time": T0})
        feed.put("otto", "ask", {"type": "needs_you", "line": "Sign the form", "time": T0})
        feed.put("otto", "head", {"type": "heads_up", "line": "Storm tonight", "time": T0})
        got = cards.validate_update(station_doc(
            {"type": "update", "line": None, "time": WHEN}))
        self.assertTrue(got["update"]["null"])
        self.assertNotIn("kind", got["update"])
        feed.put("otto", "none", got["update"])
        rows = list(feed.ordered(T0 + 1)[0]) + list(feed.ordered(T0 + 1)[1])
        self.assertEqual(sorted(r["id"] for r in rows), ["ask", "head"])


class FillerStaysOnOtherTypes(unittest.TestCase):
    def test_needs_you_and_heads_up_keep_a_filler_line(self):
        for type_ in ("needs_you", "heads_up", "working_on", "done"):
            got = cards.validate_update(station_doc(
                {"type": type_, "line": "Nothing open.", "time": WHEN}))
            self.assertFalse(got["update"].get("null"), type_)
            self.assertEqual(got["update"]["type"], type_)
            self.assertIn("Nothing", got["update"]["line"])
        feed = cards.UpdateFeed()
        feed.put("otto", "mail", {"type": "update", "kind": "Email", "line": "Invoice paid", "time": T0})
        posted = cards.validate_update(station_doc(
            {"type": "heads_up", "line": "Nothing open.", "time": FRESH}))
        feed.put("otto", "head", posted["update"])
        rows = {r["id"]: r for r in feed.ordered(T0 + 1)[0] + feed.ordered(T0 + 1)[1]}
        self.assertEqual(rows["mail"]["line"], "Invoice paid")
        self.assertEqual(rows["head"]["type"], "heads_up")


class HelperAgrees(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = _seal()

    def _pair(self, update):
        try:
            parsed = cards.validate_update(station_doc(update))["update"]
            station = "ok"
        except cards.CardError:
            parsed = None
            station = "no"
        helper = self.home.update_show_ok(helper_doc(update))
        return station, parsed, helper

    def test_filler_with_detail_is_a_real_update_on_both_sides(self):
        cases = (
            ({"type": "update", "line": None, "time": WHEN}, "null", None),
            ({"type": "update", "kind": "Calendar", "line": None, "time": WHEN}, "null", "Calendar"),
            ({"type": "update", "line": None, "detail": "Friday", "time": WHEN}, "no", None),
            ({"type": "update", "line": None, "expires_at": WHEN, "time": WHEN}, "no", None),
            ({"type": "update", "line": "Nothing open.", "time": WHEN}, "null", None),
            ({"type": "update", "kind": "Calendar", "line": "Nothing open.", "time": WHEN}, "null", "Calendar"),
            ({"type": "update", "kind": "Calendar", "line": "Nothing open.", "detail": "Friday", "time": WHEN},
             "real", "Calendar"),
            ({"type": "update", "line": "Nothing open.", "detail": "Friday", "time": WHEN}, "no", None),
            ({"type": "update", "line": "Nothing open.", "expires_at": WHEN, "kind": "Calendar", "time": WHEN},
             "real", "Calendar"),
            ({"type": "heads_up", "line": "Nothing open.", "time": WHEN}, "keep", None),
            ({"type": "needs_you", "line": "All clear.", "time": WHEN}, "keep", None),
            ({"type": "needs_you", "line": None, "time": WHEN}, "no", None),
            ({"type": "update", "kind": "Calendar", "line": "Nothing open on the calendar until Friday",
              "time": WHEN}, "words", "Calendar"),
        )
        for update, expect, kind in cases:
            station, parsed, helper = self._pair(update)
            with self.subTest(update=update):
                self.assertEqual(helper, station == "ok", (station, helper))
                if expect == "no":
                    self.assertEqual(station, "no")
                    continue
                self.assertEqual(station, "ok")
                if expect == "null":
                    self.assertTrue(parsed["null"])
                    self.assertEqual(parsed.get("kind"), kind)
                elif expect == "real":
                    self.assertFalse(parsed.get("null"))
                    self.assertEqual(parsed.get("kind"), kind)
                    self.assertIn("detail" if "detail" in update else "expires_at", parsed)
                elif expect == "words":
                    self.assertFalse(parsed.get("null"))
                    self.assertIn("Friday", parsed["line"])
                else:
                    self.assertFalse(parsed.get("null"))
                    self.assertEqual(parsed["type"], update["type"])


if __name__ == "__main__":
    unittest.main()
