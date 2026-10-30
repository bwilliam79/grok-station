#!/usr/bin/env python3
"""NOPT: an untrusted or missing train_flow mid-session speaks the fixed failed-start line instead of raising."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINE = "I couldn't start recording. Try again in a moment."


def _block():
    src = open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8").read()
    i = src.index("train = train_wake_request(heard, connect_bots(roster, list_source_slug()))")
    return src[i:src.index("result = train_session_result(plan)", i)]


class UntrustedFlowMidSession(unittest.TestCase):
    def test_load_failure_is_caught(self):
        b = _block()
        self.assertIn("plan = _train_flow().voice_train_plan(", b)
        self.assertIn("except RuntimeError:", b)
        self.assertIn('plan = {"begin": False, "say": ENROLL_START_FAILED, "enroll": None, "hold_s": 4}', b)

    def test_fallback_plan_is_spoken_once_and_held_4s(self):
        os.environ.setdefault("RELAY_TOKEN", "test-token")
        os.environ.setdefault("XAI_API_KEY", "test-key")
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        import relay
        out = relay.train_session_result({"begin": False, "say": relay.ENROLL_START_FAILED, "enroll": None, "hold_s": 4})
        self.assertEqual(out["say"], LINE)
        self.assertIsNone(out["enroll"])
        self.assertEqual(out["done"], {"type": "done", "local": True, "hold_s": 4, "text": LINE})


if __name__ == "__main__":
    unittest.main()
