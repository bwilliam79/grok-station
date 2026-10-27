"""A passing admin connection test marks the bot reachable. The listener is not started."""
import asyncio, os, sys, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import relay  # noqa: E402


class PassingTestClearsError(unittest.TestCase):
    def test_handler_clears_last_error_even_if_the_handoff_does_not(self):
        d = tempfile.mkdtemp()
        old_hooks = relay.WEBHOOKS_FILE
        old_ask = relay.ask_teammate
        tid = "t_passingtest01"
        relay.WEBHOOKS_FILE = os.path.join(d, "webhooks.json")
        relay._note_status("kai", error="No answer")
        before = relay.status_view("kai", True)
        self.assertEqual(before["state"], "error")
        self.assertEqual(before["last_error"], "No answer")
        self.assertIsNotNone(before["last_error_at"])

        async def fake_ask(*_a, **_k):
            return "ok", "ok"

        relay.ask_teammate = fake_ask
        relay._runs[tid] = {
            "slug": "kai", "state": "waiting", "t0": time.monotonic(), "elapsed_s": 0.0,
            "answered_in_s": None, "error": "No answer", "cancel": asyncio.Event(), "detail": {},
        }
        try:
            asyncio.run(relay._run_bot_test(tid))
            after = relay.status_view("kai", True)
            self.assertEqual(relay._runs[tid]["state"], "ok")
        finally:
            relay.ask_teammate = old_ask
            relay.WEBHOOKS_FILE = old_hooks
            relay._runs.pop(tid, None)
        self.assertIsNone(after["last_error"])
        self.assertIsNone(after["last_error_at"])
        self.assertEqual(after["state"], "connected")
        self.assertIsNotNone(after["last_answered_at"])


if __name__ == "__main__":
    unittest.main()
