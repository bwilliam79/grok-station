#!/usr/bin/env python3
"""0.13.9 bug: a mic that only gives silence (or stalls) after an update just kept reopening. Nothing reset it.

Three silence/stall reopens within about 2 minutes, outside a session, now ask MicRecovery for a USB reset
with the reason "stuck" (gated by the drought flag, the 45 s minimum gap and the session check).
Fails on 0.13.8 / 2e654a1 (no request).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_mic_reset import FakeRec, _within, gw  # noqa: E402


class FakeRecovery:
    def __init__(self):
        self.asked = []

    def request(self, reason):
        self.asked.append(reason)
        return "ok"


class StuckMic(unittest.TestCase):
    def setUp(self):
        self.old = (gw.RECOVERY, gw.ENROLL_STATE.get("busy"))
        gw.RECOVERY = FakeRecovery()
        gw.STUCK_REOPENS.clear()

    def tearDown(self):
        gw.RECOVERY, busy = self.old
        gw.ENROLL_STATE["busy"] = busy
        gw.STUCK_REOPENS.clear()

    def _rounds(self, mode, rounds, in_session):
        procs = []

        def spawn(cap):
            p = FakeRec(mode)
            procs.append(p)
            return p
        try:
            return _within(lambda: gw.run_capture_rounds(
                lambda: "plughw:CARD=M0,DEV=0", spawn, lambda buf, guard: None, in_session, rounds=rounds,
                stall_s=0.15, silence_s=0.16, chunk_s=0.08, sleep=lambda s: None, log=lambda m: None,
                gap=30.0, n_bytes=8), timeout=30)
        finally:
            for p in procs:
                p.kill()

    def test_silence_only_capture_requests_exactly_one_reset(self):
        reasons = self._rounds("zeros", 5, lambda: False)
        self.assertEqual(reasons, ["silence"] * 5)
        self.assertEqual(gw.RECOVERY.asked, ["stuck"])

    def test_no_reset_during_an_enroll_session(self):
        gw.ENROLL_STATE["busy"] = True
        reasons = self._rounds("stall", 4, gw.mic_busy)
        self.assertEqual(reasons, ["stall"] * 4)
        self.assertEqual(gw.RECOVERY.asked, [])


class StuckGates(unittest.TestCase):
    def test_stuck_uses_the_drought_flag(self):
        runs = []
        rec = gw.MicRecovery(lambda card: runs.append(card), flags=lambda: {"button": True, "night": True, "drought": False},
                             card=lambda: "Plus", busy=lambda: False, kick=lambda: None, log=lambda m: None)
        self.assertEqual(rec.request("stuck"), "off")
        rec.flags = lambda: {"button": False, "night": False, "drought": True}
        self.assertEqual(rec.request("stuck"), "ok")
        self.assertEqual(runs, ["Plus"])
        self.assertEqual(rec.request("stuck"), "deferred")  # inside the 45 s minimum gap
        self.assertEqual(runs, ["Plus"])
        rec.busy = lambda: True
        rec._last_reset = None
        self.assertEqual(rec.request("stuck"), "deferred")  # a session waits
        self.assertEqual(runs, ["Plus"])


if __name__ == "__main__":
    unittest.main()
