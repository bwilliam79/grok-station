"""0.13.46: the post-install grok_admin import runs as grok-admin, isolated.

A failure of that import is an install failure. The installer does not roll back.
"""
import importlib.util
import os
import subprocess
import sys
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _health():
    text = open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read()
    start = text.index("st_health() {")
    return text[start:text.index("\n}\n", start)]


def _import_check(roots):
    code = textwrap.dedent("""\
        import importlib.util, sys
        roots = %r
        sys.path = roots + [p for p in sys.path if p and p not in roots]
        spec = importlib.util.find_spec("grok_admin")
        origin = spec.origin if spec is not None else ""
        if not origin.startswith(%r):
            raise SystemExit(1)
        import grok_admin
        """ % (roots, roots[0].rstrip("/") + "/"))
    return subprocess.run(
        ["env", "-i", "-C", "/", "PATH=/usr/bin:/bin", sys.executable, "-I", "-B", "-c", code],
        capture_output=True, text=True, timeout=60)


class InstallImport(unittest.TestCase):
    def test_the_health_check_imports_as_grok_admin_and_a_failure_sets_bad(self):
        body = _health()
        self.assertIn("runuser -u grok-admin -- env -i -C / PATH=/usr/bin:/bin python3 -I -B -c", body)
        self.assertIn('importlib.util.find_spec("grok_admin")', body)
        self.assertIn('sys.path = roots + [p for p in sys.path if p and p not in roots]', body)
        self.assertIn('"/opt/grok-station/admin"', body)
        self.assertIn("chk runuser -u grok-admin", body)
        self.assertIn("bad=1", body)
        self.assertNotIn("rollback", body.lower())
        self.assertNotIn("git reset", body)

    def test_the_import_check_passes_on_the_tree_and_fails_when_the_module_is_missing(self):
        roots = [
            os.path.join(ROOT, "admin"),
            os.path.join(ROOT, "relay"),
            os.path.join(ROOT, "app"),
            ROOT,
        ]
        good = _import_check(roots)
        self.assertEqual(good.returncode, 0, good.stderr[-2000:])
        bad = _import_check(["/tmp/gs1346-missing-admin"])
        self.assertNotEqual(bad.returncode, 0)


if __name__ == "__main__":
    unittest.main()
