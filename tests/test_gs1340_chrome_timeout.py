"""0.13.40: a headless Chrome that never exits cannot hang the suite.

run_chrome kills the process group at 20 seconds. A sleeper skips with
"headless Chrome hung after 20s". GS_REQUIRE_CHROME=1 makes that a failure.
Empty stdout skips with "Chrome produced no render", or fails under the same flag.
A script that prints a dump returns at once.

Before this helper, subprocess.run(..., timeout=120) on the same sleeper was still
waiting when an outer `timeout 60` killed it (exit 124).
"""
import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest

import chrome_harness as H


def _script(body):
    d = tempfile.mkdtemp(prefix="gs-chrome-")
    path = os.path.join(d, "chrome")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, stat.S_IRWXU)
    return d, path


def _call(argv):
    box = {}

    def go():
        try:
            box["out"] = H.run_chrome(argv)
        except BaseException as exc:  # SkipTest and AssertionError included
            box["err"] = exc

    th = threading.Thread(target=go)
    t0 = time.monotonic()
    th.start()
    th.join(40)
    return th, time.monotonic() - t0, box


class ChromeTimeout(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("GS_REQUIRE_CHROME", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("GS_REQUIRE_CHROME", None)
        else:
            os.environ["GS_REQUIRE_CHROME"] = self._saved
        if getattr(self, "_dir", None):
            subprocess.run(["rm", "-rf", self._dir], check=False)

    def test_sleeper_skips_in_about_20_seconds_and_is_killed(self):
        self._dir, path = _script("echo $$ > \"$1\"\nsleep 300")
        pidfile = os.path.join(self._dir, "pid")
        th, elapsed, box = _call([path, pidfile, "--headless=new", "--dump-dom", "file:///none"])
        self.assertFalse(th.is_alive(), "helper still running after 40s")
        self.assertIsInstance(box.get("err"), unittest.SkipTest)
        self.assertEqual(str(box["err"]), H.HUNG)
        self.assertGreaterEqual(elapsed, 18)
        self.assertLess(elapsed, 30)
        with open(pidfile, encoding="utf-8") as fh:
            pid = int(fh.read().strip())
        alive = subprocess.run(["kill", "-0", str(pid)], capture_output=True)
        self.assertNotEqual(alive.returncode, 0, "sleeper pid %s still alive" % pid)

    def test_empty_stdout_skips(self):
        self._dir, path = _script("exit 0")
        with self.assertRaises(unittest.SkipTest) as cm:
            H.run_chrome([path, "--dump-dom", "file:///none"])
        self.assertEqual(str(cm.exception), H.EMPTY)

    def test_a_dump_is_returned(self):
        self._dir, path = _script("printf '<pre id=\"out\">ok</pre>\\n'")
        out = H.run_chrome([path, "--dump-dom", "file:///none"])
        self.assertIn("ok", out)

    def test_require_chrome_fails_a_sleeper_and_an_empty_dump(self):
        os.environ["GS_REQUIRE_CHROME"] = "1"
        self._dir, sleeper = _script("sleep 300")
        th, elapsed, box = _call([sleeper, "--headless=new", "--dump-dom", "file:///none"])
        self.assertFalse(th.is_alive(), "helper still running after 40s")
        self.assertIsInstance(box.get("err"), AssertionError)
        self.assertEqual(str(box["err"]), H.HUNG)
        self.assertGreaterEqual(elapsed, 18)
        self.assertLess(elapsed, 30)
        empty_dir, empty = _script("exit 0")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", empty_dir], check=False))
        with self.assertRaises(AssertionError) as cm:
            H.run_chrome([empty, "--dump-dom", "file:///none"])
        self.assertEqual(str(cm.exception), H.EMPTY)


if __name__ == "__main__":
    unittest.main()
