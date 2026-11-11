"""0.13.41: a Chrome run, including one that is killed, leaves no new temp dirs."""
import os
import stat
import subprocess
import tempfile
import unittest

import chrome_harness as H

LEAK_PREFIXES = ("scoped_dir", ".org.chromium", ".com.google.Chrome", "com.google.Chrome", "gs-chrome-run-")


def _script(body):
    d = tempfile.mkdtemp(prefix="gs-chrome-case-")
    path = os.path.join(d, "chrome")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, stat.S_IRWXU)
    return d, path


def _leaks(before):
    now = set(os.listdir("/tmp"))
    return [name for name in sorted(now - before) if name.startswith(LEAK_PREFIXES)]


class ChromeTemp(unittest.TestCase):
    def test_a_finished_run_and_a_killed_run_leave_no_temp_dirs(self):
        before = set(os.listdir("/tmp"))
        # A real Chrome writes scoped_dir under TMPDIR. The stand-in does the same,
        # so a harness that leaves TMPDIR as /tmp fails this test.
        write = 'd="${TMPDIR:-/tmp}"; mkdir -p "$d/scoped_dir$$"\n'
        dump_dir, dump = _script(write + "printf '<html>ok</html>\\n'")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", dump_dir], check=False))
        out = H.run_chrome([dump, "--dump-dom", "file:///none"])
        self.assertIn("ok", out)
        self.assertEqual(_leaks(before), [])
        sleep_dir, sleeper = _script(write + "sleep 30")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", sleep_dir], check=False))
        with self.assertRaises(unittest.SkipTest):
            H.run_chrome([sleeper, "--dump-dom", "file:///none"], timeout=1)
        self.assertEqual(_leaks(before), [])


if __name__ == "__main__":
    unittest.main()
