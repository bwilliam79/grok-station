"""0.12.15 (Nix Info items on 1513162), through the real handlers.

I1: while the bot list cannot load, the degraded view marks only bots with a pinned key when the keys file is
    readable (a home bot whose key was removed is not marked).
I2: a helpers.json deleted while the relay was stopped (a new process) still counts as damage: a 0600 marker with
    no bot data says the file existed.
I3: the banner says Hide warning can't hide it while an update-status save is failing.
I4: writing webhooks.json leaves no .bak copy, and a stale webhooks.json.bak is removed at startup.

Names and keys are made up.
"""
import json, os, stat, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_gs1215_helper_degraded as td  # noqa: E402
import test_gs1214_helper_hardening as t14  # noqa: E402

ga = td.tfc.t15.ga
ta = td.ta
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class I1PinnedOnly(td._D):
    def test_unpaired_home_bot_not_marked(self):
        self.two_bots_then_corrupt()
        st, j = self.c.req("DELETE", "/api/bots/oak/key", {"password": ta.GOOD})
        self.assertEqual(st, 200, j)
        self.break_list()
        self.assertIs(self.banner(), True)
        st, j = self.st.call("GET", "/admin/bots")
        rows = {b["slug"]: b for b in j["bots"]}
        self.assertIs(rows["ivy"]["helper_bare"], True)
        self.assertIs(rows["oak"]["helper_bare"], False)


class I2MarkerAcrossRuns(td._D):
    def test_deleted_between_runs_is_damage(self):
        r = self.st.r
        self.st.webhooks.clear()
        self.run_flow(slug="ivy", reply_version=ga.HELPER_RELEASE)
        self.assertIs(self.banner(), False)
        r._helper_reset_state()  # a new relay process
        os.remove(r.HELPER_FILE)
        self.assertIs(self.banner(), True)
        self.assertTrue(self.unconfirmed("ivy"))
        marker = os.path.join(os.path.dirname(r.HELPER_FILE), ".helpers-existed")
        self.assertEqual(os.path.getsize(marker), 0)
        self.assertEqual(stat.S_IMODE(os.stat(marker).st_mode), 0o600)

    def test_fresh_install_is_not_damage(self):
        self.st.r._helper_reset_state()
        self.assertIs(self.banner(), False)


class I3BannerText(unittest.TestCase):
    def test_banner_mentions_failing_update_save(self):
        t = t14.render_banner({"helper_status_unreadable": True, "update_status_unsaved": True})
        if t is None:
            self.skipTest("node not installed")
        self.assertIn("Hide warning can’t hide this", t)
        self.assertNotIn("Hide warning", t14.render_banner({"helper_status_unreadable": True}))


class I4NoWebhookBackup(td._D):
    def test_no_bak_written_and_stale_removed(self):
        r = self.st.r
        self.add_bot("ivy")
        self.assertTrue(r.write_webhooks({"ivy": {"url": self.st.hook_url(), "token": td.tfc.t15.e2e.HOOK,
                                                  "saved_at": 11}}))
        d = r.bots_dir()
        self.assertEqual([n for n in os.listdir(d) if n.endswith(".bak")], [])
        self.assertEqual(stat.S_IMODE(os.stat(r.WEBHOOKS_FILE).st_mode), 0o600)
        stale = os.path.join(d, "webhooks.json.bak")
        with open(stale, "w") as f:
            f.write("{}")
        self.assertEqual(r.drop_stale_backups(), 1)
        self.assertFalse(os.path.exists(stale))
        src = open(os.path.join(ROOT, "relay", "relay.py")).read()
        main = src[src.index('if __name__ == "__main__":'):]
        self.assertIn("drop_stale_backups()", main)  # runs at relay startup
        self.assertNotIn("webhooks.json.bak\")", src.replace('"webhooks.json.bak",)', ""))


if __name__ == "__main__":
    unittest.main()
