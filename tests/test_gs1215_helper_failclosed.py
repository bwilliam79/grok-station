"""0.12.15 (Nix review of b39015f): helper status recovery fails closed and backs off.

(1) If helpers.json is corrupt AND the bot list (keys, overlay or personas) cannot be loaded, the station must not
    rewrite the file from an empty guess: the bad file stays, the banner stays, a bot post writes nothing, and the
    log names only the error type. Once the list loads again (next retry), recovery marks every paired home bot.
(2) If the recovery rewrite fails, it is retried at most once a minute (injectable clock), and the warning view
    (banner plus 'not confirmed' rows) is kept in between.

Real handlers and node row render, like test_gs1215_helper_recovery. Names and keys are made up.
"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_gs1215_helper_recovery as t15  # noqa: E402


class _Clocked(t15._R):
    def setUp(self):
        super().setUp()
        r = self.st.r
        self.t = 1000.0
        if hasattr(r, "_helper_clock"):
            old = r._helper_clock
            r._helper_clock = lambda: self.t
            self.addCleanup(setattr, r, "_helper_clock", old)
        self.addCleanup(self._reset)

    def _reset(self):
        r = self.st.r
        if hasattr(r, "_helper_reset_state"):
            r._helper_reset_state()


class BotListErrorFailsClosed(_Clocked):
    def test_list_error_keeps_bad_file_and_banner(self):
        r = self.st.r
        path = self.two_bots_then_corrupt()
        real = r.persona_list

        def flaky():
            if sys._getframe(1).f_code.co_name == "_paired_home_bots":
                raise RuntimeError("boom")
            return real()
        r.persona_list = flaky
        self.addCleanup(setattr, r, "persona_list", real)
        self.assertIs(self.banner(), True)
        self.post_a()  # a bot post must not replace the bad file with a partial one
        self.assertIs(self.banner(), True)
        with open(path) as f:
            self.assertEqual(f.read(), "{not json")
        logged = "\n".join(self.st.lines[self.n0:])
        self.assertIn("RuntimeError", logged)
        self.assertNotIn("boom", logged)
        self.assertNotIn("ivy", logged.lower())
        st, j = self.c.req("POST", "/api/bots/helper-status/dismiss", {})
        self.assertEqual(st, 200, j)  # the dismissal waits in memory; the file is still not rewritten on a guess
        self.assertIs(self.banner(), True)  # still degraded
        with open(path) as f:
            self.assertEqual(f.read(), "{not json")
        self.assertTrue(self.unconfirmed("oak"))  # degraded view: rows stay not confirmed
        # The list loads again: the next retry (a minute later) builds the full recovery set.
        r.persona_list = real
        self.t += 61
        self.assertIs(self.banner(), False)  # rebuilt and saved; the earlier dismissal holds
        self.assertTrue(self.unconfirmed("oak"))
        self.assertTrue(self.unconfirmed("ivy"))


class RewriteFailureBacksOff(_Clocked):
    def test_failed_rewrite_retries_once_a_minute(self):
        r = self.st.r
        self.two_bots_then_corrupt()
        real = r.atomic_write
        tries = []

        def failing(path, data, mode=0o600):
            if path == r.HELPER_FILE:
                tries.append(self.t)
                raise PermissionError("read-only")
            return real(path, data, mode)
        r.atomic_write = failing
        self.addCleanup(setattr, r, "atomic_write", real)
        for _ in range(4):
            self.assertIs(self.banner(), True)
            self.t += 5
        self.assertTrue(self.unconfirmed("oak"))  # the warning view is kept while backing off
        self.assertEqual(len(tries), 1, tries)
        self.t += 61
        self.assertIs(self.banner(), True)
        self.assertEqual(len(tries), 2, tries)
        r.atomic_write = real
        self.t += 61
        self.assertIs(self.banner(), True)  # repaired, recovery still pending
        self.assertTrue(self.unconfirmed("oak"))
        self.assertFalse(r._helper_file_bad[0])


if __name__ == "__main__":
    unittest.main()
