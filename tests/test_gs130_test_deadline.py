"""0.13.0 regression: the bot Test gives up on the server at TEST_LIMIT_S (30 s), the same deadline the admin
countdown shows. Before, the server waited REPLY_WAIT (minutes) and the page failed on its own clock at ~0:33-0:35."""
import asyncio, os, secrets, sys, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_avatar  # noqa: E402  (temp state dirs and relay import)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestDeadline(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_avatar.Avatar.asyncSetUp
    asyncTearDown = test_avatar.Avatar.asyncTearDown

    async def test_server_gives_up_at_the_shared_limit(self):
        r = self.relay
        self.assertEqual(getattr(r, "TEST_LIMIT_S", None), 30)
        real_teammate, real_limit = r.ask_teammate, r.TEST_LIMIT_S

        async def silent_bot(http, slug, q, cancel=None, **kw):
            await cancel.wait()  # never answers; only the station's deadline ends it
            return None, "cancelled"

        try:
            r.ask_teammate, r.TEST_LIMIT_S = silent_bot, 0.3
            tid = "t_" + secrets.token_urlsafe(12)
            r._runs[tid] = {"slug": "jules", "state": "waiting", "t0": time.monotonic(),
                            "elapsed_s": 0.0, "answered_in_s": None, "error": None, "cancel": asyncio.Event(),
                            "hook": None, "detail": {}, "then_avatar": False, "actor": "admin"}
            view = r.test_view(r._runs[tid])
            self.assertEqual(view["limit_s"], 0.3)
            await asyncio.wait_for(r._run_bot_test(tid), 3)
            self.assertEqual(r._runs[tid]["state"], "failed")
            self.assertEqual(r._runs[tid]["error"], "No answer")
            self.assertLess(r._runs[tid]["elapsed_s"], 1.5)
            self.assertEqual(r.test_view(r._runs[tid])["remaining_s"], 0.0)
        finally:
            r.ask_teammate, r.TEST_LIMIT_S = real_teammate, real_limit

    def test_page_counts_down_to_the_server_deadline(self):
        js = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        self.assertNotRegex(js, r"sec\s*>=\s*35")  # no private page clock that outlives the server
        self.assertIn("limit_s", js)
        admin = open(os.path.join(ROOT, "admin", "grok_admin.py")).read()
        self.assertRegex(admin, r'"limit_s"')
        self.assertRegex(admin, r'"remaining_s"')


if __name__ == "__main__":
    unittest.main()
