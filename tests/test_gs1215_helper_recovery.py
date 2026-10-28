"""0.12.15: Nix's Low on 0.12.14, recovery from a corrupt helpers.json, through the real handlers and node render.

Before: the first bot post after corruption rewrote helpers.json with only that bot, so every other bot's helper
history vanished and the banner cleared. Now the station rewrites the file at once with a recovery marker: every
paired home bot at that moment shows 'Helper version not confirmed' (and counts in the Bots-menu dot) until it sends a
signed helper version, and the 'Helper status unreadable' banner stays until all have, or an admin dismisses it.
No copy of the bad file is kept. A bot added afterwards follows the normal rules (no flash after Add bot).

Names and keys are made up. Nothing leaves the box.
"""
import json, os, stat, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_gs1212_helper_e2e as e2e  # noqa: E402
import test_gs1213_helper_evidence as t13  # noqa: E402
import test_gs1214_helper_hardening as t14  # noqa: E402

ga = e2e.ga
ta = e2e.ta
UNCONF = t13.UNCONF


class _R(t13._F):
    def check(self, step, slug=None):
        return super().check(step, slug or getattr(self, "_cur", "ivy"))

    def run_flow(self, slug="ivy", reply_version=None):
        self._cur = slug  # run_flow's own checks look at the bot being added
        try:
            return super().run_flow(slug=slug, reply_version=reply_version)
        finally:
            self._cur = "ivy"

    def add_bot(self, slug="ivy"):
        """Same calls as the Add bot dialog, but keeps the other bots' loopback webhooks."""
        c = self.c
        st, j = c.req("POST", "/api/bots", {"name": slug.capitalize(), "slug": slug, "key_mode": "home",
                                             "voice": "ara", "reply_host": "box.example.lan", "password": ta.GOOD})
        self.assertEqual(st, 201, j)
        self.check("after create", slug)
        st, j = c.req("PUT", "/api/bots/%s/key" % slug, {"public_key": self.pub, "password": ta.GOOD})
        self.assertEqual(st, 200, j)
        self.check("after pairing", slug)
        st, j = c.req("PUT", "/api/bots/%s/webhook" % slug, {"url": e2e.URL, "key": e2e.HOOK, "password": ta.GOOD})
        self.assertEqual(st, 200, j)
        hooks = getattr(self, "_hooks", {})
        hooks[slug] = {"url": self.st.hook_url(), "token": e2e.HOOK, "saved_at": 10}
        self._hooks = hooks
        self.assertTrue(self.st.r.write_webhooks(dict(hooks)))
        self.check("after webhook", slug)
        st, j = c.req("POST", "/api/bots/%s/test" % slug, {"then_avatar": True})
        self.assertIn(st, (200, 202), j)
        self.tid = j["test_id"]
        self.check("test waiting", slug)

    def two_bots_then_corrupt(self):
        for s in ("ivy", "oak"):
            self.st.webhooks.clear()
            self.run_flow(slug=s, reply_version=ga.HELPER_RELEASE)
            self.st.r._test_last.clear()
        path = self.st.r.HELPER_FILE
        with open(path, "w") as f:
            f.write("{not json")
        self.n0 = len(self.st.lines)
        return path

    def banner(self):
        st, j = self.c.req("GET", "/api/bots")
        self.assertEqual(st, 200, j)
        return j.get("helper_status_unreadable")

    def unconfirmed(self, slug):
        b, notes = self.check("state of " + slug, slug)
        if notes is not None:
            (self.assertIn if b["helper"]["state"] == "unconfirmed" else self.assertNotIn)(UNCONF, notes)
        return b["helper"]["state"] == "unconfirmed"

    def signed(self, slug):
        self.st.webhooks.clear()
        self.ask_update(slug)
        ask = self.wait_hook(kind="update_bot")
        line = "%s instr=%s helper=%s computer=-" % (ask["release"], "0123456789abcdef", ga.HELPER_RELEASE)
        st, j = self.reply(slug, ask["request_id"], line, ga.HELPER_RELEASE)
        self.assertEqual(st, 200, j)

    def post_a(self):
        st, j = self.st.post_voice("/voice/avatar", e2e.helper.build_avatar("ivy", e2e._png(), self.seed,
                                                                            self.station_pub()), ga.HELPER_RELEASE)
        self.assertEqual(st, 200, j)


class CorruptRecovery(_R):
    def test_other_bot_stays_unconfirmed_after_one_posts(self):
        path = self.two_bots_then_corrupt()
        self.post_a()  # bot A posts first: before 0.12.15 this rewrote the file with A only
        self.assertTrue(self.unconfirmed("oak"))
        self.assertTrue(self.unconfirmed("ivy"))  # a header alone is not a confirmation
        self.assertNotEqual(self.finish_row("oak")["state"], "done")  # Bots-menu dot
        self.assertIs(self.banner(), True)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if n.startswith("helpers")], ["helpers.json"])
        logged = "\n".join(self.st.lines[self.n0:]).lower()
        self.assertNotIn("ivy", logged)
        self.assertNotIn("oak", logged)
        banner = t14.render_banner({"helper_status_unreadable": True})
        if banner is not None:
            self.assertIn("Helper status unreadable", banner)

    def test_signed_versions_clear_bots_then_banner(self):
        self.two_bots_then_corrupt()
        self.post_a()
        self.signed("oak")
        self.assertFalse(self.unconfirmed("oak"))
        self.assertTrue(self.unconfirmed("ivy"))
        self.assertIs(self.banner(), True)  # ivy has not re-confirmed yet
        self.signed("ivy")
        self.assertFalse(self.unconfirmed("ivy"))
        self.assertIs(self.banner(), False)

    def test_admin_dismiss_clears_banner(self):
        self.two_bots_then_corrupt()
        self.assertIs(self.banner(), True)
        st, j = self.c.req("POST", "/api/bots/helper-status/dismiss", {})
        self.assertEqual(st, 200, j)
        self.assertIs(self.banner(), False)
        self.assertTrue(self.unconfirmed("oak"))  # the bots themselves still need to confirm

    def test_dismiss_needs_admin_origin_header(self):
        self.two_bots_then_corrupt()
        self.assertIs(self.banner(), True)
        st, j = self.c.req("POST", "/api/bots/helper-status/dismiss", {}, csrf=False)
        self.assertEqual(st, 403, j)
        self.assertIs(self.banner(), True)

    def test_bot_added_after_corruption_does_not_flash(self):
        self.two_bots_then_corrupt()
        self.assertIs(self.banner(), True)
        self.seen.clear()
        self.st.webhooks.clear()
        self.run_flow(slug="elm", reply_version=ga.HELPER_RELEASE)
        self.assert_quiet()
        self.assertTrue(self.unconfirmed("oak"))

    def test_delete_drops_bot_from_recovery(self):
        self.two_bots_then_corrupt()
        self.assertIs(self.banner(), True)
        self.signed("ivy")
        st, j = self.c.req("DELETE", "/api/bots/oak", {"password": ta.GOOD})
        self.assertEqual(st, 200, j)
        self.assertIs(self.banner(), False)


if __name__ == "__main__":
    unittest.main()
