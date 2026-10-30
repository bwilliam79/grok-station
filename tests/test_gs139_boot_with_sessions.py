#!/usr/bin/env python3
"""0.13.9 startup bug (Noor, at 1e35c78): `AUTH = Auth()` runs at import and loads sessions.json, which called
session_mode(), defined later in the file. With a saved sign-in on disk the admin died with NameError, so the
restart after every update failed. The other tests start from an empty state dir and missed it.

This boots grok_admin in a fresh interpreter from a populated state dir (password, saved sessions, both grace
modes). Fails on 6f18fb1."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = r"""
import sys
sys.path.insert(0, %r)
import grok_admin as ga
print("SESSIONS", len(ga.AUTH.sessions))
"""


def _boot(grace, seen_ago):
    d = tempfile.mkdtemp(prefix="gs-boot-")
    state, run = os.path.join(d, "state"), os.path.join(d, "run")
    os.makedirs(state)
    os.makedirs(run)
    t = time.time()
    tok = "boot-test-token"
    with open(os.path.join(state, "auth.json"), "w") as fh:
        json.dump({"password": {"salt": "00", "hash": "00", "n": 16384, "r": 8, "p": 1}, "set_at": int(t)}, fh)
    with open(os.path.join(state, "sessions.json"), "w") as fh:
        json.dump({hashlib.sha256(tok.encode()).hexdigest(): {"exp": t - seen_ago + 12 * 3600, "seen": t - seen_ago,
                                                             "sid": "0123abcd"}}, fh)
    if grace is not None:
        with open(os.path.join(state, "update-settings.json"), "w") as fh:
            json.dump({"password_grace_min": grace}, fh)
    env = dict(os.environ, ADMIN_STATE_DIR=state, ADMIN_RUN_DIR=run)
    return subprocess.run([sys.executable, "-c", PROBE % os.path.join(ROOT, "admin")], env=env,
                          capture_output=True, text=True, timeout=60)


class BootWithSavedSessions(unittest.TestCase):
    def test_session_mode_keeps_an_old_sign_in(self):
        out = _boot("session", 2 * 86400)
        self.assertEqual(out.returncode, 0, out.stderr[-1500:])
        self.assertIn("SESSIONS 1", out.stdout)

    def test_timed_mode_drops_a_stale_one_and_keeps_a_fresh_one(self):
        out = _boot(10, 2 * 86400)
        self.assertEqual(out.returncode, 0, out.stderr[-1500:])
        self.assertIn("SESSIONS 0", out.stdout)
        out = _boot(None, 60)
        self.assertEqual(out.returncode, 0, out.stderr[-1500:])
        self.assertIn("SESSIONS 1", out.stdout)


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
