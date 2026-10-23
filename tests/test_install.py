#!/usr/bin/env python3
"""Single-device installer: dry run works anywhere, prints no secrets, installs every relay module relay.py imports."""
import os, re, subprocess, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Install(unittest.TestCase):
    def test_dry_run(self):
        r = subprocess.run(["sh", os.path.join(ROOT, "install.sh"), "--dry-run", "--verbose"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("grok-station-relay.service", r.stdout)
        self.assertIn("set-key", r.stdout)
        self.assertNotRegex(r.stdout, r"\b[0-9a-f]{48,}\b")  # tokens are generated at run time, never printed
        self.assertNotIn("docker", r.stdout.lower())

    def test_quiet_stages_and_failure(self):
        r = subprocess.run(["sh", os.path.join(ROOT, "install.sh"), "--dry-run"], capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, NO_COLOR="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("✓ Checking health", r.stdout)
        self.assertNotIn("+ ", r.stdout)  # command lines go to the log unless --verbose
        self.assertNotIn("\033", r.stdout)
        # a failing stage stops the install with the stage name, the log tail and the log path
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            src = open(os.path.join(ROOT, "install.sh")).read().replace("st_display() {", "st_display() {\n  echo boom-marker; false", 1)
            f = os.path.join(d, "install.sh"); open(f, "w").write(src)
            r = subprocess.run(["sh", f, "--dry-run"], capture_output=True, text=True, timeout=30, env=dict(os.environ, NO_COLOR="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("✗ Installing the display", r.stdout)
        self.assertIn("boom-marker", r.stdout)
        self.assertNotIn("Installing voice relay", r.stdout)

    def test_bootstrap(self):
        b = open(os.path.join(ROOT, "bootstrap.sh")).read()
        self.assertIn("https://github.com/bwilliam79/grok-station", b)
        self.assertIn('exec sh "$DIR/install.sh"', b)
        self.assertTrue(b.rstrip().endswith('main "$@"'))  # whole script parsed before anything runs (piped to sh)
        self.assertNotRegex(b, r"\bread\b")  # no prompts: stdin is the script itself
        self.assertEqual(subprocess.run(["sh", "-n", os.path.join(ROOT, "bootstrap.sh")]).returncode, 0)

    def test_relay_imports_are_installed(self):
        src = open(os.path.join(ROOT, "relay", "relay.py")).read()
        local = {m for m in re.findall(r"^import (\w+)", src, re.M) if os.path.isfile(os.path.join(ROOT, "relay", m + ".py"))}
        line = [l for l in open(os.path.join(ROOT, "install.sh")) if l.startswith("for f in relay.py")][0]
        for m in local:
            self.assertIn(m + ".py", line)

    def test_admin_imports_are_installed(self):
        """Every repo module the admin page imports is installed (a missing one crash-looped the page on a fresh Pi).
        Relay pairing and the release-manifest updater are gone, and nothing may import them."""
        src = open(os.path.join(ROOT, "admin", "grok_admin.py")).read()
        files = open(os.path.join(ROOT, "tools", "install-files.sh")).read()
        mods = set(re.findall(r"^(?:import|from) (\w+)", src, re.M))
        self.assertFalse({"pairing", "pair_listener", "release"} & mods)
        for name in ("admin/pairing.py", "admin/pair_listener.py", "relay/release.py"):
            self.assertFalse(os.path.exists(os.path.join(ROOT, name)), name)
        for m in mods:
            local = [d for d in ("admin", "relay", "app") if os.path.isfile(os.path.join(ROOT, d, m + ".py"))]
            if local:
                self.assertIn(f"{local[0]}/{m}.py", files, m)

    def test_update_unit_is_fixed(self):
        unit = open(os.path.join(ROOT, "systemd", "grok-station-update.service")).read()
        self.assertIn("Type=oneshot", unit)
        self.assertIn("ExecStart=/usr/local/lib/grok-station/grok-station-update\n", unit)
        helper = open(os.path.join(ROOT, "admin", "grok-station-helper")).read()
        self.assertIn('"systemctl", "start", "--no-block", UPDATE_UNIT', helper)
        self.assertIn('UPDATE_UNIT = "grok-station-update.service"', helper)
        sh = open(os.path.join(ROOT, "install.sh")).read()
        self.assertIn("/etc/grok-station/source-dir", sh)
        self.assertIn("systemd/grok-station-update.service", sh)
        upd = open(os.path.join(ROOT, "tools", "grok-station-update")).read()
        self.assertIn('exec sh "$SRC/bootstrap.sh"', upd)

    def test_post_install_check(self):
        sh = open(os.path.join(ROOT, "install.sh")).read()
        for u in ("grok-station-relay", "grok-station-admin", "grok-station ", "grok-station-wake"):
            self.assertIn(u, sh.split("Post-install check", 1)[1])
        self.assertIn("https://127.0.0.1/", sh)
        self.assertIn("journalctl -u grok-station-admin", sh)

    def test_pi3_audio_and_permissions(self):
        sh = open(os.path.join(ROOT, "install.sh")).read()
        self.assertRegex(sh, r"usermod -aG grok-admin,audio grok-admin")  # admin page lists sound cards
        self.assertIn("$O/portraits", sh)  # wake listener writes the persona cache (sync_personas PermissionError)
        admin = open(os.path.join(ROOT, "systemd", "grok-station-admin.service")).read()
        self.assertIn("SupplementaryGroups=adm audio", admin)
        wake = open(os.path.join(ROOT, "systemd", "grok-station-wake.service")).read()
        self.assertNotIn("CPUQuota", wake)  # the quota starved arecord on a Pi 3B (overruns)
        src = open(os.path.join(ROOT, "wake", "grok_wake.py")).read()
        self.assertIn("--buffer-time=", src); self.assertIn("--period-time=", src)
        self.assertIn('download_models(["feature-models-only"])', sh)
        self.assertIn("wake/models/hey_grok.onnx", sh)
        self.assertIn("$V/voice/ack", sh)  # cached "Asking <name>" clips; the relay unit already writes the state dir
        self.assertNotIn("jarvis", sh.lower())

    def test_fresh_install_reboots_update_does_not(self):
        env = dict(os.environ, NO_COLOR="1")
        env.pop("GROK_STATION_NONINTERACTIVE", None)
        for extra in ([], ["--no-reboot"]):
            r = subprocess.run(["sh", os.path.join(ROOT, "install.sh"), "--dry-run", *extra], capture_output=True, text=True,
                               timeout=30, env=env, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("After the reboot, open on a phone or laptop", r.stdout)
            self.assertIn("Reboot to finish: sudo reboot", r.stdout)  # dry run never reboots or prompts
            self.assertNotIn("Press Enter", r.stdout)
        r = subprocess.run(["sh", os.path.join(ROOT, "install.sh"), "--dry-run"], capture_output=True, text=True, timeout=30,
                           env=dict(env, GROK_STATION_NONINTERACTIVE="1"), stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("reboot", r.stdout.lower().replace("reboot-required", ""))
        sh = open(os.path.join(ROOT, "install.sh")).read()
        self.assertIn("--no-reboot", sh)
        self.assertIn("read -r _ < /dev/tty", sh)
        self.assertIn("GROK_STATION_NO_RESTART=1", sh)
        files = open(os.path.join(ROOT, "tools", "install-files.sh")).read()
        self.assertIn('if [ "${GROK_STATION_NO_RESTART:-0}" != 1 ]', files)
        self.assertIn("GROK_STATION_NONINTERACTIVE=1", open(os.path.join(ROOT, "tools", "grok-station-update")).read())

    def test_wifi_powersave_journal_cap_and_nacl(self):
        sh = open(os.path.join(ROOT, "install.sh")).read()
        self.assertIn("[ -d /etc/NetworkManager/conf.d ]", sh)
        self.assertIn("/etc/NetworkManager/conf.d/wifi-powersave-off.conf", sh)
        self.assertIn("[connection]", sh)
        self.assertIn("wifi.powersave = 2", sh)
        self.assertIn("/var/log/journal", sh)
        self.assertIn("/etc/systemd/journald.conf.d/size.conf", sh)
        self.assertIn("[Journal]", sh)
        self.assertIn("SystemMaxUse=200M", sh)
        self.assertIn("import aiohttp, nacl", sh)

    def test_relay_unit_is_loopback(self):
        unit = open(os.path.join(ROOT, "systemd", "grok-station-relay.service")).read()
        self.assertIn("RELAY_BIND=127.0.0.1", unit)


if __name__ == "__main__":
    unittest.main()
