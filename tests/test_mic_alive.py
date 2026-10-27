"""0.12.6: mic icon follows capture-alive (audio_at), and processing pauses are not a device stall."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wake"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import grok_wake as gw
import grok_station as gs
from test_mic_reset import FakeRec, _within

CARDS = " 2 [Plus           ]: USB-Audio - EMEET OfficeCore M0 Plus"


class MicIcon(unittest.TestCase):
    def test_long_session_stays_ok(self):
        now = 1000.0  # meter (level_at) frozen 60 s by an open session, frames still flowing
        st = {"level_at": now - 60, "audio_at": now - 0.1, "capture": "plughw:CARD=Plus", "session": True}
        self.assertTrue(gs.check_mic(now, lambda: st, CARDS)["ok"])

    def test_enrollment_stays_ok(self):
        now = 1000.0
        st = {"level_at": now - 45, "audio_at": now - 0.2, "capture": "plughw:CARD=Plus"}
        self.assertTrue(gs.check_mic(now, lambda: st, CARDS)["ok"])

    def test_brief_reopen_stays_ok(self):
        now = 1000.0
        self.assertTrue(gs.check_mic(now, lambda: {"audio_at": now - 3, "level_at": now - 3, "capture": None}, CARDS)["ok"])

    def test_dead_capture_is_a_problem(self):
        now = 1000.0
        self.assertFalse(gs.check_mic(now, lambda: {"audio_at": now - 15, "capture": None}, CARDS)["ok"])
        self.assertEqual(gs.check_mic(now, lambda: {"audio_at": now - 31, "level_at": now, "capture": "x"}, CARDS)["detail"],
                         "capture stalled")

    def test_older_listener_falls_back_to_level_at(self):
        now = 1000.0
        self.assertTrue(gs.check_mic(now, lambda: {"level_at": now - 5, "capture": "x"}, CARDS)["ok"])
        self.assertFalse(gs.check_mic(now, lambda: {"level_at": now - 40, "capture": "x"}, CARDS)["ok"])


class Watchdog(unittest.TestCase):
    def test_reads_update_audio_at(self):
        gw.LIVE["audio_at"] = 0.0
        g = gw.CaptureGuard(FakeRec("noise"), stall_s=0.2)
        g.read(8)
        self.assertGreater(gw.LIVE["audio_at"], time.time() - 5)

    def test_training_pause_is_not_a_stall(self):
        proc = FakeRec("noise")
        g = gw.CaptureGuard(proc, stall_s=0.15)
        g.attach()
        try:
            g.read(8)
            time.sleep(0.5)  # CPU-heavy fewshot build in the consumer: no read pending
            self.assertIsNone(g.reason)
            self.assertFalse(proc.killed)
            self.assertTrue(g.read(8))
        finally:
            g.stop()

    def test_dead_device_still_stalls(self):
        proc = FakeRec("stall")
        g = gw.CaptureGuard(proc, stall_s=0.15)
        g.attach()
        try:
            self.assertEqual(_within(lambda: g.read(8)), b"")
            self.assertEqual(g.reason, "stall")
            self.assertTrue(proc.killed)
        finally:
            g.stop()

    def test_first_stall_reopens_fast_repeat_backs_off(self):
        self.assertEqual(gw.reopen_delay("stall", 100, 0), 2.0)
        self.assertEqual(gw.reopen_delay("stall", 100, 90, gap=30), 20.0)
        self.assertEqual(gw.reopen_delay("silence", 200, 100, gap=30), 2.0)


if __name__ == "__main__":
    unittest.main()
