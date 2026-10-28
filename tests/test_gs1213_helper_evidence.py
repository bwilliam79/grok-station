"""0.12.13: the helper warning is based on evidence, never on absence (Nix M1 and L1 on 0.12.12).

Every post a current helper makes (gs-seal-reply reply, show and avatar, which gs-update.sh also uses for its status
line) carries X-GS-Helper. So a seal-verified post from a home bot WITHOUT that header comes from a helper older than
0.11.7: the row shows 'Helper version not confirmed: use Update' and the Bots-menu dot counts it. A bot that has not
posted yet shows nothing (the owner saw a warning flash right after Add bot three times). A signed helper version
always wins over the unsigned header, and a header never overwrites it.

Same real handlers and node row render as test_gs1212_helper_e2e. Nothing leaves the box; names and keys are made up.
"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_gs1212_helper_e2e as e2e  # noqa: E402

ga = e2e.ga
UNCONF = "Helper version not confirmed: use Update"


class _F(e2e._Flow):
    def finish_row(self, slug="ivy"):
        st, j = self.c.req("GET", "/api/finish-update")
        self.assertEqual(st, 200, j)
        for g in j.get("computers") or j.get("groups") or []:
            for b in g.get("bots") or []:
                if b.get("slug") == slug:
                    return b
        self.fail("no finish row for %s: %r" % (slug, j))

    def assert_quiet(self):
        for step, h, notes in self.seen:
            self.assertFalse(h and (h.get("out_of_date") or h.get("state") == "unconfirmed"), "%s: %r" % (step, h))
            if notes is not None:
                self.assertNotIn(e2e.WARN, notes, step)
                self.assertNotIn(UNCONF, notes, step)


class OldHelperWithoutHeader(_F):
    def test_no_header_after_first_sealed_reply_shows_badge_and_dot(self):
        self.add_bot()
        b, notes = self.check("before any post")
        self.assertEqual(b["helper"]["state"], "unknown")  # not posted yet: quiet
        ask = self.wait_hook()
        st, j = self.reply("ivy", ask["request_id"], "Hi, I'm here.", None)  # pre-0.11.7 helper: no header
        self.assertEqual(st, 200, j)
        b, notes = self.check("after headerless reply")
        self.assertEqual(b["helper"]["state"], "unconfirmed", b["helper"])
        self.assertIs(b["helper"]["out_of_date"], True)
        if notes is not None:
            self.assertIn(UNCONF, notes)
            self.assertNotIn(e2e.WARN, notes)
        self.assertNotEqual(self.finish_row()["state"], "done")  # counted in the Bots-menu dot

    def test_delete_clears_the_evidence(self):
        self.run_flow(reply_version=None)
        self.assertEqual(self.bot()["helper"]["state"], "unconfirmed")
        st, j = self.c.req("DELETE", "/api/bots/ivy", {"password": ga_pw()})
        self.assertEqual(st, 200, j)
        self.st.r._test_last.clear(); self.st.webhooks.clear(); self.seen.clear()
        self.run_flow(reply_version=ga.HELPER_RELEASE)
        self.assert_quiet()


class FreshAddStaysQuiet(_F):
    def test_fresh_add_then_test_reply_with_header(self):
        self.run_flow(reply_version=ga.HELPER_RELEASE)
        self.assert_quiet()
        self.assertEqual(self.bot()["helper"]["state"], "current")
        self.assertEqual(self.finish_row()["state"], "done")

    def test_readd_with_header(self):
        self.run_flow(reply_version=ga.HELPER_RELEASE)
        st, j = self.c.req("DELETE", "/api/bots/ivy", {"password": ga_pw()})
        self.assertEqual(st, 200, j)
        self.st.r._test_last.clear(); self.st.webhooks.clear(); self.seen.clear()
        self.run_flow(reply_version=ga.HELPER_RELEASE)
        self.assert_quiet()


class SignedWins(_F):
    def test_signed_old_plus_newer_header_stays_behind(self):
        self.run_flow(reply_version=ga.HELPER_RELEASE)
        self.ask_update()
        ask = self.wait_hook(kind="update_bot")
        st, j = self.reply("ivy", ask["request_id"],
                           "%s instr=%s helper=0.11.6 computer=-" % (ask["release"], "0123456789abcdef"),
                           ga.HELPER_RELEASE)
        self.assertEqual(st, 200, j)
        b, notes = self.check("after signed old status")
        self.assertEqual(b["helper"]["state"], "behind", b["helper"])
        self.assertEqual(b["helper"]["version"], "0.11.6")
        # A later post with a newer header must not overwrite the signed value.
        self.st.post_voice("/voice/avatar", e2e.helper.build_avatar("ivy", e2e._png(), self.seed, self.station_pub()),
                           ga.HELPER_RELEASE)
        b, notes = self.check("after newer header")
        self.assertEqual(b["helper"]["version"], "0.11.6", b["helper"])
        self.assertEqual(b["helper"]["state"], "behind")
        self.assertEqual(self.st.r.helper_records()["ivy"]["signed"], "0.11.6")
        if notes is not None:
            self.assertIn(e2e.WARN, notes)


def ga_pw():
    return e2e.ta.GOOD


if __name__ == "__main__":
    unittest.main()
