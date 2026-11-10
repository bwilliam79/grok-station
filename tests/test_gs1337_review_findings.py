#!/usr/bin/env python3
"""0.13.37 regressions for the review of the home-only release (GS1337-01, -02, -03).

01: a pin from a bot that was cloud-mode before 0.13.37 stayed trusted, and key_mode (the only record of it) was
    dropped on the next overlay save. The first 0.13.37 start now marks those pins "needs Replace key" (never
    deletes them), keeps pins with home evidence, and logs one audit line per bot. tests/test_lifecycle_scenarios.py
    has the three cases end to end; this file checks the parts underneath.
02: Dismiss decided "not a bot" from persona_list(), which drops hidden bots and bots whose voice is not offered,
    and an unreadable list counted as "no bots" (fail open). Now known_bot_slugs_or_none() decides and an
    unreadable list skips the forget.
03: Replace key and Accept now name the key they replace (expect_old_fp); the relay answers 409 when the pin
    changed since the page loaded.
04: the preview copies only the files the migration reads. 05: a damaged key check fails closed.
06: the migration read bot_keys.json through botreply.load_keys, which reads a damaged file as no keys, so a
    damaged keys file at the first start finished the migration with nothing marked and every legacy key was
    trusted once the file was restored. It now reads the file strictly.
Keys are made up. Only public verify-key fingerprints appear.
"""
import base64, json, os, re, shutil, subprocess, sys, tempfile, time, unittest

from nacl.signing import SigningKey

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "admin"))
sys.path.insert(0, os.path.join(ROOT, "relay"))
sys.path.insert(0, HERE)
import test_admin  # noqa: E402  (the shared admin sandbox)
import test_bots_api  # noqa: E402

ga = test_admin.ga
APP_JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()


def b64(sk):
    return base64.b64encode(bytes(sk.verify_key)).decode()


def fp16(relay, pub):
    return relay.botreply.key_fingerprint(pub).replace(" ", "")


class _Relay(test_bots_api.BotsApi):
    """The relay admin API on a temp state dir, with the helper, update and pairing files there too."""

    async def asyncSetUp(self):
        await test_bots_api.BotsApi.asyncSetUp(self)
        r = self.relay
        self._files = {k: getattr(r, k) for k in ("HELPER_FILE", "UPDATES_FILE", "PAIRED_FILE")}
        r.HELPER_FILE = os.path.join(self.tmp, "helpers.json")
        r.UPDATES_FILE = os.path.join(self.tmp, "updates.json")
        r.PAIRED_FILE = os.path.join(self.tmp, "paired.json")
        self._hs = dict(r._hs)
        r._hs.clear()
        r._hs.update(r._helper_fresh_state())
        r._helper_file_bad[0] = False
        r._us.update(mem=None, retry_at=None, logged=False)
        if hasattr(r, "_key_move_cache"):  # absent before 0.13.37's fix (the before run)
            self._kmc = dict(r._key_move_cache)
            self.fresh_relay_state()

    async def asyncTearDown(self):
        r = self.relay
        for k, v in self._files.items():
            setattr(r, k, v)
        r._hs.clear()
        r._hs.update(self._hs)
        if hasattr(r, "_key_move_cache"):
            r._key_move_cache.clear()
            r._key_move_cache.update(self._kmc)
        await test_bots_api.BotsApi.asyncTearDown(self)

    def fresh_relay_state(self):
        """What a relay restart starts with: no key-migration state in memory."""
        kmc = self.relay._key_move_cache
        for k in list(kmc):
            kmc[k] = None

    def pin(self, slug, pub):
        self.assertTrue(self.relay.botreply.pin_key(self.relay.bot_keys_path(), slug, pub))

    def overlay(self, **extra):
        doc = {"added": {}, "hidden": [], "renamed": {}, "voices": {}}
        doc.update(extra)
        with open(self.relay.overlay_path(), "w") as fh:
            json.dump(doc, fh)

    def audit(self):
        p = self.relay.audit_path()
        return open(p).read() if os.path.exists(p) else ""


# The BotsApi tests are not rerun here.
for _name in [n for n in dir(test_bots_api.BotsApi) if n.startswith("test_")]:
    setattr(_Relay, _name, None)


class KeyMigration(_Relay):
    """GS1337-01."""

    async def test_marked_pin_is_refused_for_replies_until_replaced(self):
        import botreply
        r = self.relay
        signer = SigningKey.generate()
        self.pin("jules", b64(signer))
        self.overlay(key_mode={"jules": "cloud"})
        self.assertEqual(r.run_key_migration()["jules"]["verdict"], "cloud")
        self.assertIn("jules", botreply.load_keys(r.bot_keys_path())["keys"])  # kept, not deleted
        self.assertFalse(r.bot_paired("jules"))
        self.assertEqual(r.icon_view()["paired"], False)
        helper = self._helper()
        station = botreply.load_or_create_station(r.SECRETS_DIR)
        rid = self.handoff.new_request_id()
        self.handoff.write_pending(r.PENDING_DIR, rid, "jules", "n0", int(time.time() * 1000) + 60000, question="q")
        raw = helper.build_outer("jules", rid, "From the bot computer.", base64.b64encode(bytes(signer)).decode(),
                                 botreply.station_pub_b64(station))
        refused = await self.client.post("/voice/reply", data=raw, headers={"Content-Type": "application/json"})
        self.assertEqual(refused.status, 403)
        listed = await (await self.client.get("/admin/bots", headers=self.headers())).json()
        self.assertEqual(listed["bots"][0]["key_move"], "cloud")
        home = SigningKey.generate()
        put = await self.client.put("/admin/bots/jules/key", headers=self.headers(),
                                    json={"public_key": b64(home), "actor": "admin",
                                          "expect_old_fp": fp16(r, b64(signer))})
        self.assertEqual(put.status, 200, await put.text())
        self.assertTrue(r.bot_paired("jules"))
        listed = await (await self.client.get("/admin/bots", headers=self.headers())).json()
        self.assertIsNone(listed["bots"][0]["key_move"])
        self.assertRegex(self.audit(), r"jules key-migration cleared was=cloud new=[0-9a-f]{16} via=configure")

    async def test_runs_once_and_waits_on_an_unreadable_overlay(self):
        r = self.relay
        self.pin("jules", b64(SigningKey.generate()))
        with open(r.overlay_path(), "w") as fh:
            fh.write("{not json")
        self.assertIsNone(r.run_key_migration())
        self.assertFalse(os.path.exists(r.key_migration_path()))
        self.assertTrue(r.bot_paired("jules"))  # nothing decided yet
        self.overlay()
        self.assertEqual(r.run_key_migration()["jules"]["verdict"], "no-evidence")
        self.assertIsNone(r.run_key_migration())
        self.assertEqual(self.audit().count(" key-migration no-evidence "), 1)

    async def test_home_evidence_fields(self):
        r = self.relay
        for s in ("jules", "otto", "kai", "pip", "sol", "ivy"):
            self.pin(s, b64(SigningKey.generate()))
        self.overlay(key_mode={"jules": "home"})
        with open(r.HELPER_FILE, "w") as fh:
            json.dump({"otto": {"signed": "0.13.36"}, "kai": {"header": "0.13.36"}}, fh)
        with open(r.UPDATES_FILE, "w") as fh:
            json.dump({"pip": {"release": "0.13.36", "instr": "0" * 16, "helper": "-", "computer": "ab" * 8,
                               "at": 1}}, fh)
        with open(r.PAIRED_FILE, "w") as fh:
            json.dump({"sol": "0.13.36", "ivy": "0.13.37"}, fh)
        got = r.run_key_migration(preview=True)
        self.assertFalse(os.path.exists(r.key_migration_path()))  # a preview writes nothing
        self.assertEqual({s: (v["verdict"], v["evidence"]) for s, v in got.items()}, {
            "jules": ("home", ["key_mode"]),
            "otto": ("home-evidence", ["helper-signed"]),
            "kai": ("home-evidence", ["helper-header"]),
            "pip": ("home-evidence", ["status-computer"]),
            "sol": ("no-evidence", []),  # a pre-0.13.37 pairing stamp is not evidence (cloud pins were stamped)
            "ivy": ("home-evidence", ["paired-0.13.37"]),
        })

    async def test_explicit_cloud_beats_home_evidence(self):
        r = self.relay
        self.pin("jules", b64(SigningKey.generate()))
        self.overlay(key_mode={"jules": "cloud"})
        with open(r.HELPER_FILE, "w") as fh:
            json.dump({"jules": {"header": "0.13.36"}}, fh)
        self.assertEqual(r.run_key_migration()["jules"]["verdict"], "cloud")

    async def test_clearing_rules(self):
        r = self.relay
        a, b = b64(SigningKey.generate()), b64(SigningKey.generate())
        self.pin("jules", a)
        self.pin("otto", b)
        self.overlay(key_mode={"jules": "cloud"})
        r.run_key_migration()
        self.assertFalse(r.clear_key_move("jules", r.botreply.key_fingerprint(a), "setup"))  # same key, cloud
        self.assertFalse(r.clear_key_move("otto", r.botreply.key_fingerprint(b), "configure"))  # same key by hand
        self.assertTrue(r.clear_key_move("otto", r.botreply.key_fingerprint(b), "setup"))  # Set up from home
        self.assertTrue(r.bot_paired("otto"))
        self.assertFalse(r.bot_paired("jules"))

    async def test_a_damaged_marks_file_keeps_the_last_good_marks(self):
        r = self.relay
        self.pin("jules", b64(SigningKey.generate()))
        self.overlay()
        r.run_key_migration()
        self.assertFalse(r.bot_paired("jules"))
        with open(r.key_migration_path(), "w") as fh:
            fh.write("{broken")
        self.assertFalse(r.bot_paired("jules"))

    async def test_a_new_key_pinned_another_way_is_not_held_by_the_old_mark(self):
        r = self.relay
        self.pin("jules", b64(SigningKey.generate()))
        self.overlay()
        r.run_key_migration()
        self.pin("jules", b64(SigningKey.generate()))  # the mark names the old fingerprint only
        self.assertTrue(r.bot_paired("jules"))

    async def test_overlay_keeps_key_mode_until_the_migration(self):
        r = self.relay
        self.pin("jules", b64(SigningKey.generate()))  # GS1337-06: a station with bots has a keys file
        self.overlay(key_mode={"jules": "cloud"})
        ov = r.load_overlay_doc()
        self.assertTrue(r.save_overlay(ov) is not False)
        self.assertEqual(json.load(open(r.overlay_path()))["key_mode"], {"jules": "cloud"})
        r.run_key_migration()
        r.save_overlay(r.load_overlay_doc())
        self.assertNotIn("key_mode", json.load(open(r.overlay_path())))

    def test_relay_start_runs_the_migration_first(self):
        src = open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8").read()
        main = src[src.index('if __name__ == "__main__":'):]
        self.assertLess(main.index("key_migration_start()"), main.index("retire"))
        self.assertNotIn("run_key_migration()", main)  # only through key_migration_start, which fails closed

    def test_preview_tool_reads_a_copy(self):
        src = open(os.path.join(ROOT, "tools", "key-migration-preview.py"), encoding="utf-8").read()
        self.assertIn("preview=True", src)
        self.assertIn("mkdtemp", src)


class KeyCheckFailsClosed(_Relay):
    """GS1337-05: a damaged key-migration.json, or a migration that raised or could not run, distrusts every pin
    (like a corrupt bot_keys.json) and says so loudly, instead of trusting marked keys again."""

    def setup_marked(self):
        r = self.relay
        self.home, self.cloud = b64(SigningKey.generate()), b64(SigningKey.generate())
        self.pin("jules", self.home)
        self.pin("otto", self.cloud)
        self.overlay(key_mode={"jules": "home", "otto": "cloud"})
        r.run_key_migration()
        self.assertTrue(r.bot_paired("jules"))
        self.assertFalse(r.bot_paired("otto"))

    def assertAllDistrusted(self):
        r = self.relay
        self.assertFalse(r.bot_paired("jules"))
        self.assertFalse(r.bot_paired("otto"))
        self.assertEqual(r.trusted_keys()["keys"], {})
        self.assertFalse(r.icon_view()["paired"])
        self.assertEqual((r.key_move_for("jules") or {}).get("why"), "damaged")
        self.assertTrue(any("KEY CHECK FAILED CLOSED" in l for l in self.lines), self.lines[-5:])
        self.assertRegex(self.audit(), r"key-migration damaged reason=\S+ all-keys-distrusted")

    async def test_corrupt_json_after_a_restart(self):
        self.setup_marked()
        self.fresh_relay_state()
        with open(self.relay.key_migration_path(), "w") as fh:
            fh.write("{broken")
        self.assertAllDistrusted()
        self.assertFalse(self.relay.clear_key_move("otto", "ab" * 8, "configure"))

    async def test_wrong_shape_after_a_restart(self):
        self.setup_marked()
        self.fresh_relay_state()
        with open(self.relay.key_migration_path(), "w") as fh:
            json.dump({"v": 1, "needs_replace": ["otto"]}, fh)
        self.assertAllDistrusted()

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode 000 file")
    async def test_unreadable_file_after_a_restart(self):
        self.setup_marked()
        self.fresh_relay_state()
        os.chmod(self.relay.key_migration_path(), 0)
        self.addCleanup(os.chmod, self.relay.key_migration_path(), 0o600)
        self.assertAllDistrusted()

    async def test_file_that_vanished_while_running(self):
        self.setup_marked()
        os.unlink(self.relay.key_migration_path())
        self.assertAllDistrusted()

    async def test_migration_exception_at_start(self):
        r = self.relay
        self.pin("jules", b64(SigningKey.generate()))
        self.overlay(key_mode={"jules": "home"})
        real = r.run_key_migration
        r.run_key_migration = lambda preview=False: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertIn("RuntimeError", r.key_migration_start())
        finally:
            r.run_key_migration = real
        self.assertFalse(r.bot_paired("jules"))
        self.assertEqual(r.trusted_keys()["keys"], {})
        self.assertTrue(any("KEY CHECK FAILED CLOSED" in l for l in self.lines))

    async def test_migration_that_could_not_run_at_start(self):
        r = self.relay
        self.pin("jules", b64(SigningKey.generate()))
        with open(r.overlay_path(), "w") as fh:
            fh.write("{not json")
        self.assertIsNotNone(r.key_migration_start())
        self.assertFalse(r.bot_paired("jules"))

    async def test_a_good_start_and_a_repaired_file_trust_again(self):
        r = self.relay
        self.setup_marked()
        with open(r.key_migration_path()) as fh:
            good = fh.read()
        with open(r.key_migration_path(), "w") as fh:
            fh.write("{broken")
        self.assertFalse(r.bot_paired("jules"))
        with open(r.key_migration_path(), "w") as fh:
            fh.write(good)
        self.assertTrue(r.bot_paired("jules"))
        self.assertFalse(r.bot_paired("otto"))  # the real mark still holds
        self.assertTrue(any("readable again" in l for l in self.lines))

    async def test_clean_start_is_not_damaged(self):
        r = self.relay
        self.pin("jules", b64(SigningKey.generate()))
        self.overlay(key_mode={"jules": "home"})
        self.assertIsNone(r.key_migration_start())
        self.assertTrue(r.bot_paired("jules"))
        self.assertIsNone(r.key_migration_start())  # second start: file exists and reads fine
        self.assertTrue(r.bot_paired("jules"))


class KeysFileReadStrictly(_Relay):
    """GS1337-06: the migration reads bot_keys.json strictly. A damaged or missing keys file at the first start
    leaves it not done (nothing written, every key distrusted), so the keys are judged once the file reads again."""

    def three_bots(self):
        """jules home (key_mode), kai cloud (key_mode), pip no key_mode and no evidence."""
        self.keys = {s: b64(SigningKey.generate()) for s in ("jules", "kai", "pip")}
        for slug, pub in self.keys.items():
            self.pin(slug, pub)
        self.overlay(key_mode={"jules": "home", "kai": "cloud"})
        with open(self.relay.bot_keys_path()) as fh:
            self.good = fh.read()

    def assertNotDone(self, damaged):
        r = self.relay
        self.assertIsNotNone(damaged, "the start counted as done")
        self.assertFalse(os.path.lexists(r.key_migration_path()), "key-migration.json was written")
        self.assertEqual(r.trusted_keys()["keys"], {})
        self.assertTrue(any("KEY CHECK FAILED CLOSED" in l for l in self.lines), self.lines[-5:])
        self.assertEqual(r._raw_overlay_key_modes().get("kai"), "cloud")  # key_mode kept for the next start

    async def test_truncated_keys_file_at_first_start(self):
        self.three_bots()
        with open(self.relay.bot_keys_path(), "w") as fh:
            fh.write(self.good[:len(self.good) // 2])
        self.assertNotDone(self.relay.key_migration_start())

    async def test_corrupt_keys_file_at_first_start(self):
        self.three_bots()
        with open(self.relay.bot_keys_path(), "w") as fh:
            fh.write("{not json")
        self.assertNotDone(self.relay.key_migration_start())

    async def test_wrong_shape_keys_file_at_first_start(self):
        self.three_bots()
        with open(self.relay.bot_keys_path(), "w") as fh:
            json.dump({"killed": False, "keys": list(self.keys)}, fh)
        self.assertNotDone(self.relay.key_migration_start())

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode 000 file")
    async def test_unreadable_keys_file_at_first_start(self):
        self.three_bots()
        os.chmod(self.relay.bot_keys_path(), 0)
        self.addCleanup(os.chmod, self.relay.bot_keys_path(), 0o600)
        self.assertNotDone(self.relay.key_migration_start())

    async def test_missing_keys_file_with_existing_bots(self):
        self.three_bots()
        os.unlink(self.relay.bot_keys_path())
        self.assertNotDone(self.relay.key_migration_start())

    async def test_missing_keys_file_with_bots_known_only_from_the_registry(self):
        r = self.relay
        self.assertTrue(os.path.exists(r.PERSONAS_FILE))  # the fixture registry lists jules
        self.assertFalse(os.path.lexists(r.bot_keys_path()))
        self.assertIsNotNone(r.key_migration_start())
        self.assertFalse(os.path.lexists(r.key_migration_path()))

    async def test_missing_keys_file_on_a_fresh_station(self):
        """No keys, no registry, no overlay, no helper, update or pairing records: done, nothing to judge."""
        r = self.relay
        os.unlink(r.PERSONAS_FILE)
        r._personas["mtime"] = None
        for path in (r.bot_keys_path(), r.overlay_path(), r.PAIRED_FILE, r.HELPER_FILE, r.UPDATES_FILE):
            self.assertFalse(os.path.lexists(path), path)
        self.assertIsNone(r.key_migration_start())
        with open(r.key_migration_path()) as fh:
            doc = json.load(fh)
        self.assertEqual((doc["needs_replace"], doc["kept"]), ({}, {}))
        self.assertFalse(any("KEY CHECK FAILED CLOSED" in l for l in self.lines))

    async def test_restored_keys_file_is_still_judged(self):
        """The probe from the review: a truncated keys file at the first start, then the file restored and the
        relay restarted. Every key is judged then; the cloud and no-evidence keys stay distrusted."""
        r = self.relay
        self.three_bots()
        with open(r.bot_keys_path(), "w") as fh:
            fh.write(self.good[:len(self.good) // 2])
        self.assertIsNotNone(r.key_migration_start())
        with open(r.bot_keys_path(), "w") as fh:
            fh.write(self.good)
        self.assertEqual(r.trusted_keys()["keys"], {})  # still this run: nothing trusted until a start judges
        self.fresh_relay_state()  # restart
        self.assertIsNone(r.key_migration_start())
        self.assertEqual(sorted(r.trusted_keys()["keys"]), ["jules"])
        self.assertEqual(r.key_move_for("kai")["why"], "cloud")
        self.assertEqual(r.key_move_for("pip")["why"], "no-evidence")
        self.assertTrue(r.bot_paired("jules"))
        self.assertFalse(r.bot_paired("kai"))
        self.assertFalse(r.bot_paired("pip"))


def _load_preview():
    import importlib.machinery, importlib.util
    path = os.path.join(ROOT, "tools", "key-migration-preview.py")
    loader = importlib.machinery.SourceFileLoader("key_migration_preview", path)
    spec = importlib.util.spec_from_loader("key_migration_preview", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


CANARY = "https://api2.cursor.sh/automations/webhook/CANARY-" + "q" * 20


def _station(root):
    """A station state dir with every kind of file the bots dir and its neighbours hold. Made-up values."""
    keys = {s: b64(SigningKey.generate()) for s in ("juno", "otto", "kai")}
    files = {
        "bots/bot_keys.json": {"killed": False, "keys": keys},
        "bots/personas-overlay.json": {"key_mode": {"juno": "home", "kai": "cloud"}, "added": {}},
        "bots/helpers.json": {"otto": {"header": "0.13.36"}},
        "bots/updates.json": {},
        "bots/paired.json": {"juno": "0.13.36"},
        "bots/webhooks.json": {"juno": {"url": CANARY, "token": "tok-CANARY-" + "z" * 16, "saved_at": 1}},
        "bots/setup-relay.json": {"otto": {"pending_pub": keys["otto"]}},
        "bots/imported.json": {"rows": {}},
        "bots/status.json": {},
        "minted/voice-tokens.json": {"agents": {"juno": "vt_CANARY" + "A" * 40}},
        "personas/personas.json": {"personas": [{"slug": s, "name": s.title(), "voice": "ara", "prompt": "p",
                                                  "aliases": [], "facts": []} for s in ("juno", "otto", "kai")]},
        "secrets/station_key.json": {"seed": "CANARY-station-seed"},
    }
    for rel, doc in files.items():
        os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
        with open(os.path.join(root, rel), "w") as fh:
            json.dump(doc, fh)
    for rel, text in (("bots/audit-2026-11.log", "x\n"), ("bots/station.key", "CANARY-private"),
                      ("bots/webhooks.json.bak", CANARY), ("bots/bot.seed", "CANARY-seed")):
        with open(os.path.join(root, rel), "w") as fh:
            fh.write(text)
    return files


class PreviewCopiesOnlyWhatItReads(unittest.TestCase):
    """GS1337-04: the preview copies an allowlist, never webhooks or secrets, and removes its copy on signals."""

    def setUp(self):
        self.src = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.src, True)
        _station(self.src)

    def test_allowlist_only(self):
        tool = _load_preview()
        dst = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dst, True)
        copied = tool.copy_state(self.src, dst)
        self.assertEqual(sorted(copied), sorted(["bots/bot_keys.json", "bots/personas-overlay.json", "bots/helpers.json",
                                                 "bots/updates.json", "bots/paired.json", "personas/personas.json"]))
        found = sorted(os.path.relpath(os.path.join(d, f), dst) for d, _, fs in os.walk(dst) for f in fs)
        self.assertEqual(found, sorted(copied))
        for d, _, fs in os.walk(dst):
            for f in fs:
                self.assertNotIn("CANARY", open(os.path.join(d, f)).read())
        self.assertNotIn("webhooks.json", " ".join(found))
        self.assertFalse(any(re.search(r"secret|seed|\.key$|token|minted", f) for f in found), found)

    def test_a_full_run_never_holds_a_webhook_or_secret(self):
        """The real tool in a subprocess. Every file in its temp dir is listed just before it is removed."""
        prog = ("import json, os, shutil, sys, runpy\n"
                "seen = []\n"
                "real = shutil.rmtree\n"
                "def snap(p, *a, **k):\n"
                "    for d, _, fs in os.walk(p):\n"
                "        for f in fs:\n"
                "            fp = os.path.join(d, f)\n"
                "            seen.append([os.path.relpath(fp, p), 'CANARY' in open(fp, errors='replace').read()])\n"
                "    return real(p, *a, **k)\n"
                "shutil.rmtree = snap\n"
                "sys.argv = [%r, %r]\n"
                "try:\n"
                "    runpy.run_path(%r, run_name='__main__')\n"
                "except SystemExit as e:\n"
                "    code = e.code\n"
                "sys.stderr.write('SEEN ' + json.dumps(seen) + '\\n')\n") % (
            "key-migration-preview.py", self.src, os.path.join(ROOT, "tools", "key-migration-preview.py"))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GROK_", "RELAY_", "PYTHONPATH"))}
        out = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=60, env=env)
        self.assertIn('"would_mark": ["kai"', out.stdout, out.stderr[-2000:])
        seen = json.loads(out.stderr.rsplit("SEEN ", 1)[1])
        names = [p for p, _ in seen]
        self.assertFalse(any(c for _, c in seen), seen)
        self.assertFalse(any("webhooks" in p or "minted" in p or "seed" in p or p.endswith(".key") for p in names), names)
        self.assertNotIn("CANARY", out.stdout)

    def test_same_verdicts_as_a_full_copy(self):
        """The allowlist is complete: the preview decides the same from it as from a copy of everything."""
        tool = _load_preview()
        code = ("import json, os, sys; sys.path.insert(0, %r); os.environ['GROK_STATE_DIR'] = sys.argv[1];"
                "os.environ['GROK_SECRETS_DIR'] = os.path.join(sys.argv[1], 'secrets-x');"
                "import relay; print(json.dumps(relay.run_key_migration(preview=True), sort_keys=True))"
                % os.path.join(ROOT, "relay"))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GROK_", "RELAY_", "PYTHONPATH"))}
        full = tempfile.mkdtemp()
        part = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, full, True)
        self.addCleanup(shutil.rmtree, part, True)
        shutil.copytree(self.src, full, dirs_exist_ok=True)
        tool.copy_state(self.src, part)
        a = subprocess.run([sys.executable, "-c", code, full], capture_output=True, text=True, timeout=60, env=env)
        b = subprocess.run([sys.executable, "-c", code, part], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(a.returncode, 0, a.stderr[-1500:])
        self.assertEqual(json.loads(a.stdout.splitlines()[-1]), json.loads(b.stdout.splitlines()[-1]))

    def _preview(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GROK_", "RELAY_", "PYTHONPATH"))}
        return subprocess.run([sys.executable, os.path.join(ROOT, "tools", "key-migration-preview.py"), self.src],
                              capture_output=True, text=True, timeout=60, env=env)

    def test_preview_could_not_decide_on_a_truncated_keys_file(self):
        """GS1337-06: never an empty would_mark for a keys file it can't read."""
        p = os.path.join(self.src, "bots", "bot_keys.json")
        good = open(p).read()
        with open(p, "w") as fh:
            fh.write(good[:len(good) // 2])
        out = self._preview()
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr[-1500:])
        self.assertIn("could not decide", out.stdout)
        self.assertNotIn("would_mark", out.stdout)

    def test_preview_could_not_decide_on_a_missing_keys_file_with_bots(self):
        os.unlink(os.path.join(self.src, "bots", "bot_keys.json"))
        out = self._preview()
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr[-1500:])
        self.assertIn("could not decide", out.stdout)
        self.assertNotIn("would_mark", out.stdout)

    def test_temp_dir_is_private_and_removed_on_signals(self):
        import signal as sg
        for sig in (sg.SIGTERM, sg.SIGINT, sg.SIGHUP):
            prog = ("import importlib.machinery, importlib.util, os, signal, sys, time\n"
                    "l = importlib.machinery.SourceFileLoader('p', %r)\n"
                    "s = importlib.util.spec_from_loader('p', l); m = importlib.util.module_from_spec(s); l.exec_module(m)\n"
                    "t = m.make_tmp(); m.install_cleanup(t)\n"
                    "open(os.path.join(t, 'x'), 'w').close()\n"
                    "print(t, oct(os.stat(t).st_mode & 0o777), flush=True)\n"
                    "os.kill(os.getpid(), %d); time.sleep(10)\n") % (os.path.join(ROOT, "tools", "key-migration-preview.py"), sig)
            out = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=30)
            tmp, mode = out.stdout.split()
            self.assertEqual(mode, "0o700")
            self.assertEqual(out.returncode, 128 + sig)
            self.assertFalse(os.path.exists(tmp), tmp)


class DismissGuard(_Relay):
    """GS1337-02."""

    async def dismiss(self, name, forget):
        r = self.relay
        r.dismiss_imported = lambda key: True
        res = await self.client.delete("/admin/bots/imported/" + name, headers=self.headers(),
                                       json={"actor": "admin", "forget": forget})
        return res.status, await res.json()

    async def run_case(self, forget, registry=None, overlay=None):
        r = self.relay
        if registry is not None:
            with open(r.PERSONAS_FILE, "w") as fh:
                fh.write(registry)
            r._personas["mtime"] = None
        if overlay is not None:
            with open(r.overlay_path(), "w") as fh:
                fh.write(overlay)
        called = []
        real = r.forget_bot_traces
        r.forget_bot_traces = lambda slug: called.append(slug)
        try:
            st, j = await self.dismiss(forget, forget)
        finally:
            r.forget_bot_traces = real
        return st, j, called

    async def test_a_hidden_bot_or_one_with_an_unoffered_voice_is_protected(self):
        reg = json.dumps({"personas": [
            {"slug": "jules", "name": "Jules", "voice": "ara", "prompt": "p", "aliases": [], "facts": []},
            {"slug": "quill", "name": "Quill", "voice": "not-offered", "prompt": "p", "aliases": [], "facts": []}]})
        st, j, called = await self.run_case("quill", registry=reg)
        self.assertEqual((st, j.get("forgot"), called), (200, False, []))
        st, j, called = await self.run_case("jules", overlay=json.dumps({"hidden": ["jules"]}))
        self.assertEqual((st, j.get("forgot"), called), (200, False, []))

    async def test_an_unreadable_bot_list_skips_the_forget(self):
        st, j, called = await self.run_case("newbie", registry="{broken")
        self.assertEqual((st, j.get("forgot"), called), (200, False, []))
        self.relay._personas["mtime"] = None
        self._registry()
        st, j, called = await self.run_case("newbie", overlay="[1, 2")
        self.assertEqual((st, j.get("forgot"), called), (200, False, []))

    async def test_a_name_that_is_no_bot_is_forgotten(self):
        st, j, called = await self.run_case("newbie")
        self.assertEqual((st, j.get("forgot"), called), (200, True, ["newbie"]))


class _H(ga.Handler):
    def __init__(self):
        self.out = []

    def send(self, code, body):
        self.out.append((code, body))
        return code

    def need_password(self, j, always=False, session_ok=False):
        return True


def call(method, *args):
    h = _H()
    code = getattr(ga.Handler, method)(h, *args)
    return code, (h.out[-1][1] if h.out else None)


class AdminSide(unittest.TestCase):
    """The admin page forwards expect_old_fp and only clears its own state when the relay forgot (02, 03)."""

    def setUp(self):
        self.calls = []
        self._rc, self._forget = ga.relay_call, ga.forget_bot_admin
        self.forgot = []
        ga.forget_bot_admin = lambda slug: self.forgot.append(slug)
        self.list_reply = (200, {"bots": []})
        self.reply = (200, {"ok": True, "forgot": True})

        def fake(method, path, payload=None, timeout=10):
            self.calls.append((method, path, dict(payload or {})))
            if method == "GET" and path == "/admin/bots":
                return self.list_reply
            return self.reply
        ga.relay_call = fake

    def tearDown(self):
        ga.relay_call, ga.forget_bot_admin = self._rc, self._forget

    def test_dismiss_with_an_unreadable_list_sends_no_forget(self):
        self.list_reply = (500, {"error": "unavailable"})
        self.assertEqual(call("bots_dismiss", "newbie")[0], 200)
        self.assertEqual(self.calls[-1][2]["forget"], "")
        self.assertEqual(self.forgot, [])
        self.list_reply = None
        self.assertEqual(call("bots_dismiss", "newbie")[0], 200)
        self.assertEqual(self.calls[-1][2]["forget"], "")

    def test_dismiss_clears_admin_state_only_when_the_relay_forgot(self):
        self.reply = (200, {"ok": True, "forgot": False})
        call("bots_dismiss", "newbie")
        self.assertEqual(self.calls[-1][2]["forget"], "newbie")
        self.assertEqual(self.forgot, [])
        self.reply = (200, {"ok": True, "forgot": True})
        call("bots_dismiss", "newbie")
        self.assertEqual(self.forgot, ["newbie"])

    def test_key_put_needs_and_forwards_expect_old_fp(self):
        pub = b64(SigningKey.generate())
        self.assertEqual(call("bots_key_put", "jules", {"public_key": pub})[0], 400)
        self.assertEqual(call("bots_key_put", "jules", {"public_key": pub, "expect_old_fp": "nope"})[0], 400)
        n = len(self.calls)
        self.assertEqual(call("bots_key_put", "jules", {"public_key": pub, "expect_old_fp": "ABCD 0123 4567 89ef"})[0], 200)
        self.assertEqual(self.calls[n][2]["expect_old_fp"], "abcd0123456789ef")
        call("bots_key_put", "jules", {"public_key": pub, "expect_old_fp": "-"})
        self.assertEqual(self.calls[-1][2]["expect_old_fp"], "-")

    def test_accept_needs_and_forwards_expect_old_fp_and_409_passes_through(self):
        self.assertEqual(call("bots_setup_pair", {"slug": "otto", "decision": "accept"})[0], 400)
        self.reply = (200, {"ok": True, "state": "accepted"})
        pair = lambda: [c[2] for c in self.calls if c[1] == "/admin/bots/setup-pair"][-1]
        self.assertEqual(call("bots_setup_pair", {"slug": "otto", "decision": "accept", "expect_old_fp": "-"})[0], 200)
        self.assertEqual(pair()["expect_old_fp"], "-")
        self.assertEqual(call("bots_setup_pair", {"slug": "otto", "decision": "reject"})[0], 200)
        self.assertNotIn("expect_old_fp", pair())
        self.reply = (409, {"error": "The paired key changed since this page loaded. Reload and compare the fingerprint again."})
        code, body = call("bots_setup_pair", {"slug": "otto", "decision": "accept", "expect_old_fp": "-"})
        self.assertEqual(code, 409)
        self.assertIn("changed since this page loaded", body["error"])

    def test_key_move_reaches_the_page(self):
        b = ga.safe_bot({"slug": "kai", "name": "Kai", "voice": "ara", "key_move": "cloud"})
        self.assertEqual(b["key_move"], "cloud")
        self.assertIsNone(ga.safe_bot({"slug": "kai", "name": "Kai", "voice": "ara", "key_move": "x"})["key_move"])


class ExpectOldFpRelay(_Relay):
    """GS1337-03 at the relay."""

    async def put(self, pub, expect):
        body = {"public_key": pub, "actor": "admin"}
        if expect is not None:
            body["expect_old_fp"] = expect
        res = await self.client.put("/admin/bots/jules/key", headers=self.headers(), json=body)
        return res.status, await res.json()

    async def test_key_put(self):
        r = self.relay
        a, b, c = (b64(SigningKey.generate()) for _ in range(3))
        self.assertEqual((await self.put(a, None))[0], 400)
        self.assertEqual((await self.put(a, "-"))[0], 200)
        st, j = await self.put(b, "-")  # the page loaded before a was pinned
        self.assertEqual((st, j["error"]), (409, r.KEY_CHANGED))
        st, j = await self.put(b, fp16(r, c))
        self.assertEqual(st, 409)
        self.assertEqual(r.pinned_fingerprint("jules"), r.botreply.key_fingerprint(a))  # untouched
        self.assertEqual((await self.put(b, fp16(r, a)))[0], 200)
        self.assertEqual(r.pinned_fingerprint("jules"), r.botreply.key_fingerprint(b))

    async def test_accept(self):
        r = self.relay
        a, posted = b64(SigningKey.generate()), b64(SigningKey.generate())
        self.pin("jules", a)
        seen = []

        def decide(target, decision, actor="admin", expect_old_fp=None):
            seen.append(expect_old_fp)
            if decision == "accept" and not r.expect_old_ok(target, expect_old_fp):
                return {"ok": False, "changed": True}
            return {"ok": True, "state": decision + "ed"}
        real = r.setup_pair_decide
        r.setup_pair_decide = decide
        try:
            post = lambda body: self.client.post("/admin/bots/setup-pair", headers=self.headers(),
                                                 json=dict(body, actor="admin", slug="jules"))
            self.assertEqual((await post({"decision": "accept"})).status, 400)
            stale = await post({"decision": "accept", "expect_old_fp": "-"})
            self.assertEqual((stale.status, (await stale.json())["error"]), (409, r.KEY_CHANGED))
            ok = await post({"decision": "accept", "expect_old_fp": fp16(r, a)})
            self.assertEqual(ok.status, 200)
            self.assertEqual((await post({"decision": "reject"})).status, 200)
        finally:
            r.setup_pair_decide = real
        self.assertEqual(seen, ["-", fp16(r, a), None])

    async def test_decide_itself_checks_before_pinning(self):
        r = self.relay
        a = b64(SigningKey.generate())
        self.pin("jules", a)
        src = open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8").read()
        body = src[src.index("def setup_pair_decide("):]
        body = body[:body.index("\ndef ")]
        self.assertLess(body.index("expect_old_ok"), body.index("pin_key"))


class Page(unittest.TestCase):
    """GS1337-03 on the page, and the key_move row text (01)."""

    def test_every_key_pin_names_the_key_it_replaces(self):
        puts = [m.start() for m in re.finditer(r'api\("PUT", (`/api/bots/\$\{encodeURIComponent\([^)]*\)\}/key`'
                                                r'|"/api/bots/" \+ encodeURIComponent\([^)]*\) \+ "/key")', APP_JS)]
        self.assertEqual(len(puts), 3)  # Configure, the pasted-reply form, Add bot
        for at in puts:
            window = APP_JS[max(0, at - 400):at]
            self.assertIn("expect_old_fp: expectOldFp(", window)
        dec = APP_JS[APP_JS.index("function decideSetupPair("):]
        dec = dec[:dec.index("\n}\n")]
        self.assertIn('if (decision === "accept") body.expect_old_fp = expectOldFp(', dec)

    @unittest.skipUnless(shutil.which("node"), "node")
    def test_expect_old_fp_and_key_move_text(self):
        def fn(name):
            i = APP_JS.index("function " + name + "(")
            return APP_JS[i:APP_JS.index("\n}\n", i) + 3]
        pub = b64(SigningKey.generate())
        prog = ("const {createHash}=require('crypto');"
                "function sha256Hex(b){return createHash('sha256').update(Buffer.from(b)).digest('hex');}"
                + fn("pubKeyOk") + fn("keyFingerprint") + fn("expectOldFp") + fn("botBySlug") + fn("keyMoveText")
                + "const el={_bots:[{slug:'kai',paired:true,public_key:%s}]};" % json.dumps(pub)
                + "process.stdout.write(JSON.stringify([expectOldFp(botBySlug(el,'kai')), expectOldFp(botBySlug(el,'x')),"
                + " expectOldFp({paired:false,public_key:%s}), keyMoveText({key_move:'cloud',name:'Juniper Hollow'}),"
                  " keyMoveText({key_move:'no-evidence',slug:'kai',name:'Kai'}), keyMoveText({}),"
                  " keyMoveText({key_move:'damaged',name:'Kai'})]));" % json.dumps(pub))
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        import botreply
        self.assertEqual(got[0], botreply.key_fingerprint(pub).replace(" ", ""))
        self.assertEqual(got[1:3], ["-", "-"])
        # One line for both cases; the row draws the warning glyph, so the text has none.
        self.assertEqual(got[3], "Replies refused: set Juniper Hollow up again on its home computer")
        self.assertEqual(got[4], "Replies refused: set Kai up again on its home computer")
        self.assertEqual(got[5], "")
        self.assertEqual(got[6], "Replies refused for all bots: the station's key check file is damaged. "
                                 "Setting up again won't fix it.")

    def test_setup_row_copy(self):
        # The glyph and the first word are joined by a no-break space (U+00A0), so they never wrap apart.
        # "Set up again" is joined by no-break spaces too, so "again" never lands alone on a line.
        self.assertIn('"\\u26a0\\u00a0Key is on the bot\'s computer \\u00b7 Set\\u00a0up\\u00a0again"', APP_JS)
        self.assertIn('"\\u26a0\\u00a0Key location not confirmed \\u00b7 Set\\u00a0up\\u00a0again"', APP_JS)
        self.assertNotIn("\\u00b7 Set up again", APP_JS)
        self.assertNotIn("Set up to move it home", APP_JS)
        self.assertIn('"Replies from " + bot + " are refused until it is set up again on its home computer."', APP_JS)

    def test_key_move_note_wraps_like_a_failure_line(self):
        self.assertIn("note === rowFailLine(b) || note === keyMoveText(b)", APP_JS)

    def test_damaged_note_shows_on_rows_with_a_set_up_row(self):
        """Only a cloud or no-evidence mark moves the note to the Set up row; a damaged key check has no Set up row
        text, so a bot with a Set up row must still show the damaged note."""
        self.assertIn("const note = su0 && su0.key_move && setupBits(su0) ? \"\" : finishNotes(b);", APP_JS)
        build = APP_JS[APP_JS.index("const keyMove = (b.key_move === \"cloud\""):][:200]
        self.assertIn('(b.key_move === "cloud" || b.key_move === "no-evidence") ? b.key_move : ""', build)


if __name__ == "__main__":
    unittest.main()
