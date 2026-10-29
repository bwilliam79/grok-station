"""Mic icon false 'problem' (owner report, Oct 29): a working EMEET whose noise suppression sends digital
silence between words trips the silence watchdog; the listener then waits out the reopen backoff with
capture=None, and the display's 10 s grace is shorter than that wait, so the icon shows mic-problem
although the next round reads good audio again."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wake"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import grok_wake as gw
import grok_station as gs
from test_mic_reset import FakeRec

CARDS = " 2 [Plus           ]: USB-Audio - EMEET OfficeCore M0 Plus"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class MicIconRecovery(unittest.TestCase):
    def test_icon_stays_ok_while_listener_reopens_after_quiet_room(self):
        clock = Clock()
        seen = []

        def payload():
            L = gw.LIVE
            return {"level_at": L["at"], "audio_at": L.get("audio_at"), "capture": L["capture"],
                    "reopen_by": L.get("reopen_by")}

        def sleep(s):  # the listener waiting to reopen; the display keeps polling every 2 s
            end = clock.t + s
            while clock.t < end:
                clock.t = min(end, clock.t + 2.0)
                seen.append((round(clock.t, 1), gs.check_mic(clock.t, payload, CARDS)))

        procs = [FakeRec("zeros", exit_after=200), FakeRec("zeros", exit_after=200), FakeRec("noise", exit_after=20)]

        def handle(buf, guard):
            clock.t += 0.08

        def on_open(cap, proc):
            gw.LIVE["capture"] = cap

        with mock.patch("time.time", clock):
            gw.LIVE["audio_at"] = 0.0
            reasons = gw.run_capture_rounds(lambda: "plughw:CARD=Plus,DEV=0", lambda cap: procs.pop(0), handle,
                                            lambda: False, rounds=3, stall_s=60, silence_s=10.0, chunk_s=0.08,
                                            sleep=sleep, now=clock, log=lambda m: None, n_bytes=64, on_open=on_open)
        self.assertEqual(reasons[:2], ["silence", "silence"])
        bad = [(t, r["detail"]) for t, r in seen if not r["ok"]]
        self.assertEqual(bad, [], "icon went to mic-problem while the listener was only waiting to reopen")

    def test_real_problems_still_show(self):
        ok = {"capture": "plughw:CARD=Plus,DEV=0", "audio_at": 1000.0, "level_at": 1000.0}
        self.assertEqual(gs.check_mic(1001.0, lambda: ok, "")["detail"], "speakerphone missing")
        self.assertEqual(gs.check_mic(1001.0, lambda: None, CARDS)["detail"], "wake listener down")
        waiting = {"capture": None, "audio_at": 1000.0, "reopen_by": 1030.0}
        self.assertTrue(gs.check_mic(1035.0, lambda: waiting, CARDS)["ok"])
        self.assertEqual(gs.check_mic(1041.0, lambda: waiting, CARDS)["detail"], "no capture device")
        stalled = {"capture": "plughw:CARD=Plus,DEV=0", "audio_at": 1000.0, "reopen_by": None}
        self.assertEqual(gs.check_mic(1031.0, lambda: stalled, CARDS)["detail"], "capture stalled")

    def test_older_listener_and_bad_deadline(self):
        old = {"capture": None, "audio_at": 1000.0}
        self.assertEqual(gs.check_mic(1015.0, lambda: old, CARDS)["detail"], "no capture device")
        for bad in ("soon", 1e12, [1]):
            p = {"capture": None, "audio_at": 1000.0, "reopen_by": bad}
            self.assertEqual(gs.check_mic(1015.0, lambda: p, CARDS)["detail"], "no capture device")

    def test_status_payload_has_reopen_by(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "wake", "grok_wake.py")).read()
        self.assertIn('"reopen_by": LIVE.get("reopen_by")', src)
        self.assertIsNone(gw.LIVE["reopen_by"])


if __name__ == "__main__":
    unittest.main()
