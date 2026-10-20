#!/usr/bin/env python3
"""Single-device installer: dry run works anywhere, prints no secrets, installs every relay module relay.py imports."""
import os, re, subprocess, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Install(unittest.TestCase):
    def test_dry_run(self):
        r = subprocess.run(["sh", os.path.join(ROOT, "install.sh"), "--dry-run"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("grok-station-relay.service", r.stdout)
        self.assertIn("set-key", r.stdout)
        self.assertNotRegex(r.stdout, r"\b[0-9a-f]{48,}\b")  # tokens are generated at run time, never printed
        self.assertNotIn("docker", r.stdout.lower())

    def test_relay_imports_are_installed(self):
        src = open(os.path.join(ROOT, "relay", "relay.py")).read()
        local = {m for m in re.findall(r"^import (\w+)", src, re.M) if os.path.isfile(os.path.join(ROOT, "relay", m + ".py"))}
        line = [l for l in open(os.path.join(ROOT, "install.sh")) if l.startswith("for f in relay.py")][0]
        for m in local:
            self.assertIn(m + ".py", line)

    def test_admin_imports_are_installed(self):
        """The admin page crash-looped on a fresh Pi: pairing, pair_listener and release were never installed."""
        src = open(os.path.join(ROOT, "admin", "grok_admin.py")).read()
        files = open(os.path.join(ROOT, "tools", "install-files.sh")).read()
        mods = set(re.findall(r"^import (\w+)  # noqa: E402", src, re.M))
        self.assertTrue({"pairing", "pair_listener", "release"} <= mods)
        for m in mods:
            local = [d for d in ("admin", "relay") if os.path.isfile(os.path.join(ROOT, d, m + ".py"))]
            self.assertTrue(local, m)
            self.assertIn(f"{local[0]}/{m}.py", files, m)

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
        self.assertNotIn("jarvis", sh.lower())

    def test_relay_unit_is_loopback(self):
        unit = open(os.path.join(ROOT, "systemd", "grok-station-relay.service")).read()
        self.assertIn("RELAY_BIND=127.0.0.1", unit)


if __name__ == "__main__":
    unittest.main()
