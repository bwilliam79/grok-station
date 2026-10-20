"""Relay audit log: monthly files, mode only on create, chmod EPERM still appends.

Import relay inside the tests so collection does not bind its environment first.
"""
import datetime, errno, os, stat, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay"))


def _relay():
    import relay
    return relay


class AuditLog(unittest.TestCase):
    def setUp(self):
        r = _relay()
        self.relay = r
        self.tmp = tempfile.mkdtemp()
        self.snap = {
            "WEBHOOKS_FILE": r.WEBHOOKS_FILE, "AUDIT_MIRROR": r.AUDIT_MIRROR,
            "AUDIT_MIRROR_DIR": r.AUDIT_MIRROR_DIR, "log": r.log,
        }
        bots = os.path.join(self.tmp, "bots")
        os.makedirs(bots)
        r.WEBHOOKS_FILE = os.path.join(bots, "webhooks.json")
        r.AUDIT_MIRROR = os.path.join(self.tmp, "voice-audit", "audit.log")
        r.AUDIT_MIRROR_DIR = ""
        r.log = lambda *_a: None

    def tearDown(self):
        r, s = self.relay, self.snap
        r.WEBHOOKS_FILE, r.AUDIT_MIRROR = s["WEBHOOKS_FILE"], s["AUDIT_MIRROR"]
        r.AUDIT_MIRROR_DIR, r.log = s["AUDIT_MIRROR_DIR"], s["log"]

    def test_chmod_eperm_still_writes_an_existing_file(self):
        r = self.relay
        path = os.path.join(self.tmp, "locked.log")
        with open(path, "w") as f:
            f.write("OLD\n")
        before = stat.S_IMODE(os.stat(path).st_mode)

        def deny(*_a, **_k):
            raise PermissionError(errno.EPERM, "Operation not permitted")

        fresh = os.path.join(self.tmp, "fresh-eperm.log")
        with mock.patch("os.fchmod", deny), mock.patch("os.chmod", deny):
            self.assertTrue(r._append_audit(path, "NEW\n", 0o600))
            self.assertTrue(r._append_audit(fresh, "NEW\n", 0o640))
        self.assertEqual(open(path).read(), "OLD\nNEW\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), before)
        self.assertEqual(open(fresh).read(), "NEW\n")

    def test_mode_set_only_when_this_call_creates_the_file(self):
        r = self.relay
        existing = os.path.join(self.tmp, "existing.log")
        with open(existing, "w") as f:
            f.write("PRIOR\n")
        os.chmod(existing, 0o640)
        self.assertTrue(r._append_audit(existing, "kept\n", 0o600))
        self.assertEqual(stat.S_IMODE(os.stat(existing).st_mode), 0o640)
        self.assertEqual(open(existing).read(), "PRIOR\nkept\n")
        fresh = os.path.join(self.tmp, "fresh.log")
        old = os.umask(0o077)  # would hide the group/other bits of 0o644 if fchmod did not run
        try:
            self.assertTrue(r._append_audit(fresh, "new\n", 0o644))
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(fresh).st_mode), 0o644)
        self.assertEqual(open(fresh).read(), "new\n")

    def test_monthly_names_and_legacy_mirror_log_path(self):
        r = self.relay
        month = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
        name = "audit-" + month + ".log"
        mirror_dir = os.path.join(self.tmp, "mirror")
        os.makedirs(mirror_dir)
        r.AUDIT_MIRROR_DIR = ""
        r.AUDIT_MIRROR = os.path.join(mirror_dir, "audit.log")  # ends in .log: use the directory
        old = os.path.join(self.tmp, "bots", "audit.log")
        with open(old, "w") as f:
            f.write("OLD\n")
        r.audit("admin", "jules", "add", "-")
        primary = r.audit_path()
        self.assertEqual(os.path.basename(primary), name)
        self.assertRegex(os.path.basename(primary), r"^audit-\d{4}-\d{2}\.log$")
        self.assertEqual(os.path.dirname(primary), os.path.join(self.tmp, "bots"))
        mirrored = os.path.join(mirror_dir, name)
        self.assertEqual(os.path.basename(r.audit_mirror_path()), name)
        self.assertEqual(open(mirrored).read(), open(primary).read())
        self.assertRegex(open(primary).read(), r" admin jules add -\n$")
        self.assertFalse(os.path.exists(os.path.join(mirror_dir, "audit.log")))
        self.assertEqual(open(old).read(), "OLD\n")  # no migration, no append onto the old name
        self.assertEqual(stat.S_IMODE(os.stat(primary).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(mirrored).st_mode), 0o644)

        plain = os.path.join(self.tmp, "plain-mirror")
        os.makedirs(plain)
        r.AUDIT_MIRROR = plain  # not a .log path: the value is the directory
        r.audit("admin", "jules", "rename", "-")
        self.assertIn("rename", open(os.path.join(plain, name)).read())
        self.assertNotIn("rename", open(mirrored).read())

        preferred = os.path.join(self.tmp, "preferred")
        os.makedirs(preferred)
        r.AUDIT_MIRROR_DIR = preferred  # preferred over AUDIT_MIRROR
        r.AUDIT_MIRROR = os.path.join(mirror_dir, "audit.log")
        r.audit("admin", "jules", "remove", "-")
        self.assertIn("remove", open(os.path.join(preferred, name)).read())
        self.assertNotIn("remove", open(mirrored).read())
        self.assertNotIn("remove", open(os.path.join(plain, name)).read())

        absent = os.path.join(self.tmp, "no-such-audit-dir")
        r.AUDIT_MIRROR_DIR = absent
        r.audit("admin", "jules", "test", "wxyz")
        self.assertFalse(os.path.exists(absent))
        self.assertIn("test wxyz", open(r.audit_path()).read())


if __name__ == "__main__":
    unittest.main()
