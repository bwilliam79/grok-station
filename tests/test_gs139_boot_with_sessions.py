#!/usr/bin/env python3
"""0.13.9 startup bug (Noor, at 1e35c78): `AUTH = Auth()` runs at import and loads sessions.json, which called
session_mode(), defined later in the file. With a saved sign-in on disk the admin died with NameError, so the
restart after every update failed. The other tests start from an empty state dir and missed it.

This re-runs grok_admin's top level from a populated state dir (password, saved sessions, both grace
modes). Fails on 6f18fb1."""
import hashlib
import os
import runpy
import time
import unittest

from test_admin import CLOCK, ga, reset_state

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = os.path.join(ROOT, "admin", "grok_admin.py")


def _boot(grace, seen_ago):
    """Run grok_admin.py's top level again (a fresh module namespace, the shared test_admin sandbox) with a
    password, one saved session and a grace setting already on disk, as after an update restart."""
    reset_state()
    t = time.time()
    ga.write_json(ga.path("auth.json"), {"password": ga.hash_password("kitchen table lamp 42"), "set_at": int(t)})
    ga.write_json(ga.path("sessions.json"), {hashlib.sha256(b"boot-test-token").hexdigest(): {
        "exp": t - seen_ago + 12 * 3600, "seen": t - seen_ago, "sid": "0123abcd"}})
    if grace is not None:
        ga.write_json(ga.path("update-settings.json"), {"password_grace_min": grace})
    try:
        mod = runpy.run_path(ADMIN, run_name="gs_boot_probe")
        return len(mod["AUTH"].sessions)
    finally:
        reset_state()


class BootWithSavedSessions(unittest.TestCase):
    def setUp(self):
        self._clock = CLOCK[0]

    def tearDown(self):
        CLOCK[0] = self._clock

    def test_session_mode_keeps_an_old_sign_in(self):
        self.assertEqual(_boot("session", 2 * 86400), 1)

    def test_timed_mode_drops_a_stale_one_and_keeps_a_fresh_one(self):
        self.assertEqual(_boot(10, 2 * 86400), 0)
        self.assertEqual(_boot(None, 60), 1)


class ImportTimeOrder(unittest.TestCase):
    def test_auth_init_only_calls_functions_defined_above_it(self):
        import ast
        t = ast.parse(open(os.path.join(ROOT, "admin", "grok_admin.py"), encoding="utf-8").read())
        defs = {n.name: n for n in t.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        meth = {m.name: m for m in defs["Auth"].body if isinstance(m, ast.FunctionDef)}
        line = [n.lineno for n in t.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "AUTH"][0]
        seen, late = set(), []

        def walk(fn):
            for c in ast.walk(fn):
                f = c.func if isinstance(c, ast.Call) else None
                if isinstance(f, ast.Name) and f.id in defs and f.id not in seen:
                    seen.add(f.id)
                    if defs[f.id].lineno > line:
                        late.append(f.id)
                    walk(defs[f.id])
                if isinstance(c, ast.Attribute) and getattr(c.value, "id", "") == "self" and c.attr in meth and c.attr not in seen:
                    seen.add(c.attr)
                    walk(meth[c.attr])
        walk(meth["__init__"])
        self.assertIn("session_mode", seen)
        self.assertEqual(late, [])


if __name__ == "__main__":
    unittest.main()
