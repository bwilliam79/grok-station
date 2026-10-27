"""A passing admin connection test marks the bot reachable. The listener is not started."""
import asyncio, os, sys, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
relay = None  # imported in setUp, after the other relay tests set their temp dirs at collection




class PassingTestClearsError(unittest.TestCase):
    def setUp(self):
        global relay
        self.tmp = tempfile.mkdtemp()
        old = os.environ.get("GROK_STATE_DIR")
        if "relay" not in sys.modules and old is None:
            os.environ["GROK_STATE_DIR"] = self.tmp  # run alone: keep relay out of /var/lib
        try:
            import relay as mod
        finally:
            if old is None:
                os.environ.pop("GROK_STATE_DIR", None)
        relay = mod

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
