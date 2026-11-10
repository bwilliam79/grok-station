"""0.13.37 regression: a bot Test waits up to 3 minutes for the reply.

At 0.13.36 TEST_LIMIT_S was 30. A bot that answered after 30 s failed the Test, and the station dropped the pending
request at the limit, so the late reply was refused with 403 "request". Now the limit is 180 s (REPLY_WAIT), the
pending stays for the whole window, and the page says it waits until the reply, up to 3:00.
"""
import asyncio, os, re, secrets, sys, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_avatar  # noqa: E402  (temp state dirs and relay import)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestWindow(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_avatar.Avatar.asyncSetUp
    asyncTearDown = test_avatar.Avatar.asyncTearDown

    async def test_a_reply_45_seconds_in_passes_and_keeps_the_pending(self):
        r = self.relay
        real = (r.ask_teammate, r.handoff.remove_pending)
        removed = []

        async def slow_bot(http, slug, q, cancel=None, detail=None, **kw):
            detail["rid"] = "rid-slow"
            await asyncio.sleep(0.05)  # the reply lands 45 s after the Test started (t0 below)
            return "hello", "ok"

        try:
            r.ask_teammate = slow_bot
            r.handoff.remove_pending = lambda d, rid: removed.append(rid)
            tid = "t_" + secrets.token_urlsafe(12)
            r._runs[tid] = {"slug": "jules", "state": "waiting", "t0": time.monotonic() - 45.0,
                            "elapsed_s": 0.0, "answered_in_s": None, "error": None, "cancel": asyncio.Event(),
                            "hook": None, "detail": {}, "then_avatar": False, "actor": "admin"}
            view = r.test_view(r._runs[tid])
            self.assertEqual(view["limit_s"], 180)
            self.assertGreater(view["remaining_s"], 130)
            await asyncio.wait_for(r._run_bot_test(tid), 5)
            self.assertEqual(r._runs[tid]["state"], "ok", r._runs[tid])
            self.assertEqual(removed, [])  # the pending was not dropped, so no 403 "request"
        finally:
            r.ask_teammate, r.handoff.remove_pending = real

    def test_limit_matches_reply_wait_and_the_page(self):
        r = self.relay
        self.assertEqual(r.TEST_LIMIT_S, 180)
        self.assertGreaterEqual(r.TEST_LIMIT_S, 170)
        js = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        self.assertRegex(js, r"const TEST_LIMIT_S = 180;")
        self.assertNotIn("Gives up after ${TEST_LIMIT_S} seconds.", js)
        self.assertEqual(js.count("Waiting until the reply comes, up to ${mss(TEST_LIMIT_S)}."), 2)


if __name__ == "__main__":
    unittest.main()
