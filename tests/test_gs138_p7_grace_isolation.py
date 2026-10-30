#!/usr/bin/env python3
"""P7 (seed=101 suite order at the P6 rc): StepUp's grace tests leave password_grace_min 0 in the shared state dir,
so test_update_flow's session round trip, run after them, can't open a step-up window. Run that exact pair."""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_admin  # noqa: E402
import test_update_flow  # noqa: E402


class GraceDoesNotLeak(unittest.TestCase):
    def test_grace_zero_then_session_round_trip(self):
        suite = unittest.TestSuite([test_admin.StepUp("test_grace_zero_always_asks"),
                                    test_update_flow.Flow("test_session_file_round_trip")])
        r = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        self.assertEqual([f[0].id() for f in r.failures + r.errors], [])


if __name__ == "__main__":
    unittest.main()
