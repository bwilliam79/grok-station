"""0.13.0 regression: a failed Test marks an already-online bot offline with the real reason, and a later pass
clears it. Before, a Test that ran out of time on the page left bot-status.json on the old answer, so the Bots
page kept the bot online."""
import asyncio, os, secrets, sys, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_avatar  # noqa: E402


class StatusAfterTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_avatar.Avatar.asyncSetUp
    asyncTearDown = test_avatar.Avatar.asyncTearDown

    def _rec(self):
        r = self.relay
        tid = "t_" + secrets.token_urlsafe(12)
        r._runs[tid] = {"slug": "jules", "state": "waiting", "t0": time.monotonic(), "elapsed_s": 0.0,
                        "answered_in_s": None, "error": None, "cancel": asyncio.Event(), "hook": None, "detail": {},
                        "then_avatar": False, "actor": "admin"}
        return tid

    def _status(self):
        doc = self.relay.read_json_file(self.relay.status_path())
        return (doc or {}).get("jules", {})

    async def test_failed_test_marks_online_bot_offline_and_pass_clears(self):
        r = self.relay
        real_teammate, real_limit, real_ask = r.ask_teammate, getattr(r, "TEST_LIMIT_S", None), r.ask_avatar

        async def silent_bot(http, slug, q, cancel=None, **kw):
            await cancel.wait()
            return None, "cancelled"

        async def good_bot(http, slug, q, **kw):
            return "hello", "ok"

        async def no_pic(slug, actor):
            return False

        try:
            r.ask_avatar = no_pic
            r._note_status("jules", answered=True, clear_error=True)  # known and online
            self.assertNotIn("last_error", self._status())
            r.ask_teammate, r.TEST_LIMIT_S = silent_bot, 0.3
            tid = self._rec()
            r._runs[tid]["cancel_after"] = 0.3  # older code: the page gave up; the relay saw a cancel
            waiter = asyncio.create_task(r._run_bot_test(tid))
            await asyncio.sleep(0.6)
            if not waiter.done():  # unfixed relay: no deadline, so the page's own clock cancels the test
                r._runs[tid]["cancel"].set()
            await asyncio.wait_for(waiter, 3)
            st = self._status()
            self.assertEqual(st.get("last_error"), "No answer")
            self.assertGreaterEqual(st.get("last_error_at", 0), st.get("last_answered_at", 0))
            await asyncio.sleep(1.1)  # a new second, as the page compares timestamps
            r.ask_teammate = good_bot
            tid = self._rec()
            await asyncio.wait_for(r._run_bot_test(tid), 3)
            self.assertNotIn("last_error", self._status())
        finally:
            r.ask_teammate, r.ask_avatar = real_teammate, real_ask
            if real_limit is not None:
                r.TEST_LIMIT_S = real_limit

    def test_configure_test_result_updates_the_row(self):
        js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "web", "app.js")).read()
        self.assertIn("noteTestResult(", js)


if __name__ == "__main__":
    unittest.main()
