"""0.13.42: a null update drops that bot from the rotation and survives a restart.

The wire form is an update op whose line is null. A short list of empty phrases
is the same clear. gs-seal-reply accepts that JSON and still refuses a bad body.
"""
import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import cards  # noqa: E402

T0 = 1_800_000_000.0
NULL = {
    "v": 1, "op": "update", "id": "none",
    "update": {"type": "update", "line": None, "time": "2026-11-12T21:00:00Z"},
}


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()


def upd(uid="u1", type_="update", line="Shipped the build", t=T0, **extra):
    body = {"type": type_, "line": line, "time": iso(t)}
    if type_ == "update" and "kind" not in extra and line is not None:
        body["kind"] = "Mail"
    body.update(extra)
    doc = {"v": 1, "op": "update", "id": uid, "update": body, "kind": "show",
           "nonce": "n" * 22, "exp_ms": 1}
    return doc


def _seal():
    # The helper replaces sys.path while it loads. Put the test path back so later tests can import.
    saved = list(sys.path)
    loader = importlib.machinery.SourceFileLoader("gsr_null_1342", os.path.join(ROOT, "scripts", "gs-seal-reply"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("gsr_null_1342", loader))
    try:
        loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    return mod


class _Reached(Exception):
    pass


class NullUpdate(unittest.TestCase):
    def test_null_line_clears_and_survives_restart(self):
        got = cards.validate_update(upd(line=None))
        self.assertIs(got["update"]["line"], None)
        self.assertTrue(got["update"]["null"])
        self.assertNotIn("kind", got["update"])
        directory = tempfile.mkdtemp(prefix="gs1342-null-")
        path = os.path.join(directory, "update-feed.json")
        feed = cards.UpdateFeed(path)
        feed.put("otto", "a", {"type": "update", "kind": "Mail", "line": "Shipped the build", "time": T0})
        feed.put("ada", "b", {"type": "update", "kind": "Mail", "line": "Ada shipped", "time": T0})
        self.assertEqual([r["bot"] for r in feed.ordered(T0)[1]], ["ada", "otto"])
        feed.put("otto", "none", got["update"])
        needs, rest = feed.ordered(T0 + 1)
        self.assertEqual(needs, [])
        self.assertEqual([r["bot"] for r in rest], ["ada"])
        self.assertNotIn("otto", {r["bot"] for r in rest})
        again = cards.UpdateFeed(path)
        self.assertEqual([r["bot"] for r in again.ordered(T0 + 2)[1]], ["ada"])
        again.put("otto", "c", {"type": "update", "kind": "Mail", "line": "A real update", "time": T0 + 3})
        self.assertEqual([r["bot"] for r in again.ordered(T0 + 4)[1]], ["otto", "ada"])
        window = cards.update_slots(*again.ordered(T0 + 4), 0)
        self.assertIn("otto", {r["bot"] for r in window})

    def test_filler_lines_are_null_and_other_words_are_kept(self):
        for line in ("Nothing open", "nothing open.", "NOTHING NEW", "no updates!",
                     "Nothing to report...", "All clear"):
            got = cards.validate_update(upd(line=line))
            self.assertTrue(got["update"]["null"], line)
            self.assertIsNone(got["update"]["line"], line)
        kept = cards.validate_update(upd(line="Nothing open on the calendar until Friday"))
        self.assertFalse(kept["update"].get("null"))
        self.assertIn("Friday", kept["update"]["line"])
        for line in ("No updates today", "all clear, the build shipped", "nothing opened"):
            got = cards.validate_update(upd(line=line))
            self.assertFalse(got["update"].get("null"), line)
        feed = cards.UpdateFeed()
        feed.put("otto", "a", {"type": "update", "kind": "Mail", "line": "Shipped", "time": T0})
        feed.put("otto", "b", {"type": "heads_up", "line": "Nothing open.", "time": T0})
        self.assertEqual(feed.ordered(T0)[1], [])

    def test_malformed_null_is_refused_and_the_seal_tool_agrees(self):
        for bad in (
            upd(type_="needs_you", line=None),
            upd(line=5),
            upd(line=None, detail="more"),
            {"v": 1, "op": "update", "id": "u1", "update": {"type": "update", "line": None},
             "kind": "show", "nonce": "n" * 22, "exp_ms": 1},
            dict(upd(line=None), update=dict(upd(line=None)["update"], extra=1)),
        ):
            with self.assertRaises(cards.CardError):
                cards.validate_update(bad)
        home = _seal()
        self.assertTrue(home.update_show_ok(NULL))
        self.assertFalse(home.update_show_ok(
            {"v": 1, "op": "update", "id": "u1", "update": {"type": "needs_you", "line": None,
                                                             "time": "2026-11-12T21:00:00Z"}}))
        self.assertFalse(home.update_show_ok(
            {"v": 1, "op": "update", "id": "u1", "update": {"type": "update", "line": 5,
                                                             "time": "2026-11-12T21:00:00Z"}}))
        self.assertFalse(home.update_show_ok(
            {"v": 1, "op": "update", "id": "u1", "update": {"type": "update", "line": None}}))
        self.assertTrue(home.update_show_ok(
            {"v": 1, "op": "update", "id": "build",
             "update": {"type": "working_on", "line": "Building", "time": "2026-11-01T13:00:00-06:00"}}))

        def show(doc):
            with mock.patch.object(home, "read_stdin", lambda: json.dumps(doc)), \
                 mock.patch.object(home, "_check_tree", mock.Mock(side_effect=_Reached)):
                try:
                    home.cmd_show("max")
                except _Reached:
                    return "accepted"
                except home.Refused:
                    return "refused"
            return "?"

        self.assertEqual(show(NULL), "accepted")
        self.assertEqual(show({"v": 1, "op": "update", "id": "u1", "nonce": "n",
                               "update": {"type": "update", "line": None, "time": "2026-11-12T21:00:00Z"}}),
                         "refused")
        self.assertEqual(show({"v": 1, "op": "update", "id": "u1",
                               "update": {"type": "needs_you", "line": None, "time": "2026-11-12T21:00:00Z"}}),
                         "refused")
        self.assertEqual(show({"v": 1, "op": "speak", "id": "x"}), "refused")


if __name__ == "__main__":
    unittest.main()
