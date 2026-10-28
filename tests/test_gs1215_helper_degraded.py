"""0.12.15 (Nix re-review of 99a6774, and the self-review after it): degraded helper status loses nothing.

- A dismissal made while a save is waiting to be retried is part of the pending state: once the retry saves, the
  banner stays dismissed and the bots stay not confirmed.
- While the bot list is unreadable (or a rebuild is backing off), the rows show every home bot the station can see
  as not confirmed instead of going quiet, and a signed confirmation that arrives meanwhile survives the rebuild.
- Self-review: an entry with a valid bot id but a damaged value stays not confirmed (not dropped); helpers.json
  vanishing after the station saw it is treated like damage; a bot re-paired while degraded is not marked by the
  later rebuild.

Real handlers and node row render. Names and keys are made up.
"""
import json, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_gs1215_helper_failclosed as tfc  # noqa: E402

ta = tfc.t15.ta


class _D(tfc._Clocked):
    def break_list(self):
        r = self.st.r
        real = r.persona_list

        def flaky():
            if sys._getframe(1).f_code.co_name == "_paired_home_bots":
                raise RuntimeError("boom")
            return real()
        r.persona_list = flaky
        self.addCleanup(setattr, r, "persona_list", real)
        return lambda: setattr(r, "persona_list", real)

    def break_writes(self):
        r = self.st.r
        real = r.atomic_write

        def failing(path, data, mode=0o600):
            if path == r.HELPER_FILE:
                raise PermissionError("read-only")
            return real(path, data, mode)
        r.atomic_write = failing
        self.addCleanup(setattr, r, "atomic_write", real)
        return lambda: setattr(r, "atomic_write", real)


class DismissDuringPendingSave(_D):
    def test_dismissal_survives_the_retry(self):
        path = self.two_bots_then_corrupt()
        fix = self.break_writes()
        self.assertIs(self.banner(), True)
        st, j = self.c.req("POST", "/api/bots/helper-status/dismiss", {})
        self.assertEqual(st, 200, j)
        fix()
        self.t += 61
        self.assertIs(self.banner(), False)
        self.assertTrue(self.unconfirmed("oak"))
        self.assertTrue(self.unconfirmed("ivy"))
        with open(path) as f:
            self.assertNotIn("_recovery", json.load(f))
        self.t += 61
        self.assertIs(self.banner(), False)


class DegradedViewIsNotQuiet(_D):
    def test_rows_unconfirmed_while_list_unreadable(self):
        self.two_bots_then_corrupt()
        self.break_list()
        self.assertIs(self.banner(), True)
        self.assertTrue(self.unconfirmed("oak"))
        self.assertTrue(self.unconfirmed("ivy"))
        self.assertNotEqual(self.finish_row("oak")["state"], "done")

    def test_signed_confirmation_survives_rebuild(self):
        self.two_bots_then_corrupt()
        fix = self.break_list()
        self.assertIs(self.banner(), True)
        self.signed("oak")
        self.assertFalse(self.unconfirmed("oak"))
        fix()
        self.t += 61
        self.assertIs(self.banner(), True)  # ivy still has to confirm
        self.assertFalse(self.unconfirmed("oak"))
        self.assertTrue(self.unconfirmed("ivy"))
        self.signed("ivy")
        self.assertIs(self.banner(), False)


class SelfReview(_D):
    def test_damaged_entry_for_a_bot_stays_unconfirmed(self):
        self.two_bots_then_corrupt()
        self.signed("oak")
        self.signed("ivy")
        path = self.st.r.HELPER_FILE
        with open(path) as f:
            doc = json.load(f)
        doc["oak"] = 5
        with open(path, "w") as f:
            json.dump(doc, f)
        self.assertTrue(self.unconfirmed("oak"))
        self.assertFalse(self.unconfirmed("ivy"))

    def test_file_vanishing_is_treated_as_damage(self):
        self.st.webhooks.clear()
        self.run_flow(slug="ivy", reply_version=tfc.t15.ga.HELPER_RELEASE)
        self.assertIs(self.banner(), False)
        os.remove(self.st.r.HELPER_FILE)
        self.assertIs(self.banner(), True)
        self.assertTrue(self.unconfirmed("ivy"))

    def test_repaired_bot_not_marked_by_later_rebuild(self):
        self.two_bots_then_corrupt()
        fix = self.break_list()
        self.assertIs(self.banner(), True)
        st, j = self.c.req("DELETE", "/api/bots/oak", {"password": ta.GOOD})
        self.assertEqual(st, 200, j)
        self.st.r._test_last.clear()
        self.seen.clear()
        self.st.webhooks.clear()
        self.run_flow(slug="oak", reply_version=tfc.t15.ga.HELPER_RELEASE)
        self.assert_quiet()  # no flash after Add bot, even while degraded
        fix()
        self.t += 61
        self.banner()
        self.assertFalse(self.unconfirmed("oak"))
        self.assertTrue(self.unconfirmed("ivy"))


if __name__ == "__main__":
    unittest.main()
