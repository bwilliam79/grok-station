"""0.13.15 (Nix Info): relay and display units carry the extra sandbox lines; wake and admin do not change."""
import os, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WANT = {"RestrictAddressFamilies": "AF_UNIX AF_INET AF_INET6", "LockPersonality": "yes", "RestrictRealtime": "yes",
        "ProtectKernelTunables": "yes", "ProtectKernelModules": "yes", "ProtectKernelLogs": "yes",
        "ProtectControlGroups": "yes"}


def keys(name):
    out = {}
    with open(os.path.join(ROOT, "systemd", name)) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out.setdefault(k, []).append(v)
    return out


class Hardening(unittest.TestCase):
    def test_relay_and_display(self):
        for unit in ("grok-station-relay.service", "grok-station.service"):
            k = keys(unit)
            for key, val in WANT.items():
                self.assertEqual(k.get(key), [val], (unit, key))

    def test_wake_admin_untouched(self):
        for unit in ("grok-station-wake.service", "grok-station-admin.service"):
            k = keys(unit)
            for key in ("LockPersonality", "RestrictRealtime", "ProtectKernelLogs"):
                self.assertNotIn(key, k, (unit, key))


if __name__ == "__main__":
    unittest.main()
