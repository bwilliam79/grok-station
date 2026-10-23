#!/usr/bin/env python3
"""0.8.12: View update log (admin only, secrets hidden) and the updater handling a rewritten main."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "grok_admin" not in sys.modules:
    _d = tempfile.mkdtemp(prefix="gs-ulog-")
    for k, v in (("ADMIN_STATE_DIR", "state"), ("ADMIN_RUN_DIR", "run")):
        os.environ[k] = os.path.join(_d, v)
        os.makedirs(os.environ[k])
    os.environ["ADMIN_VERSION_FILE"] = os.path.join(_d, "VERSION")
    os.environ["ADMIN_XAI_KEY_FILE"] = os.path.join(_d, "xai")
    with open(os.environ["ADMIN_VERSION_FILE"], "w") as fh:
        fh.write("0.1.0\n")
sys.path.insert(0, os.path.join(ROOT, "admin"))
import grok_admin as ga  # noqa: E402


def _helper():
    g = {"__name__": "helper", "__file__": os.path.join(ROOT, "admin", "grok-station-helper")}
    exec(compile(open(g["__file__"]).read(), "helper", "exec"), g)
    return g


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as fh:
        return fh.read()


class Redact(unittest.TestCase):
    def test_hides_secrets(self):
        r = _helper()["redact"]
        for line in ("Authorization: Bearer abcdef123456", "XAI_API_KEY=xai-AAAAAAAAAAAAAAAAAAAAAAAA",
                     "token=ghp_ZZZZZZZZZZZZZZZZZZZZZZZZZZ", "https://user:pw@github.com/x",
                     "password: hunter2hunter2", "seed " + "Q" * 44):
            out = r(line)
            for s in ("abcdef123456", "AAAAAAAAAAAA", "ZZZZZZZZZZ", "user:pw", "hunter2", "Q" * 44):
                self.assertNotIn(s, out, line)

    def test_keeps_plain_lines(self):
        r = _helper()["redact"]
        line = "fatal: Not possible to fast-forward, aborting."
        self.assertEqual(r(line), line)


class Api(unittest.TestCase):
    def setUp(self):
        self._saved = ga.helper

    def tearDown(self):
        ga.helper = self._saved

    def test_log_via_helper(self):
        ga.helper = lambda *a, **k: (True, {"lines": ["a", "b"], "at": 5}) if a[0] == "update-log" else (False, {})
        self.assertEqual(ga.update_log(), {"lines": ["a", "b"], "at": 5})

    def test_helper_failure_is_plain(self):
        ga.helper = lambda *a, **k: (False, {})
        self.assertEqual(ga.update_log()["error"], "Couldn't read the update log.")

    def test_route_is_not_open(self):
        src = _read("admin", "grok_admin.py")
        self.assertIn('"/api/updates/log"', src)
        open_block = src[src.index("open_routes"):src.index("open_routes") + 600]
        self.assertNotIn("/api/updates/log", open_block)

    def test_failure_points_to_log(self):
        ga.helper = lambda *a, **k: (True, {"state": "failed"})
        os.makedirs(os.path.dirname(ga.path("x")), exist_ok=True)
        ga.write_json(ga.path("update-target.json"), {"to": "9.9.9"})
        self.assertIn("View update log", ga.update_job("0.1.0")["error"])
        ga.helper = lambda *a, **k: (True, {"state": "failed", "error": "Couldn't update: moving to the new version failed."})
        self.assertIn("moving to the new version failed", ga.update_job("0.1.0")["error"])


class Ui(unittest.TestCase):
    def test_button(self):
        js = _read("admin", "web", "app.js")
        self.assertIn("View update log", js)
        self.assertIn('"/api/updates/log"', js)


class Bootstrap(unittest.TestCase):
    def test_rewritten_main(self):
        b = _read("bootstrap.sh")
        self.assertNotIn("pull --quiet --ff-only", b)
        i = b.index("merge --quiet --ff-only")
        self.assertLess(b.index("remote get-url origin"), b.index("reset --quiet --hard"))
        self.assertLess(i, b.index("reset --quiet --hard"))
        self.assertNotIn("git clean", b)
        self.assertIn("github.com:bwilliam79/grok-station.git", b)


if __name__ == "__main__":
    unittest.main()
