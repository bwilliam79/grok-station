"""0.13.46: the post-install grok_admin import runs as grok-admin, isolated.

A failure of that import is an install failure. The installer does not roll back.
"""
import importlib.util
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _health():
    text = open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read()
    start = text.index("st_health() {")
    return text[start:text.index("\n}\n", start)]


def _snippet():
    """The Python the installer runs, taken from install.sh rather than copied here."""
    body = _health()
    mark = "grok_admin_import='"
    start = body.index(mark) + len(mark)
    end = body.index("'\n", start)
    return body[start:end]


def _run(code):
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
        self.assertIn('echo "!!   failed: grok_admin import"', body)
        self.assertIn("bad=1", body)
        self.assertNotIn("chk runuser -u grok-admin", body)
        self.assertNotIn("rollback", body.lower())
        self.assertNotIn("git reset", body)
        # A failure prints the short label. It does not echo the Python source.
        fail = body[body.index('echo "!!   failed: grok_admin import"'):]
        self.assertNotIn("importlib", fail.split("fi", 1)[0])

    def test_the_import_snippet_from_install_sh_runs(self):
        code = _snippet()
        self.assertIn('find_spec("grok_admin")', code)
        missing = _run(code)
        self.assertNotEqual(missing.returncode, 0)
        rooted = code.replace("/opt/grok-station", ROOT)
        good = _run(rooted)
        self.assertEqual(good.returncode, 0, good.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
