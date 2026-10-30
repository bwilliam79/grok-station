#!/usr/bin/env python3
"""Item 3 (Noor, final copy): a failed enroll start is spoken once and shown on the tile for 4 s. Nothing is saved."""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINE = "I couldn't start recording. Try again in a moment."


def _block():
    src = open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8").read()
    i = src.index("train = train_wake_request(heard, connect_bots(roster, list_source_slug()))")
    return src[i:src.index('return "user ended"', i)]


class EnrollFailTile(unittest.TestCase):
    def test_line_is_final_copy(self):
        src = open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8").read()
        self.assertIn('ENROLL_START_FAILED = "%s"' % LINE, src)

    def test_spoken_once(self):
        self.assertEqual(_block().count("play_fixed(fail_say"), 1)

    def test_done_carries_the_line_for_4_seconds(self):
        b = _block()
        i = b.index("if fail_say:")
        self.assertIn('done["hold_s"] = 4', b[i:])
        self.assertIn('done["text"] = fail_say', b[i:])

    def test_listener_and_station_show_a_4s_line(self):
        wake = open(os.path.join(ROOT, "wake", "grok_wake.py"), encoding="utf-8").read()
        self.assertIn('elif ev.get("hold_s") == 4 and str(ev.get("text") or "").strip():', wake)
        self.assertIn('self.state("notice", text=self.reply, hold_s=4)', wake)


if __name__ == "__main__":
    unittest.main()
