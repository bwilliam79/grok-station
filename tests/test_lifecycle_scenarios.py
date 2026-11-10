#!/usr/bin/env python3
"""End-to-end bot Update and Set up scenarios on an installed tree (docs/bot-lifecycle.md).

setUpModule runs tools/install-files.sh (plus install.sh's relay file list) for real into a temporary root
(tests/lifecycle_sim.py). Each scenario then runs tests/lifecycle_driver.py in a fresh process that imports the
admin page, the relay and the wake listener from that installed tree, serves the relay's admin API on loopback,
and drives it through the admin page's own handlers. Every scenario checks what the Bots page shows, what the gate
answers, and that what is sent agrees with what the receiving side checks.

Regressions covered: the main bot one release behind with the same content (0.13.35), an Update with no reply
while the bot's computer is off (0.13.34), an installed tree missing an imported module (0.13.33 gs_render), an
update status with long digit runs (0.13.32), the UTC clear stamp against a local session id (0.13.31), mixed
releases across bots, and Set up through a current main bot with pins equal to the main bot's literals.
0.13.37: Remove then rescan and Dismiss then rescan are net-new rows with nothing left over, a second Set up retires
the first request and its posted key, a Set up for a bot with no stored key location is home (never cloud), and a
cloud-mode request left open by 0.13.36 is retired on upgrade and can't be accepted. GS1337-01: the first
0.13.37 start marks pins of bots that were cloud (explicit key_mode cloud, or no key_mode and no home evidence) as
needing Replace key, keeps pins with home evidence trusted, and logs one audit line per bot.

No network: bot webhooks are a recorder. Needs `unshare -rm` (user and mount namespaces). Without it the module is
skipped, except under GS_RELEASE_CHECK=1 (tools/release-check.sh), where that is a failure.
"""
import json, os, re, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import lifecycle_sim  # noqa: E402

STRICT = os.environ.get("GS_RELEASE_CHECK") == "1"
with open(os.path.join(ROOT, "VERSION")) as _fh:
    REL = _fh.read().strip()
_SIM = {}


def setUpModule():
    if not lifecycle_sim.available():
        msg = "unshare -rm is not available, so the installed-tree scenarios can't run here"
        if STRICT:
            raise AssertionError(msg)
        raise unittest.SkipTest(msg)
    d = tempfile.mkdtemp(prefix="gs-lifecycle-")
    _SIM["dir"] = d
    try:
        _SIM["root"], _SIM["log"] = lifecycle_sim.install(d)
    except lifecycle_sim.Unavailable as e:
        shutil.rmtree(d, ignore_errors=True)
        _SIM.pop("dir", None)
        if STRICT:
            raise AssertionError(str(e))
        raise unittest.SkipTest(str(e))
    _SIM["opt"] = os.path.join(_SIM["root"], "opt", "grok-station")


def tearDownModule():
    if _SIM.get("dir"):
        shutil.rmtree(_SIM["dir"], ignore_errors=True)


def scenario(name):
    work = tempfile.mkdtemp(prefix=name + "-", dir=_SIM["dir"])
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ADMIN_", "GROK_", "RELAY_", "WAKE_"))}
    env.pop("PYTHONPATH", None)
    p = subprocess.run([sys.executable, os.path.join(HERE, "lifecycle_driver.py"), _SIM["root"], work, name],
                       capture_output=True, text=True, timeout=180, cwd=work, env=env)
    if p.returncode != 0:
        raise AssertionError("%s driver failed:\n%s" % (name, (p.stdout + p.stderr)[-3000:]))
    out = json.loads(p.stdout.strip().splitlines()[-1])
    for mod, path in out["loaded"].items():  # nothing came from the repo checkout
        if path:
            assert path.startswith(_SIM["opt"] + os.sep), (mod, path)
    return out


class SimGuards(unittest.TestCase):
    """The install sim refuses real root and stops when a host directory can't be made read-only."""

    def test_refuses_to_run_as_root(self):
        from unittest import mock
        with mock.patch.object(lifecycle_sim.os, "geteuid", lambda: 0), \
                mock.patch.object(lifecycle_sim.subprocess, "run", side_effect=AssertionError("ran")):
            with self.assertRaises(lifecycle_sim.Unavailable):
                lifecycle_sim.install(tempfile.mkdtemp(dir=_SIM["dir"]))

    def test_failed_read_only_remount_stops_the_run(self):
        self.assertNotIn("|| true; fi", lifecycle_sim.RUNNER)
        runner = lifecycle_sim.RUNNER.replace("mount -o remount,bind,ro", "false").replace("STUBS", "")
        out = tempfile.mkdtemp(dir=_SIM["dir"])
        rdir = tempfile.mkdtemp(dir=_SIM["dir"])
        p = subprocess.run(["unshare", "-rm", "sh", "-c", runner, "runner", rdir, ROOT, out, REL, ""],
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(os.listdir(out), [])  # nothing was installed or copied out


class InstalledTree(unittest.TestCase):
    """0.13.33: every module the admin page imports is installed by tools/install-files.sh, and it imports."""

    def _import(self, opt):
        admin = os.path.join(opt, "admin")
        code = ("import sys; sys.path.insert(0, %r); sys.dont_write_bytecode = True\n"
                "import grok_admin, gs_routine, gs_render, bot_content, safe_io, owner_check\n"
                "bad = [m.__name__ for m in (grok_admin, gs_routine, gs_render, bot_content, safe_io, owner_check)"
                " if not m.__file__.startswith(%r)]\nprint('outside' if bad else 'ok', bad)\n" % (admin, opt + os.sep))
        d = tempfile.mkdtemp(dir=_SIM["dir"])
        env = {"PATH": "/usr/bin:/bin", "ADMIN_STATE_DIR": d, "ADMIN_RUN_DIR": d, "ADMIN_HELPER": "/bin/false",
               "ADMIN_VERSION_FILE": os.path.join(opt, "VERSION")}
        return subprocess.run([sys.executable, "-I", "-c", code], cwd=d, env=env, capture_output=True, text=True,
                              timeout=60)

    def test_admin_imports_from_the_installed_tree(self):
        r = self._import(_SIM["opt"])
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertTrue(r.stdout.startswith("ok"), r.stdout)
        with open(os.path.join(_SIM["opt"], "VERSION")) as fh:
            self.assertEqual(fh.read().strip(), REL)

    def test_every_local_import_is_installed(self):
        import test_gs1334_admin_install as t
        opt = _SIM["opt"]
        for name, src in sorted(t.admin_graph().items()):
            hits = [d for d in ("admin", "relay", ".") if os.path.isfile(os.path.join(opt, d, name + ".py"))]
            self.assertTrue(hits, "%s (%s) is imported by the admin page but not in the installed tree" % (name, src))
        for f in lifecycle_sim.relay_files():
            self.assertTrue(os.path.isfile(os.path.join(opt, "relay", f)), f)

    def test_a_0_13_33_style_installer_is_caught(self):
        """Drop gs_render.py from the install line, as 0.13.33 shipped it: the installed admin page can't start."""
        src = tempfile.mkdtemp(dir=_SIM["dir"])
        subprocess.run("git -C %s archive HEAD | tar -x -C %s" % (ROOT, src), shell=True, check=True)
        for rel in ("tools/install-files.sh",):  # the work tree's installer, so an unstaged fix is what is tested
            shutil.copyfile(os.path.join(ROOT, rel), os.path.join(src, rel))
        p = os.path.join(src, "tools", "install-files.sh")
        with open(p) as fh:
            text = fh.read()
        self.assertIn('"$S/admin/gs_render.py" ', text)
        with open(p, "w") as fh:
            fh.write(text.replace('"$S/admin/gs_render.py" ', ""))
        out = tempfile.mkdtemp(dir=_SIM["dir"])
        root, _log = lifecycle_sim.install(out, src=src)
        r = self._import(os.path.join(root, "opt", "grok-station"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("No module named 'gs_render'", r.stderr)


class MainBotBehind(unittest.TestCase):
    """0.13.35: the main bot one release behind with the same content apart from the release line."""

    @classmethod
    def setUpClass(cls):
        cls.out = scenario("main_behind_same_content")

    def test_it_is_the_same_content_case(self):
        self.assertTrue(self.out["prev"])
        self.assertTrue(self.out["same_content_rule"])

    def test_page_shows_update_on_the_main_bot(self):
        page = self.out["page"]
        self.assertEqual(page["states"]["juno"], "needs")  # its row has Update; the callout says Update Juno first
        self.assertEqual(page["states"]["kai"], "done")  # a non-main bot on the same old release is not behind
        self.assertGreaterEqual(page["behind"], 1)  # the Bots-menu dot

    def test_gate_refuses_and_nothing_is_sent(self):
        self.assertEqual((self.out["gate"]["code"], self.out["gate"]["error"]), (409, "Update the main bot first."))
        self.assertEqual(self.out["gate"]["setup_relay_posts"], 0)
        # The relay refuses on its own too, before freezing or posting this release's pins.
        self.assertEqual((self.out["relay_direct"]["code"], self.out["relay_direct"]["error"]),
                         (409, "Update the main bot first."))
        self.assertEqual(self.out["relay_direct"]["posts"], 0)


class MainBotNoStatus(unittest.TestCase):
    """0.13.35: a main bot with no signed status passes the relay only when it was paired on this release."""

    @classmethod
    def setUpClass(cls):
        cls.out = scenario("main_paired_no_status")

    def test_pairing_stamps_this_release(self):
        self.assertEqual((self.out["pin"], self.out["stamp"], self.out["held"]), (200, REL, None))

    def test_relay_follows_the_stamp(self):
        self.assertEqual(self.out["relay_this_release"]["code"], 202, self.out["relay_this_release"])
        self.assertEqual(self.out["relay_this_release"]["posts"], 1)
        for k in ("relay_older_stamp", "relay_no_stamp"):
            self.assertEqual((self.out[k]["code"], self.out[k]["error"], self.out[k]["posts"]),
                             (409, "Update the main bot first.", 0), k)

    def test_page_and_admin_gate_agree(self):
        state = self.out["page"]["states"]["juno"]
        if state == "done":
            self.assertEqual(self.out["gate"]["code"], 200, self.out["gate"])
        else:
            self.assertEqual((self.out["gate"]["code"], self.out["gate"]["error"], self.out["gate"]["setup_relay_posts"]),
                             (409, "Update the main bot first.", 0))


class MainBotCurrent(unittest.TestCase):
    """Set up through a main bot on exactly this release goes ahead, with the pins the main bot holds."""

    @classmethod
    def setUpClass(cls):
        cls.out = scenario("main_current_setup_proceeds")

    def test_page(self):
        self.assertEqual(self.out["page"]["states"]["juno"], "done")
        self.assertEqual(self.out["page"]["release"], REL)

    def test_gate_sends_one_setup_relay(self):
        self.assertEqual(self.out["gate"]["code"], 200, self.out["gate"])
        self.assertEqual(self.out["gate"]["setup_relay_posts"], 1)

    def test_payload_pins_equal_the_main_bots_literals(self):
        p, lit = self.out["payload"], self.out["literals"]
        self.assertEqual((p["persona"], p["target"], p["mode"]), ("juno", "otto", "home"))  # 0.13.37: home only
        self.assertEqual(p["release"], REL)
        self.assertEqual(lit["RELEASE"], REL)
        self.assertEqual(p["routine_commit"], lit["ROUTINE_COMMIT"])
        self.assertEqual(p["routine_sha256"], lit["HASH_ROUTINE_HOME"])
        self.assertNotIn("HASH_ROUTINE_CLOUD", lit)
        self.assertEqual(p["update_commit"], lit["UPDATE_COMMIT"])
        self.assertEqual(p["update_sha256"], lit["HASH_UPDATE"])
        self.assertEqual(p["template_commit"], lit["INSTALL_COMMIT"])
        self.assertEqual(p["template_sha256"], lit["HASH_INSTALL"])
        # The update pin in the main bot's rendered instructions is the same one setup sends.
        self.assertTrue(self.out["main_update_pin"]["in_text"])
        self.assertEqual(self.out["main_update_pin"]["commit"], p["update_commit"])
        self.assertEqual(self.out["main_update_pin"]["sha"], p["update_sha256"])
        st = self.out["station"]
        self.assertEqual((p["station"], p["pin_sha256"], p["station_pub"]), (st["station"], st["pin"], st["pub"]))


class MixedReleases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = scenario("mixed_releases")

    def test_page(self):
        s = self.out["page"]["states"]
        self.assertEqual(s["juno"], "done")
        self.assertEqual(s["kai"], "done")  # older release, same content, not the main bot
        self.assertEqual(s["pip"], "needs")  # older release with different content
        self.assertEqual(self.out["page"]["behind"], sum(1 for v in s.values() if v != "done"))

    def test_gate_and_payload(self):
        self.assertEqual(self.out["gate"]["code"], 200, self.out["gate"])
        self.assertEqual(self.out["gate"]["setup_relay_posts"], 1)


class UpdateNoReply(unittest.TestCase):
    """0.13.34: Update sent while the bot's computer is restarting; no reply; the row ends after 10 minutes."""

    @classmethod
    def setUpClass(cls):
        cls.out = scenario("update_timeout_offline")

    def test_server_side(self):
        s = self.out["steps"]
        self.assertEqual(s["before"], "needs")
        self.assertEqual(s["send"], 202)
        self.assertEqual(s["update_posts"], [REL])  # the update_bot webhook names this release
        self.assertEqual(s["waiting"][0], "waiting")
        self.assertGreater(s["waiting"][1], 590)
        self.assertEqual(s["second_press"][0], 409)
        self.assertEqual(s["after_10_min"], ["noreply", "No reply within 10 minutes.", 0])
        self.assertEqual(s["offline_press"], [502, "Couldn't reach the bot."])
        self.assertEqual(s["offline_row"][0], "noreply")
        self.assertEqual(s["back_online_press"], 202)
        self.assertEqual(set(self.out["windows"].values()), {600})

    def test_page_ends_the_row_on_its_own_clock_with_the_installed_app_js(self):
        import test_gs1334_update_timeout as t
        saved = t.JS
        with open(os.path.join(_SIM["opt"], "admin", "web", "app.js"), encoding="utf-8") as fh:
            t.JS = fh.read()
        try:
            got = t.page_run(r"""
  ANSWERS = [waiting(600), down];
  await refreshFinishChrome(); rowUpdatePoll();
  out.start = snap();
  await advance(601 * 1000);
  out.end = snap();
""")
        finally:
            t.JS = saved
        self.assertIn("Updating…", got["start"]["btn"])
        self.assertEqual(got["end"]["fail"], "Update failed: No reply within 10 minutes.")
        self.assertTrue(got["end"]["sendable"])


class StatusDigits(unittest.TestCase):
    """0.13.32: a signed update status whose hex ids hold a 9-digit run is accepted and shown."""

    @classmethod
    def setUpClass(cls):
        cls.out = scenario("status_lint_digits")

    def test_accepted_recorded_and_shown(self):
        self.assertTrue(self.out["accepted"])
        self.assertTrue(self.out["recorded"])
        self.assertEqual(self.out["page"]["have_release"]["kai"], REL)
        self.assertTrue(self.out["free_text_ssn_blocked"])  # the digit checks still guard free text


class ClearClock(unittest.TestCase):
    """0.13.31: Clear training (admin) and a new session (wake listener) use one UTC clock."""

    @classmethod
    def setUpClass(cls):
        cls.out = scenario("utc_stamps")

    def test_session_after_clear_is_scored(self):
        o = self.out
        self.assertEqual(o["clear_code"], 200)
        self.assertGreater(o["session"], o["cleared"])
        self.assertNotEqual(o["session"], o["local_would_be"])
        self.assertTrue(o["kept"])


def _assert_home_setup(tc, got, lit=None):
    tc.assertEqual((got["code"], got["posts"]), (200, 1), got)
    tc.assertEqual((got["mode"], got["frozen_mode"]), ("home", "home"))
    tc.assertTrue(got["frozen_equals_post"])
    tc.assertFalse(got["has_cloud"])
    if lit:
        tc.assertEqual(got["routine_sha256"], lit["HASH_ROUTINE_HOME"])


def _assert_clean(tc, t):
    tc.assertEqual(t, {"setup_relay": None, "pendings": [], "bot_status": False, "admin_row": None})


class RemoveThenRescan(unittest.TestCase):
    """0.13.37: Remove a home bot that was set up (key posted, waiting), rescan, Set up: net-new and home-mode."""

    @classmethod
    def setUpClass(cls):
        cls.out = scenario("remove_then_rescan")

    def test_first_setup_left_traces(self):
        b = self.out["before"]
        self.assertEqual(self.out["first"]["mode"], "home")
        self.assertEqual(b["setup_relay"]["state"], "review")
        self.assertEqual(len(b["pendings"]), 1)
        self.assertTrue(b["bot_status"])

    def test_remove_forgets_everything_for_the_slug(self):
        self.assertEqual(self.out["remove"], 200)
        _assert_clean(self, self.out["after_remove"])

    def test_rescan_is_net_new(self):
        self.assertEqual(self.out["merge"]["new"], 1)
        self.assertEqual(self.out["imported_state"], "new")
        self.assertEqual(self.out["view"], {})  # no old Sent, review or expired row

    def test_set_up_again_is_home_never_cloud(self):
        _assert_home_setup(self, self.out["again"], self.out["literals"])


class DismissThenRescan(unittest.TestCase):
    """0.13.37: Dismiss a Not set up name with an open Set up, list it again: net-new, the old key can't be accepted."""

    @classmethod
    def setUpClass(cls):
        cls.out = scenario("dismiss_then_rescan")

    def test_dismiss_forgets_the_slug(self):
        self.assertEqual(self.out["before"]["setup_relay"]["state"], "review")
        self.assertEqual(self.out["dismiss"], [200, None])
        _assert_clean(self, self.out["after_dismiss"])

    def test_rescan_is_net_new(self):
        self.assertEqual(self.out["imported_state"], "new")
        self.assertEqual(self.out["view"], {})
        self.assertIsNone(self.out["accept_old"])

    def test_set_up_again_is_home(self):
        _assert_home_setup(self, self.out["again"])


class _KeyMigration:
    """GS1337-01, one bot per case in one station (kai cloud, otto missing + home evidence, pip missing + none)."""
    _out = None

    @classmethod
    def setUpClass(cls):
        if _KeyMigration._out is None:
            _KeyMigration._out = scenario("key_migration")
        cls.out = _KeyMigration._out

    def assertRunOnce(self):
        o = self.out
        self.assertIsNotNone(o["verdicts"], "no key migration on this tree")
        self.assertIsNone(o["second_run"])  # once per station
        self.assertEqual(o["file"]["v"], 1)
        self.assertEqual(o["file"]["release"], REL)

    def audit_for(self, slug):
        return [l for l in self.out["audit"] if l.startswith(slug + " key-migration ")]


class KeyMigrationExplicitCloud(_KeyMigration, unittest.TestCase):
    def test_key_mode_survives_until_the_migration_reads_it(self):
        self.assertEqual(self.out["key_mode_before_migration"], [200, {"juno": "home", "kai": "cloud"}])
        self.assertEqual(self.out["key_mode_after_migration"][0], 200)
        self.assertIsNone(self.out["key_mode_after_migration"][1])

    def test_cloud_pin_needs_replace_key(self):
        o = self.out
        self.assertRunOnce()
        self.assertTrue(o["trusted_before"]["kai"])
        self.assertEqual(o["verdicts"]["kai"], "cloud")
        self.assertFalse(o["trusted_after"]["kai"])
        self.assertEqual(o["listed"]["kai"], "cloud")
        self.assertEqual(o["file"]["needs_replace"]["kai"]["why"], "cloud")
        self.assertEqual(len(self.audit_for("kai")), 1)
        self.assertRegex(self.audit_for("kai")[0], r"^kai key-migration cloud fp=[0-9a-f]{16} mode=cloud evidence=")

    def test_same_key_again_does_not_clear_it_and_a_new_key_does(self):
        o = self.out
        self.assertEqual(o["kai_same_key"], 200)
        self.assertEqual(o["kai_same_key_listed"], "cloud")
        self.assertEqual(o["kai_replace"], 200)
        self.assertTrue(o["trusted_end"]["kai"])
        self.assertIsNone(o["listed_end"]["kai"])
        self.assertTrue(any(l.startswith("kai key-migration cleared was=cloud ") and l.endswith("via=configure")
                            for l in o["audit_end"]), o["audit_end"])

    def test_main_bot_with_home_mode_is_kept(self):
        self.assertEqual(self.out["verdicts"]["juno"], "home")
        self.assertTrue(self.out["trusted_after"]["juno"])


class KeyMigrationMissingWithHomeEvidence(_KeyMigration, unittest.TestCase):
    def test_kept_trusted_with_the_evidence_logged(self):
        o = self.out
        self.assertRunOnce()
        self.assertEqual(o["verdicts"]["otto"], "home-evidence")
        self.assertIn("status-computer", o["evidence"]["otto"])
        self.assertTrue(o["trusted_after"]["otto"])
        self.assertIsNone(o["listed"]["otto"])
        self.assertIn("otto", o["file"]["kept"])
        self.assertEqual(len(self.audit_for("otto")), 1)
        self.assertRegex(self.audit_for("otto")[0], r"^otto key-migration home-evidence fp=[0-9a-f]{16} mode=- evidence=\S*status-computer")
        self.assertTrue(o["trusted_end"]["otto"])


class KeyMigrationMissingNoEvidence(_KeyMigration, unittest.TestCase):
    def test_marked_not_deleted(self):
        o = self.out
        self.assertRunOnce()
        self.assertEqual(o["verdicts"]["pip"], "no-evidence")
        self.assertFalse(o["trusted_after"]["pip"])
        self.assertEqual(o["listed"]["pip"], "no-evidence")
        self.assertEqual(o["file"]["needs_replace"]["pip"]["why"], "no-evidence")
        self.assertEqual(len(self.audit_for("pip")), 1)
        self.assertRegex(self.audit_for("pip")[0], r"^pip key-migration no-evidence fp=[0-9a-f]{16} mode=- evidence=-$")

    def test_set_up_again_clears_it(self):
        o = self.out
        self.assertTrue(o["pip_setup"]["review"])
        self.assertEqual(o["pip_accept"][0], 200, o["pip_accept"])
        self.assertTrue(o["trusted_end"]["pip"])
        self.assertIsNone(o["listed_end"]["pip"])
        self.assertTrue(any(l.startswith("pip key-migration cleared was=no-evidence ") and l.endswith("via=setup")
                            for l in o["audit_end"]), o["audit_end"])


class ReSetupWhilePending(unittest.TestCase):
    """0.13.37: a second Set up while a posted key waits retires the first request and that key."""

    @classmethod
    def setUpClass(cls):
        cls.out = scenario("re_setup_while_pending")

    def test_new_request_replaces_the_old(self):
        o = self.out
        self.assertTrue(o["first"]["review"])
        _assert_home_setup(self, o["second"])
        self.assertTrue(o["rids_differ"])
        self.assertFalse(o["old_pending"])
        self.assertIsNone(o["old_doc"])
        self.assertEqual(len(o["pendings"]), 1)

    def test_old_key_cannot_be_posted_or_accepted(self):
        o = self.out
        self.assertFalse(o["late_post_old"])
        self.assertIsNone(o["accept"])
        pinned, original, posted = o["pinned"]
        self.assertEqual(pinned.replace(" ", ""), original)  # the key pinned before is still the one pinned
        self.assertNotEqual(original, posted)


class StaleCloudRequest(unittest.TestCase):
    """0.13.37 upgrade: a cloud-mode Set up request left open by 0.13.36 is not actionable."""

    @classmethod
    def setUpClass(cls):
        cls.out = scenario("stale_cloud_request")

    def test_retired_at_startup(self):
        o = self.out
        self.assertEqual(o["before"]["public"], "review")
        self.assertEqual(o["retired"], ["otto"])
        self.assertFalse(o["after"]["pending"])
        self.assertEqual(o["after"]["public"], "expired")
        self.assertEqual(o["after"]["view"], {"status": "sent", "relay": "expired"})  # Send request again
        self.assertFalse(o["after"]["pending_pub"])

    def test_cannot_be_accepted_or_posted_to(self):
        o = self.out
        self.assertIsNone(o["accept"])
        self.assertEqual(o["accept_admin"], 404)
        self.assertFalse(o["late_post"])

    def test_refused_when_read_without_a_restart(self):
        self.assertIsNone(self.out["read_kai"])
        self.assertFalse(self.out["kai_file"])

    def test_fresh_set_up_is_home(self):
        _assert_home_setup(self, self.out["fresh"])


if __name__ == "__main__":
    unittest.main()
