#!/usr/bin/env python3
"""0.13.9 Nix release items for the wake service.

(b) A failed silent-stop cleanup is logged, not swallowed.
(c) Stuck-mic resets are capped at 10 an hour, with one log line when the cap is hit.
Both fail on 77ba820.
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_mic_reset import gw  # noqa: E402


class FakeRecovery:
    def __init__(self):
        self.asked = []

    def request(self, reason):
        self.asked.append(reason)
        return "ok"


class StuckCap(unittest.TestCase):
    def setUp(self):
        self.old = gw.RECOVERY
        gw.RECOVERY = FakeRecovery()
        gw.STUCK_REOPENS.clear()
        gw.STUCK_RESETS.clear()
        gw.STUCK_CAP_LOGGED[0] = False

    def tearDown(self):
        gw.RECOVERY = self.old
        gw.STUCK_REOPENS.clear()
        gw.STUCK_RESETS.clear()
        gw.STUCK_CAP_LOGGED[0] = False

    def _trip(self, t):
        out = None
        for k in range(gw.STUCK_COUNT):
            out = gw.note_stuck_reopen("silence", False, t + k)
        return out

    def test_cap_ten_an_hour_and_log_once(self):
        self.assertEqual((gw.STUCK_CAP, gw.STUCK_CAP_WINDOW_S), (10, 3600.0))
        buf = io.StringIO()
        with redirect_stdout(buf):
            results = [self._trip(1000.0 + 60 * n) for n in range(14)]
        self.assertEqual(results[:10], ["ok"] * 10)
        self.assertEqual(results[10:], ["capped"] * 4)
        self.assertEqual(gw.RECOVERY.asked, ["stuck"] * 10)
        self.assertEqual(buf.getvalue().count("mic reset capped"), 1)
        # Once the first reset is over an hour old, one more is allowed.
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self._trip(1000.0 + 3600 + 5), "ok")
        self.assertEqual(len(gw.RECOVERY.asked), 11)


class SilentStopCleanup(unittest.TestCase):
    def test_failed_rmtree_is_logged(self):
        d = tempfile.mkdtemp()
        buf = io.StringIO()
        with mock.patch.object(gw.shutil, "rmtree", side_effect=PermissionError("nope")), redirect_stdout(buf):
            gw._rmtree_logged(d, "silent-stop")
        line = buf.getvalue()
        self.assertIn("enroll cleanup failed (silent-stop)", line)
        self.assertIn("PermissionError", line)
        os.rmdir(d)
        buf = io.StringIO()
        with redirect_stdout(buf):
            gw._rmtree_logged(d, "silent-stop")  # already gone: nothing to log
        self.assertEqual(buf.getvalue(), "")

    def test_silent_stop_path_uses_it(self):
        src = open(gw.__file__, encoding="utf-8").read()
        block = src[src.index('msg, _station_msg = ENROLL_STATE.pop("silent_stop")'):]
        block = block[:block.index("return")]
        self.assertIn('_rmtree_logged(d, "silent-stop")', block)
        self.assertNotIn("ignore_errors", block)


if __name__ == "__main__":
    unittest.main()
