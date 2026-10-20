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

    def test_relay_unit_is_loopback(self):
        unit = open(os.path.join(ROOT, "systemd", "grok-station-relay.service")).read()
        self.assertIn("RELAY_BIND=127.0.0.1", unit)


if __name__ == "__main__":
    unittest.main()
