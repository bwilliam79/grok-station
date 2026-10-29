"""0.13.7 (Nix M2): the root helper has no `update <sha>` command.

sudoers lets grok-admin run the helper as root with any arguments. `update <40-hex>` downloaded that commit from
GitHub and ran its tools/install-files.sh as root, with no check that the commit was a release. Nothing called it
(Update uses update-start and the fixed grok-station-update unit), so it is gone. Run as the admin page would, it
is now an unknown command: nonzero exit, and nothing is fetched, unpacked, or run.
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "admin", "grok-station-helper")

DRIVER = r'''
import json, runpy, subprocess, sys, tarfile, tempfile, urllib.request
calls = []
def trap(name):
    def f(*a, **k):
        calls.append(name)
        raise RuntimeError("blocked " + name)
    return f
urllib.request.urlopen = trap("urlopen")
subprocess.run = trap("subprocess.run")
subprocess.Popen = trap("subprocess.Popen")
tarfile.open = trap("tarfile.open")
tempfile.mkdtemp = trap("tempfile.mkdtemp")
sys.argv = [sys.argv[1]] + sys.argv[2:]
code = 0
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as e:
    code = e.code if isinstance(e.code, int) else 1
sys.stderr.write("CALLS=" + json.dumps(calls) + "\n")
sys.exit(code)
'''


class HelperUpdateIsGone(unittest.TestCase):
    def _run(self, *args):
        p = subprocess.run([sys.executable, "-c", DRIVER, HELPER] + list(args), capture_output=True, text=True, timeout=30)
        calls = json.loads(p.stderr.rsplit("CALLS=", 1)[1]) if "CALLS=" in p.stderr else None
        return p, calls

    def test_update_sha_is_an_unknown_command(self):
        for sha in ("0" * 40, "931a04b39b48f3735aefed6c4295adf043f0d70e"):
            p, calls = self._run("update", sha)
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(json.loads(p.stdout), {"error": "unknown command"})
            self.assertEqual(calls, [], "nothing may be fetched, unpacked or run")

    def test_no_update_code_left(self):
        src = open(HELPER, encoding="utf-8").read()
        self.assertNotIn("def update(", src)
        self.assertNotIn('cmd == "update"', src)
        self.assertNotIn("codeload.github.com", src)
        self.assertNotRegex(src, r'(?m)^[^#\n]*"[^"\n]*tools/install-files\.sh')  # no root run of a fetched tree


if __name__ == "__main__":
    unittest.main()
