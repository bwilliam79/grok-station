"""Mic icon stuck after a USB reset (gs138).

A reset writes authorized 0 then 1, so the speakerphone leaves /proc/asound/cards while it
re-enumerates. check_mic used to report that as a missing mic before it looked at any planned
deadline, and the listener killed arecord only after the device was already deauthorized, which
can leave the read stuck and audio_at stale. A planned reset publishes reset_until and drops
capture first. A real unplug (no reset_until, or only reopen_by) still shows the problem.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wake"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import grok_wake as gw
import grok_station as gs

CARDS = " 2 [Plus           ]: USB-Audio - EMEET OfficeCore M0 Plus"
DEV = "plughw:CARD=Plus,DEV=0"


class MicResetIcon(unittest.TestCase):
    def setUp(self):
        self._live = dict(gw.LIVE)

    def tearDown(self):
        gw.LIVE.clear()
        gw.LIVE.update(self._live)

    def test_status_payload_has_reset_until(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "wake", "grok_wake.py"), encoding="utf-8").read()
        self.assertIn('"reset_until": LIVE.get("reset_until")', src)
        self.assertIsNone(gw.LIVE.get("reset_until"))

    def test_planned_reset_keeps_the_icon_ok(self):
        # The card is gone for the re-enumeration, and the open capture's last frame is old.
        gone = {"capture": None, "audio_at": 900.0, "reopen_by": None, "reset_until": 1020.0}
        self.assertEqual(gs.check_mic(1010.0, lambda: gone, "")["detail"], "ok")
        stalled = {"capture": DEV, "audio_at": 1000.0, "reset_until": 1040.0}
        self.assertEqual(gs.check_mic(1035.0, lambda: stalled, CARDS)["detail"], "ok")
        # Past the deadline plus the same 10 s grace as a reopen, it is a real unplug again.
        self.assertEqual(gs.check_mic(1031.0, lambda: gone, "")["detail"], "speakerphone missing")

    def test_reopen_or_a_bad_deadline_does_not_hide_an_unplug(self):
        waiting = {"capture": None, "audio_at": 900.0, "reopen_by": 1030.0}
        self.assertEqual(gs.check_mic(1010.0, lambda: waiting, "")["detail"], "speakerphone missing")
        self.assertEqual(gs.check_mic(1010.0, lambda: None, "")["detail"], "speakerphone missing")
        stale = {"capture": DEV, "audio_at": -30.0}
        for bad in (True, False, "session", "forever", 1e12, -1, None, [1]):
            payload = dict(stale, reset_until=bad)
            self.assertEqual(gs.check_mic(5.0, lambda p=payload: p, CARDS)["detail"], "capture stalled", bad)

    def test_reset_arms_the_hold_and_drops_capture_before_deauthorize(self):
        clock = {"t": 1000.0}
        order = []

        def kick():
            order.append("kick")

        def reset(card):
            order.append("reset")
            # What the display reads while authorized is 0 and the card is absent.
            seen = gs.check_mic(clock["t"], lambda: {
                "capture": gw.LIVE.get("capture"), "audio_at": gw.LIVE.get("audio_at"),
                "reopen_by": gw.LIVE.get("reopen_by"), "reset_until": gw.LIVE.get("reset_until"),
            }, "")
            order.append(seen["detail"])
            self.assertEqual(card, "M0")
            return None

        rec = gw.MicRecovery(reset, now=lambda: clock["t"], card=lambda: "M0", busy=lambda: False,
                             kick=kick, log=lambda msg: None,
                             flags=lambda: {"button": True, "night": False, "drought": False})
        gw.LIVE["capture"] = DEV
        gw.LIVE["audio_at"] = 1000.0
        self.assertEqual(rec.request("button"), "ok")
        self.assertEqual(order, ["kick", "reset", "ok"])
        until = gw.LIVE.get("reset_until")
        self.assertIsInstance(until, float)
        self.assertEqual(until, 1000.0 + gw.MIC_RESET_ICON_HOLD_S)
        self.assertLessEqual(gw.MIC_RESET_ICON_HOLD_S, 30 + 10)

    def test_no_card_retry_does_not_extend_the_hold(self):
        gw.LIVE["reset_until"] = 1020.0
        gw.run_capture_rounds(lambda: None, lambda cap: None, lambda buf, guard: None, lambda: False,
                              rounds=1, sleep=lambda s: None, now=lambda: 1005.0, log=lambda m: None)
        self.assertEqual(gw.LIVE.get("reset_until"), 1020.0)

    def test_failed_reset_does_not_keep_the_hold(self):
        clock = {"t": 1000.0}

        def reset(card):
            return "refused: reset failed"

        rec = gw.MicRecovery(reset, now=lambda: clock["t"], card=lambda: "M0", busy=lambda: False,
                             kick=lambda: None, log=lambda msg: None,
                             flags=lambda: {"button": True, "night": False, "drought": False})
        self.assertEqual(rec.request("button"), "failed")
        self.assertIsNone(gw.LIVE.get("reset_until"))


if __name__ == "__main__":
    unittest.main()
