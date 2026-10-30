#!/usr/bin/env python3
"""OPT2: poll and deadline ceilings need room above the wait they measure.

A slow machine was failing correct runs: the bots POST-failure poll used the same
2 s as REPLY_WAIT, the silent poll and the handoff timeout had about a second of
slack, and the display-status and Homebridge header deadlines were inside a second
of their budgets. Each ceiling stays under the "watchdog never fired" duration.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _fn(src, name):
    return src.split("def " + name, 1)[1].split("\n    def ", 1)[0]


class PollMargin(unittest.TestCase):
    def test_post_failure_poll_is_under_reply_wait_with_room(self):
        src = _read("tests/test_bots_api.py")
        mark = "red on POST failure, not after REPLY_WAIT"
        window = src[src.index(mark) - 900:src.index(mark)]
        wait = float(re.findall(r"REPLY_WAIT = (\d+(?:\.\d+)?)", window)[-1])
        bound = float(re.findall(r"assertLess\(time\.monotonic\(\) - t0, (\d+(?:\.\d+)?)\)", window)[-1])
        self.assertGreaterEqual(bound, 4.0)          # a slow POST still counts as immediate
        self.assertGreaterEqual(wait - bound, 4.0)   # waiting out REPLY_WAIT still fails

    def test_silent_poll_ceiling_clears_the_short_wait(self):
        src = _read("tests/test_bots_api.py")
        window = src[src.index("self.relay.REPLY_WAIT = 0.6"):][:700]
        timeout = float(re.search(r"_poll\(tid, timeout=(\d+(?:\.\d+)?)\)", window).group(1))
        bound = float(re.search(r"assertLess\(time\.monotonic\(\) - t0, (\d+(?:\.\d+)?)\)", window).group(1))
        self.assertGreaterEqual(bound - 0.6, 4.0)
        self.assertGreaterEqual(timeout, bound)  # the poll must not give up before the ceiling

    def test_handoff_timeout_poll_has_four_seconds_of_slack(self):
        src = _read("tests/test_handoff.py")
        wait = float(re.search(r'REPLY_WAIT="(\d+(?:\.\d+)?)"', src).group(1))
        fn = _fn(src, "test_no_reply_times_out")
        bound = float(re.search(r"assertLess\(time\.monotonic\(\) - t0, (\d+(?:\.\d+)?)\)", fn).group(1))
        self.assertGreaterEqual(bound - wait, 4.0)

    def test_display_status_deadline_has_three_seconds_of_slack(self):
        fn = _fn(_read("tests/test_gs132_display_status.py"), "test_overall_deadline_holds_against_a_trickling_body")
        bounds = [float(n) for n in re.findall(r"assertLess\(time\.monotonic\(\) - t0, (\d+(?:\.\d+)?)\)", fn)]
        self.assertEqual(len(bounds), 2)
        for bound in bounds:
            self.assertGreaterEqual(bound - 1.5, 3.0)  # timeout is 1.5; a missed deadline trickles for 8 s

    def test_homebridge_header_watchdog_has_slack_under_a_full_trickle(self):
        src = _read("tests/test_gs131_homebridge_api.py")
        names = (
            "test_gs134_watchdog_cuts_trickled_headers",
            "test_gs134_verify_trickled_headers_is_signin_unreachable",
            "test_gs134_saved_login_trickled_headers_is_unreachable",
            "test_gs134_short_path_watchdog_cuts_trickled_headers",
        )
        for name in names:
            fn = _fn(src, name)
            bound = float(re.search(r"assertLess\(time\.monotonic\(\) - start, (\d+(?:\.\d+)?)\)", fn).group(1))
            self.assertGreaterEqual(bound - 0.5, 1.5, name)  # budget / accessory timeout is 0.5 s
            self.assertLess(bound, 3.2, name)                # a full header trickle is about 3.5 s

    def test_bot_test_deadline_poll_has_three_seconds_of_slack(self):
        fn = _fn(_read("tests/test_gs130_test_deadline.py"), "test_server_gives_up_at_the_shared_limit")
        bound = float(re.search(r'assertLess\(r\._runs\[tid\]\["elapsed_s"\], (\d+(?:\.\d+)?)\)', fn).group(1))
        self.assertGreaterEqual(bound - 0.3, 3.0)  # TEST_LIMIT_S is 0.3 in this test; REPLY_WAIT is minutes


if __name__ == "__main__":
    unittest.main()
