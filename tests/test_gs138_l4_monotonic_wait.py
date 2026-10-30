#!/usr/bin/env python3
"""L4: the handoff reply waits run on the monotonic clock, so a wall-clock step (seen on the box: +152 s and
+163 s steps during VM pauses; NTP steps on a Pi) can't cut a wait short or stretch it."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8").read()


class MonotonicWait(unittest.TestCase):
    def test_no_wall_clock_wait_loops(self):
        self.assertNotRegex(SRC, r"while time\.time\(\) < deadline")
        self.assertNotIn("deadline - time.time()", SRC)

    def test_both_waits_use_monotonic(self):
        self.assertEqual(len(re.findall(r"deadline = time\.monotonic\(\) \+ REPLY_WAIT", SRC)), 2)
        self.assertEqual(len(re.findall(r"while time\.monotonic\(\) < deadline:", SRC)), 2)
        self.assertEqual(len(re.findall(r"deadline - time\.monotonic\(\)", SRC)), 2)


if __name__ == "__main__":
    unittest.main()
