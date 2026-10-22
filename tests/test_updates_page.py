#!/usr/bin/env python3
"""0.1.1 Updates page fixes: version-only Available, sticky-failure handling, hidden Update, versioned history, layout."""
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "grok_admin" not in sys.modules:
    _d = tempfile.mkdtemp(prefix="gs-upd-")
    os.environ["ADMIN_STATE_DIR"] = os.path.join(_d, "state")
    os.environ["ADMIN_RUN_DIR"] = os.path.join(_d, "run")
    os.environ["ADMIN_VERSION_FILE"] = os.path.join(_d, "VERSION")
    os.environ["ADMIN_XAI_KEY_FILE"] = os.path.join(_d, "xai")
    os.makedirs(os.environ["ADMIN_STATE_DIR"])
    os.makedirs(os.environ["ADMIN_RUN_DIR"])
    with open(os.environ["ADMIN_VERSION_FILE"], "w") as fh:
        fh.write("0.1.0\n")
sys.path.insert(0, os.path.join(ROOT, "admin"))
import grok_admin as ga  # noqa: E402

SHA = "dac3d4b0f8a2" + "0" * 28


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as fh:
        return fh.read()


class UpdatesView(unittest.TestCase):
    def setUp(self):
        self._saved = ga.helper, ga.REMOTE_MAIN, ga.VERSION_FILE
        self.state = "idle"
        ga.helper = lambda *a, **k: (True, {"state": self.state}) if a[0] == "update-status" else (True, {"ok": True})
        ga.VERSION_FILE = os.path.join(tempfile.mkdtemp(), "VERSION")
        self._ver("0.1.0")
        for f in ("update-check.json", "update-target.json"):
            try:
                os.remove(ga.path(f))
            except OSError:
                pass

    def tearDown(self):
        ga.helper, ga.REMOTE_MAIN, ga.VERSION_FILE = self._saved

    def _ver(self, v):
        with open(ga.VERSION_FILE, "w") as fh:
            fh.write(v + "\n")

    def test_old_cache_with_commit_id_is_not_shown_as_available(self):
        ga.write_json(ga.path("update-check.json"), {"available": SHA, "error": False, "checked_at": 1})
        v = ga.updates_view()
        self.assertIsNone(v["available"])
        self.assertFalse(v["update_available"])
        self.assertFalse(v["up_to_date"])

    def test_available_is_remote_version_and_up_to_date(self):
        ga.REMOTE_MAIN = lambda: {"version": "0.1.0", "sha": SHA}
        ga.run_update_check()
        v = ga.updates_view()
        self.assertEqual(v["available"], "0.1.0")
        self.assertTrue(v["up_to_date"])
        self.assertFalse(v["update_available"])

    def test_sticky_failure_without_target_is_idle(self):
        """The 0.1.0 install was started by the old page (no target file); its unit result must not read as failed."""
        self.state = "failed"
        self.assertEqual(ga.updates_view()["job"]["state"], "idle")

    def test_failed_unit_but_target_installed_is_success(self):
        ga.REMOTE_MAIN = lambda: {"version": "0.1.1", "sha": SHA}
        ga.run_update_check()
        self.assertEqual(ga.start_update()[0], 202)
        self._ver("0.1.1")
        self.state = "failed"
        job = ga.updates_view()["job"]
        self.assertEqual(job["state"], "succeeded")
        self.assertNotIn("error", job)

    def test_real_failure_names_no_log(self):
        ga.REMOTE_MAIN = lambda: {"version": "0.1.1", "sha": SHA}
        ga.run_update_check()
        ga.start_update()
        self.state = "failed"
        job = ga.updates_view()["job"]
        self.assertEqual(job["state"], "failed")
        self.assertNotIn("below", job["error"])

    def test_history_shows_versions_not_hashes(self):
        rows = ["2026-10-22T20:00:00Z admin station update-check dac3d4b",
                "2026-10-22T20:01:00Z admin station update-start faa73b9",
                "2026-10-22T21:00:00Z admin station update-start 0.1.0 -> 0.1.1",
                "2026-10-22T21:00:00Z admin station update-check-failed network"]
        h = ga.update_history(rows, {"dac3d4b": "0.1.0"})
        self.assertEqual(h[0]["version"], "0.1.0")
        self.assertEqual(h[1]["version"], "earlier build")
        self.assertEqual(h[2]["version"], "0.1.0 → 0.1.1")
        self.assertEqual(h[3]["version"], "network")
        for row in h:
            self.assertIsNone(re.search(r"[0-9a-f]{7}", row["version"]))


class UpdatesPageJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read("admin", "web", "app.js")
        cls.css = _read("admin", "web", "admin.css")

    def test_no_log_or_hash_on_page(self):
        self.assertNotIn("log is below", self.js)
        self.assertNotIn("(up to date)", self.js)
        self.assertNotIn("available_sha", self.js)

    def test_update_button_only_when_available(self):
        self.assertIn('d.update_available ? `<button type="button" class="btn p" id="upd">Update</button>` : ""', self.js)

    def test_history_collapsed_with_chevron_and_keyboard(self):
        self.assertIn("historyCard(d.history, !!el._histOpen)", self.js)
        self.assertIn('aria-expanded="${open ? "true" : "false"}"', self.js)
        self.assertIn('class="chev"', self.js)
        self.assertIn('<button type="button" class="disclose"', self.js)  # native button: Enter/Space
        self.assertIn('id="histbody"${open ? "" : " hidden"}', self.js)

    def test_spacing(self):
        self.assertRegex(self.css, r"\.card\.hist\{margin-top:\d+px\}")
        self.assertRegex(self.css, r"\.updbtns\{margin-bottom:\d+px\}")

    def test_reload_after_self_update(self):
        self.assertIn("location.reload()", self.js)


if __name__ == "__main__":
    unittest.main()
