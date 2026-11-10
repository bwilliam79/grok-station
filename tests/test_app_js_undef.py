"""admin/web/app.js must not read a name it does not declare.

0.13.37 deleted GRACE_MAX_S and left the read in stepupText. pageBots threw, so Refresh avatars,
Rescan bots, Add bot and Revoke all bots never got handlers. tests/app_undef.js is the check.
Node is already on the machine; the scanner is the script, not a package.
"""
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "admin", "web", "app.js")
CHECK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_undef.js")


def scan(path):
    return subprocess.run(["node", CHECK, path], capture_output=True, text=True)


class AppJsUndef(unittest.TestCase):
    def test_app_js_declares_every_name_it_reads(self):
        got = scan(JS)
        self.assertEqual(got.returncode, 0, got.stdout or got.stderr)

    def test_a_deleted_binding_is_reported(self):
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            os.write(fd, b"const keep = 1;\nfunction use() { return gone; }\n")
            os.close(fd)
            got = scan(path)
        finally:
            os.unlink(path)
        self.assertEqual(got.returncode, 1)
        self.assertEqual(got.stdout.strip(), "gone")


if __name__ == "__main__":
    unittest.main()
