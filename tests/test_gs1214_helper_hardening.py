"""0.12.14: Nix's Low items on 0.12.13, through the real handlers and the page's own JS (node).

(1) A seal-verified post with an X-GS-Helper header that is present but not a version is treated like no header:
    the row shows 'Helper version not confirmed: use Update'.
(2) A corrupt helpers.json does not crash and does not silently hide everything: the relay logs one line (no names,
    no contents), the Bots page shows a station-level 'Helper status unreadable' banner, and the next good write
    replaces the file in place (0600, no extra copy).
(3) Once a bot has been bare, a later header alone keeps it 'unconfirmed'; a signed helper version clears it.

Names and keys are made up. Nothing leaves the box.
"""
import json, os, shutil, stat, subprocess, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_gs1212_helper_e2e as e2e  # noqa: E402
import test_gs1213_helper_evidence as t13  # noqa: E402

ga = e2e.ga
UNCONF = t13.UNCONF


def render_banner(data):
    if not shutil.which("node"):
        return None
    prog = e2e._fn("helperStatusBanner") + "process.stdout.write(JSON.stringify(helperStatusBanner(%s)));" % json.dumps(data)
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


class InvalidHeaderIsBare(t13._F):
    def test_invalid_header_reply_shows_unconfirmed(self):
        self.add_bot()
        ask = self.wait_hook()
        st, j = self.reply("ivy", ask["request_id"], "Hi, I'm here.", "not-a-version")
        self.assertEqual(st, 200, j)
        b, notes = self.check("after invalid header")
        self.assertEqual(b["helper"]["state"], "unconfirmed", b["helper"])
        if notes is not None:
            self.assertIn(UNCONF, notes)
        self.assertNotEqual(self.finish_row()["state"], "done")


class BareStaysUntilSigned(t13._F):
    def _bare(self):
        self.add_bot()
        ask = self.wait_hook()
        st, j = self.reply("ivy", ask["request_id"], "Hi, I'm here.", None)
        self.assertEqual(st, 200, j)
        self.assertEqual(self.bot()["helper"]["state"], "unconfirmed")

    def test_header_alone_does_not_clear(self):
        self._bare()
        st, j = self.st.post_voice("/voice/avatar", e2e.helper.build_avatar("ivy", e2e._png(), self.seed,
                                                                            self.station_pub()), ga.HELPER_RELEASE)
        self.assertEqual(st, 200, j)
        b, notes = self.check("after header")
        self.assertEqual(b["helper"]["state"], "unconfirmed", b["helper"])
        if notes is not None:
            self.assertIn(UNCONF, notes)

    def test_signed_version_clears(self):
        self._bare()
        self.ask_update()
        ask = self.wait_hook(kind="update_bot")
        line = "%s instr=%s helper=%s computer=-" % (ask["release"], "0123456789abcdef", ga.HELPER_RELEASE)
        st, j = self.reply("ivy", ask["request_id"], line, ga.HELPER_RELEASE)
        self.assertEqual(st, 200, j)
        b, notes = self.check("after signed status")
        self.assertEqual(b["helper"]["state"], "current", b["helper"])
        if notes is not None:
            self.assertNotIn(UNCONF, notes)


class CorruptHelperFile(t13._F):
    def test_corrupt_file_warns_logs_and_is_replaced(self):
        self.run_flow(reply_version=ga.HELPER_RELEASE)
        path = self.st.r.HELPER_FILE
        with open(path, "w") as f:
            f.write("{not json")
        n0 = len(self.st.lines)
        st, j = self.c.req("GET", "/api/bots")
        self.assertEqual(st, 200, j)
        self.assertIs(j.get("helper_status_unreadable"), True)
        banner = render_banner(j)
        if banner is not None:
            self.assertIn("Helper status unreadable", banner)
        logged = "\n".join(self.st.lines[n0:])
        self.assertIn("helper status file unreadable", logged)
        self.assertNotIn("ivy", logged.lower())
        # The next good write replaces the bad file in place; no extra copies.
        st, j2 = self.st.post_voice("/voice/avatar", e2e.helper.build_avatar("ivy", e2e._png(), self.seed,
                                                                             self.station_pub()), ga.HELPER_RELEASE)
        self.assertEqual(st, 200, j2)
        with open(path) as f:
            self.assertIn("ivy", json.load(f))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if n.startswith("helpers")], ["helpers.json"])
        self.assertIn("helper status file replaced", "\n".join(self.st.lines[n0:]))
        st, j = self.c.req("GET", "/api/bots")
        # 0.12.15 (Nix Low on 0.12.14): a header-only post no longer ends the warning; the banner stays until every
        # bot paired at the time sends a signed version (test_gs1215_helper_recovery covers the rest).
        self.assertIs(j.get("helper_status_unreadable"), True)


if __name__ == "__main__":
    unittest.main()
