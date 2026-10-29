#!/usr/bin/env python3
"""Update gate and session file, without opening a socket (the HTTP cases live in test_admin.py)."""
import os
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_admin  # noqa: E402  (the one shared admin sandbox; see test_gs137_admin_import_guard)
sys.path.insert(0, os.path.join(ROOT, "admin"))
ga = test_admin.ga

SHA = "4915427d34633848f41448c563eb0543c3bce44a"


class Flow(unittest.TestCase):
    def setUp(self):
        self._helper, self._remote, self._ver = ga.helper, ga.REMOTE_MAIN, ga.VERSION_FILE
        self.ver_path = os.path.join(tempfile.mkdtemp(prefix="gs-verfile-"), "VERSION")
        ga.VERSION_FILE = self.ver_path
        self.calls = []

        def fake(*a, **k):
            self.calls.append(a)
            if a[0] == "update-status":
                return True, {"state": "idle", "log": ["should-not-reach-the-page"]}
            if a[0] == "update-start":
                return True, {"ok": True}
            return True, {"ok": True}

        ga.helper = fake
        with open(ga.VERSION_FILE, "w") as fh:
            fh.write("0.1.0\n")

    def tearDown(self):
        ga.helper, ga.REMOTE_MAIN, ga.VERSION_FILE = self._helper, self._remote, self._ver

    def _at(self, version):
        ga.REMOTE_MAIN = lambda: {"version": version, "sha": SHA}

    def test_check_apply_and_refusals(self):
        self._at("0.1.1")
        ga.run_update_check()
        view = ga.updates_view()
        self.assertEqual(view["installed"], "0.1.0")
        self.assertEqual(view["available"], "0.1.1")
        self.assertEqual(view["available_sha"], SHA[:7])
        self.assertTrue(view["update_available"])
        self.assertIsNone(view["update_blocked"])
        self.assertNotIn("log", view["job"])
        self.assertTrue(any(line["event"] == "update-check" and line["version"] == "0.1.1" for line in view["history"]))
        code, body = ga.start_update()
        self.assertEqual(code, 202, body)
        self.assertIn(("update-start",), self.calls)
        self.assertFalse(any(a[0] == "update-start" and len(a) > 1 for a in self.calls))

        self._at("0.1.0")
        ga.run_update_check()
        code, body = ga.start_update()
        self.assertEqual(code, 409, body)
        self.assertIn("Already up to date", body["error"])
        view = ga.updates_view()
        self.assertFalse(view["update_available"])
        self.assertIsNone(view["update_blocked"])

        with open(ga.VERSION_FILE, "w") as fh:
            fh.write("0.1.2\n")
        self._at("0.1.0")
        ga.run_update_check()
        code, body = ga.start_update()
        self.assertEqual(code, 409, body)
        self.assertIn("Refusing to install 0.1.0 over 0.1.2", body["error"])

        os.remove(ga.path("update-check.json"))
        self.calls.clear()
        code, body = ga.start_update()
        self.assertEqual((code, body["error"]), (409, "Check for updates first."))
        self.assertFalse(any(a[0] == "update-start" for a in self.calls))

        def boom():
            raise OSError("no network")

        ga.REMOTE_MAIN = boom
        ga.run_update_check()
        view = ga.updates_view()
        self.assertTrue(view["cant_check"])
        self.assertIsNone(view["available"])
        self.assertTrue(any(line["event"] == "update-check-failed" and line["version"] == "network" for line in view["history"]))

        with open(ga.VERSION_FILE, "w") as fh:
            fh.write("4915427\n")
        self._at("0.1.0")
        ga.run_update_check()
        self.assertEqual(ga.start_update()[0], 202)

        with open(ga.VERSION_FILE, "w") as fh:
            fh.write("0.1.0\n")
        ga.REMOTE_MAIN = lambda: {"version": "", "sha": SHA}
        ga.run_update_check()
        view = ga.updates_view()
        self.assertIn("Refusing", view["update_blocked"])
        code, body = ga.start_update()
        self.assertEqual(code, 409)
        self.assertIn("Refusing", body["error"])

        ga.REMOTE_MAIN = None  # helper answer is not a version
        ga.run_update_check()
        self.assertTrue(ga.updates_view()["cant_check"])

    def test_session_file_round_trip(self):
        ga.write_json(ga.path("auth.json"), {"password": ga.hash_password("kitchen table lamp 42")})
        ga.AUTH.__init__()
        self.assertFalse(ga.AUTH.setup_required())
        cookie = ga.AUTH.new_session()
        raw = open(ga.path("sessions.json")).read()
        self.assertNotIn(cookie, raw)
        self.assertEqual(stat.S_IMODE(os.stat(ga.path("sessions.json")).st_mode), 0o600)
        self.assertTrue(ga.AUTH.valid(cookie))
        self.assertIsNotNone(ga.AUTH.open_stepup(cookie))
        blob = ga.read_json(ga.path("sessions.json"), {})
        t = ga.now()
        info = []
        if isinstance(blob, dict):
            for key, rec in blob.items():
                exp = rec.get("exp") if isinstance(rec, dict) else None
                seen = rec.get("seen") if isinstance(rec, dict) else None
                info.append((type(key).__name__, len(key) if isinstance(key, str) else None,
                             ga._SESSION_KEY.fullmatch(key) is not None if isinstance(key, str) else False,
                             type(exp).__name__, type(seen).__name__,
                             None if not isinstance(exp, (int, float)) else exp < t,
                             None if not isinstance(seen, (int, float)) else t - seen))
        ga.AUTH.__init__()
        self.assertTrue(ga.AUTH.valid(cookie), (getattr(ga.AUTH, "_load_note", None), len(ga.AUTH.sessions), info))
        self.assertTrue(ga.AUTH.stepup_open(cookie))
        ga.AUTH.logout(cookie)
        ga.AUTH.__init__()
        self.assertFalse(ga.AUTH.valid(cookie))

        cookie = ga.AUTH.new_session()
        base = ga.now
        try:
            ga.now = lambda: base() + ga.IDLE_TTL + 5
            self.assertFalse(ga.AUTH.valid(cookie))
            ga.now = base
            ga.AUTH.__init__()
            self.assertFalse(ga.AUTH.valid(cookie))
        finally:
            ga.now = base

        cookie = ga.AUTH.new_session()
        ga.AUTH.setup_tickets["ticket"] = ga.now() + 100
        ga.write_json(ga.path("auth.json"), {"reset_at": 1})
        self.assertTrue(ga.AUTH.setup_required())
        self.assertFalse(ga.AUTH.valid(cookie))
        self.assertIsNone(ga.AUTH.set_first_password("ticket", "after reset pass 9"))
        self.assertFalse(ga.AUTH.valid(cookie))
        ga.AUTH.__init__()
        self.assertFalse(ga.AUTH.valid(cookie))


if __name__ == "__main__":
    unittest.main()
