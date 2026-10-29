"""0.13.0 regression: the automatic picture ask is recorded only when ask_avatar returns True.

Before the fix a failed ask (webhook down, refused) was still written to avatar-auto.json, so the station never
asked that bot again by itself."""
import asyncio, os, secrets, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_avatar  # noqa: E402  (reuses its temp state dirs and relay import)




class AvatarAutoFail(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_avatar.Avatar.asyncSetUp  # only the fixture, not Avatar's own tests
    asyncTearDown = test_avatar.Avatar.asyncTearDown

    async def test_failed_auto_ask_is_not_recorded(self):
        r = self.relay
        asked = []
        real_ask, real_teammate = r.ask_avatar, r.ask_teammate

        async def failing_ask(slug, actor):
            asked.append(slug)
            return False

        async def fake_teammate(http, slug, q, **kw):
            return "hello", "ok"

        for f in [os.path.join(r.PORTRAIT_WRITE, "jules.png")] + [os.path.join(d, "jules.png") for d in r.PORTRAIT_DIRS]:
            if os.path.exists(f):
                os.unlink(f)
        r._avatar_auto.clear()
        if os.path.exists(r.AVATAR_AUTO_FILE):
            os.unlink(r.AVATAR_AUTO_FILE)
        try:
            r.ask_avatar, r.ask_teammate = failing_ask, fake_teammate
            self.assertTrue(r.avatar_auto_due("jules"))
            tid = "t_" + secrets.token_urlsafe(12)
            r._runs[tid] = {"slug": "jules", "state": "waiting", "t0": 0.0, "elapsed_s": 0.0, "answered_in_s": None,
                            "error": None, "cancel": asyncio.Event(), "hook": None, "detail": {},
                            "then_avatar": False, "actor": "admin"}
            await r._run_bot_test(tid)
            self.assertEqual(r._runs[tid]["state"], "ok")
            self.assertEqual(asked, ["jules"])
            self.assertTrue(r.avatar_auto_due("jules"))  # a failed ask must not count as done
            self.assertNotIn("jules", r._avatar_auto_disk())
        finally:
            r.ask_avatar, r.ask_teammate = real_ask, real_teammate
            r._avatar_auto.clear()


if __name__ == "__main__":
    unittest.main()
