#!/usr/bin/env python3
"""Ask-again window (gs138): 0, 1..120, or the distinct value "session".

"session" lasts this sign-in until logout or a password change (0.13.9: no idle or
absolute limit in this mode). Password change, reset-admin, the setting itself, and
turning personal-info blocking off still ask in the timed modes; in "session" mode
(0.13.9) only a password change or reset-admin asks. No browser for the page; the copy
helpers run under node. The API tests use the shared admin server.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import unittest
from contextlib import redirect_stdout

import test_admin as ta
from test_admin import C, CLOCK, GOOD, ga, signed_in

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()


def _fn(name):
    start = JS.index("\nfunction " + name + "(")
    depth, j = 0, JS.index("{", start)
    for k in range(j, len(JS)):
        depth += {"{": 1, "}": -1}.get(JS[k], 0)
        if depth == 0:
            return JS[start:k + 1]
    raise AssertionError("unclosed " + name)


def _run(expr):
    prog = "\n".join(_fn(n) for n in (
        "graceView", "gracePayload", "graceMinutesInvalid", "graceToast", "graceSummary",
        "graceFormHTML", "privacyAsksFirst"))
    prog = "const esc = s => String(s);\n" + prog + "\nprocess.stdout.write(JSON.stringify(" + expr + "));"
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


class GraceCopy(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_choice_toast_and_notice(self):
        self.assertEqual(_run("graceView(0)"), {"choice": "every", "minutes": 10})
        self.assertEqual(_run("graceView(10)"), {"choice": "time", "minutes": 10})
        self.assertEqual(_run("graceView('session', 25)"), {"choice": "session", "minutes": 25})
        self.assertEqual(_run("gracePayload('every', 10)"), 0)
        self.assertEqual(_run("gracePayload('session', 10)"), "session")
        self.assertEqual(_run("gracePayload('time', '7')"), 7)
        self.assertEqual(_run("graceMinutesInvalid('time', '')"), "Enter 1 to 120 minutes.")
        self.assertEqual(_run("graceMinutesInvalid('time', '0')"), "Enter 1 to 120 minutes.")
        self.assertEqual(_run("graceMinutesInvalid('time', '121')"), "Enter 1 to 120 minutes.")
        self.assertEqual(_run("graceMinutesInvalid('time', '1.5')"), "Enter 1 to 120 minutes.")
        self.assertEqual(_run("graceMinutesInvalid('every', '')"), "")
        self.assertEqual(_run("graceToast(0)"), "Saved. The password is asked for each change.")
        self.assertEqual(_run("graceToast(1)"), "Saved. The password is asked again after 1 minute.")
        self.assertEqual(_run("graceToast(10)"), "Saved. The password is asked again after 10 minutes.")
        self.assertEqual(_run("graceToast('session')"), "Saved. The password isn't asked again this session.")
        self.assertEqual(_run("graceSummary(0)"), "Every time")
        self.assertEqual(_run("graceSummary(1)"), "After 1 minute")
        self.assertEqual(_run("graceSummary(10)"), "After 10 minutes")
        self.assertEqual(_run("graceSummary('session')"), "Not while signed in")
        self.assertTrue(_run("privacyAsksFirst(false, true)"))
        self.assertTrue(_run("privacyAsksFirst(false, false)"))
        self.assertTrue(_run("privacyAsksFirst(true, false)"))
        self.assertFalse(_run("privacyAsksFirst(true, true)"))
        html = _run("graceFormHTML(graceView('session', 10))")
        self.assertIn("Ask for the password again", html)
        self.assertIn("For changes like adding a bot or an integration, while you're signed in. Signing out always ends this.", html)
        self.assertIn("Ask for each change.", html)
        self.assertIn("Ask once, then not again for a while.", html)
        self.assertIn("Ask once, then not again until you sign out or the password changes.", html)
        self.assertIn("Anyone using this signed-in browser can change bots, webhooks and most other settings without the password.", html)
        self.assertIn("Changing the password and a few other security settings always ask.", html)
        self.assertIn(">minutes<", html)
        self.assertIn('id="gnoch">No changes', html)
        self.assertIn('aria-describedby="gnoch"', html)
        self.assertIn('id="gmin" hidden', html)  # minutes field is removed for this sign-in
        self.assertIn('id="gnotice" role="note">', html)  # notice is shown only for this choice
        self.assertNotIn('id="gnotice" role="note" hidden', html)
        every = _run("graceFormHTML(graceView(0))")
        self.assertIn('id="gmin" hidden', every)
        self.assertIn('id="gnotice" role="note" hidden', every)
        timed = _run("graceFormHTML(graceView(10))")
        self.assertIn('id="gmin">', timed)
        self.assertNotIn('id="gmin" hidden', timed)
        self.assertIn('id="gnotice" role="note" hidden', timed)


class SessionGrace(unittest.TestCase):
    URL, KEY = "https://bot.example/hook", "k" * 20

    def setUp(self):
        self._clock = CLOCK[0]
        CLOCK[0] = 1_700_000_000
        self.c = signed_in()
        self._rc = ga.relay_call

        def fake(method, path, payload=None, timeout=10, raw=None, content_type=None):
            if path == "/admin/bots" and method == "GET":
                return 200, {"bots": [{"slug": "kai", "name": "Kai", "voice": "eve"}], "voices": ["eve"]}
            if path == "/admin/policy" and method == "PUT":
                return 200, {"ok": True, "pii_mode": payload.get("pii_mode")}
            if path == "/admin/policy" and method == "GET":
                return 200, {"pii_mode": True}
            return 200, {"ok": True, "last4": "WXYZ"}

        ga.relay_call = fake

    def tearDown(self):
        ga.relay_call = self._rc
        CLOCK[0] = self._clock

    def _put(self, value, password=GOOD):
        body = {"password_grace_min": value}
        if password is not None:
            body["password"] = password
        return self.c.req("PUT", "/api/system/password-grace", body)

    def _rec(self):
        return next(iter(ga.AUTH.sessions.values()))

    def test_session_window_lasts_the_sign_in(self):
        # 0.13.9 (Nix ruling, owner-accepted): "session" has no idle or absolute expiry; logout ends it.
        self.assertEqual(ga.IDLE_TTL, 30 * 60)
        self.assertEqual(ga.SESSION_TTL, 12 * 3600)
        buf = io.StringIO()
        with redirect_stdout(buf):
            st, j = self._put("session")
        self.assertEqual(st, 200, j)
        self.assertEqual(j["password_grace_min"], "session")
        rec = self._rec()
        self.assertGreater(j["stepup_until"] - CLOCK[0], ga.SESSION_TTL)
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kay"})[0], 200)
        line = buf.getvalue()
        self.assertIn("action=password-grace old=10 new=session", line)
        self.assertIn("session=" + rec["sid"], line)
        self.assertIn("time=%d" % int(CLOCK[0]), line)
        self.assertNotIn(GOOD, line)
        self.assertNotIn(self.c.jar.get("gs_session", "missing-cookie"), line)
        self.assertNotIn(hashlib.sha256(self.c.jar["gs_session"].encode()).hexdigest(), line)
        # Neither idle nor the old 12 h cap ends it now.
        CLOCK[0] = rec["seen"] + ga.SESSION_TTL + ga.IDLE_TTL + 1
        st, state = self.c.req("GET", "/api/state")
        self.assertTrue(state.get("signed_in"))
        self.assertIsNotNone(state.get("stepup_until"))
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kai"})[0], 200)

    def test_logout_and_password_change_end_it(self):
        self.assertEqual(self._put("session")[0], 200)
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kay"})[0], 200)
        self.c.req("POST", "/api/logout", {})
        self.assertEqual(self.c.req("POST", "/api/login", {"password": GOOD})[0], 200)
        self.assertIsNone(self.c.req("GET", "/api/state")[1]["stepup_until"])
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kai"})[0], 403)
        self.assertEqual(self._put("session")[0], 200)
        self.assertEqual(self.c.req("POST", "/api/password", {"old": GOOD, "new": "a brand new pass 7"})[0], 200)
        self.assertIsNone(self.c.req("GET", "/api/state")[1]["stepup_until"])
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kay"})[0], 403)

    def test_password_change_still_asks_during_session(self):
        # Condition 1: an open "session" window is not a substitute for the current password.
        self.assertEqual(self._put("session")[0], 200)
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kay"})[0], 200)
        st, j = self.c.req("POST", "/api/password", {"new": "a brand new pass 7"})
        self.assertEqual(st, 400, j)
        self.assertEqual(j.get("error"), "Current password is wrong.")
        st, j = self.c.req("POST", "/api/password", {"old": "not-the-password", "new": "a brand new pass 7"})
        self.assertEqual(st, 400, j)
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kai"})[0], 200)
        self.assertIsNotNone(self.c.req("GET", "/api/state")[1].get("stepup_until"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            st, j = self._put(10)
        self.assertEqual(st, 200, j)
        line = buf.getvalue()
        self.assertIn("action=password-grace old=session new=10", line)
        self.assertIn("session=" + self._rec()["sid"], line)
        self.assertIn("time=%d" % int(CLOCK[0]), line)
        self.assertNotIn(GOOD, line)
        self.assertNotIn(self.c.jar.get("gs_session", "missing-cookie"), line)

    def test_always_reasks_pii_off_grace_and_reset(self):
        # A timed window (10 min): these still ask. Session mode skips them (SessionModeSkipsReask).
        self.assertEqual(self._put(10)[0], 200)
        self.assertEqual(self.c.req("PUT", "/api/system/password-grace", {"password_grace_min": 10})[0], 403)
        self.assertEqual(self.c.req("PUT", "/api/privacy", {"pii_mode": False})[0], 403)
        buf = io.StringIO()
        with redirect_stdout(buf):
            st, _ = self.c.req("PUT", "/api/privacy", {"pii_mode": False, "password": GOOD})
        self.assertEqual(st, 200)
        self.assertIn("pii_mode off", buf.getvalue())
        self.assertNotIn(GOOD, buf.getvalue())
        # Turning it back on may use the window the password just opened.
        self.assertEqual(self.c.req("PUT", "/api/privacy", {"pii_mode": True})[0], 200)
        # reset-admin drops the password, so the old cookie is not a window.
        auth = os.path.join(os.environ["ADMIN_STATE_DIR"], "auth.json")
        doc = json.load(open(auth))
        doc.pop("password", None)
        doc["reset_at"] = int(CLOCK[0])
        json.dump(doc, open(auth, "w"))
        self.assertTrue(ga.AUTH.setup_required())
        st, state = self.c.req("GET", "/api/state")
        self.assertTrue(st == 401 or state.get("stepup_until") is None)
        self.assertNotEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kay"})[0], 200)

    def test_invalid_values_and_factory_default(self):
        for bad in ("Session", "forever", "session ", 121, -1, 1.5, True, False, None, "10", 0.0):
            st, j = self._put(bad)
            self.assertEqual(st, 400, (bad, j))
        self.assertEqual(ga.password_grace_min(), 10)
        self.assertEqual(self._put("session")[0], 200)
        self.assertEqual(ga.password_grace_min(), "session")
        settings = os.path.join(os.environ["ADMIN_STATE_DIR"], "update-settings.json")
        doc = json.load(open(settings))
        self.assertEqual(doc["password_grace_min"], "session")
        doc.pop("password_grace_min")
        json.dump(doc, open(settings, "w"))
        self.assertEqual(ga.password_grace_min(), 10)
        os.remove(settings)
        self.assertEqual(ga.password_grace_min(), 10)
        self.assertEqual(self.c.req("GET", "/api/system")[1]["password_grace_min"], 10)
        # A corrupt stored value is not "session" and is not kept.
        json.dump({"password_grace_min": "forever", "autocheck": True}, open(settings, "w"))
        self.assertEqual(ga.password_grace_min(), 10)
        self.assertIs(json.load(open(settings))["autocheck"], True)

    def test_numeric_values_still_work(self):
        st, j = self._put(0)
        self.assertEqual((st, j["password_grace_min"], j["stepup_until"]), (200, 0, None))
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kay"})[0], 403)
        st, j = self._put(15)
        self.assertEqual(st, 200, j)
        self.assertEqual(j["password_grace_min"], 15)
        self.assertEqual(j["stepup_until"], CLOCK[0] + 15 * 60)

    def test_omitted_password_does_not_lock_out(self):
        self.assertEqual(self._put(10)[0], 200)
        for _ in range(6):
            st, j = self.c.req("PUT", "/api/privacy", {"pii_mode": False})
            self.assertEqual(st, 403, j)
            self.assertNotEqual(j.get("error"), "Too many tries. Wait 15 minutes.")
        st, _ = self.c.req("PUT", "/api/privacy", {"pii_mode": False, "password": GOOD})
        self.assertEqual(st, 200)
        for _ in range(5):
            self.assertEqual(self.c.req("PUT", "/api/system/password-grace",
                                        {"password_grace_min": 0, "password": "wrong-wrong-1"})[0], 403)
        self.assertEqual(self.c.req("PUT", "/api/privacy", {"pii_mode": True, "password": GOOD})[0], 429)

    def test_allow_bots_action_control_is_absent(self):
        blob = JS + open(os.path.join(ROOT, "admin", "grok_admin.py"), encoding="utf-8").read()
        self.assertNotIn("Allow bots to take action", blob)


class SessionModeSkipsReask(unittest.TestCase):
    """0.13.9 (owner decision 2026-10-30, Nix conditions): in "session" mode, being signed in is enough for
    privacy off, the grace setting and WLED Voice home control on. Each writes an audit line with old and new
    values and a time, never a token. Password change and reset-admin still ask; other modes still re-ask.
    Fails on 5ffe867 (all three answered 403 without a password)."""

    def setUp(self):
        self._clock = CLOCK[0]
        CLOCK[0] = 1_700_000_000
        self.c = signed_in()
        self._rc = ga.relay_call
        self.pii = [True]
        self._reg = os.environ.get("ADMIN_INTEGRATIONS")
        os.environ["ADMIN_INTEGRATIONS"] = os.path.join(os.environ["ADMIN_STATE_DIR"], "integrations.json")

        def fake(method, path, payload=None, timeout=10, raw=None, content_type=None):
            if path == "/admin/policy" and method == "PUT":
                self.pii[0] = payload.get("pii_mode")
                return 200, {"ok": True, "pii_mode": self.pii[0]}
            if path == "/admin/policy" and method == "GET":
                return 200, {"pii_mode": self.pii[0]}
            return 200, {"ok": True}
        ga.relay_call = fake

    def tearDown(self):
        ga.relay_call = self._rc
        CLOCK[0] = self._clock
        if self._reg is None:
            os.environ.pop("ADMIN_INTEGRATIONS", None)
        else:
            os.environ["ADMIN_INTEGRATIONS"] = self._reg

    def _fresh_session(self, grace):
        st, _ = self.c.req("PUT", "/api/system/password-grace", {"password_grace_min": grace, "password": GOOD})
        self.assertEqual(st, 200)
        self.c.req("POST", "/api/logout", {})
        self.assertEqual(self.c.req("POST", "/api/login", {"password": GOOD})[0], 200)  # no grace window open yet

    def _seed_wled(self):
        doc = ga.gsi.load()
        doc["integrations"]["wled"] = {"enabled": True}
        doc["devices"].append({"id": "w-abcd", "kind": "wled", "host": "127.0.0.1:9", "name": "Kitchen Strip",
                               "aliases": [], "type": "light", "caps": list(ga.gsi.CAPS), "presets": {}})
        ga.gsi.save(doc)

    def _audit(self, fn):
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = fn()
        return out, [ln for ln in buf.getvalue().splitlines() if ln.startswith("audit ")]

    def test_signed_in_is_enough_in_session_mode(self):
        self._fresh_session("session")
        tok = self.c.jar["gs_session"]
        sid = ga.AUTH.session_public_id(tok)
        (st, j), lines = self._audit(lambda: self.c.req("PUT", "/api/privacy", {"pii_mode": False}))
        self.assertEqual(st, 200, j)
        self.assertIn("audit persona=- action=pii_mode old=on new=off session=%s last4=- time=%d" % (sid, CLOCK[0]), lines)
        self._seed_wled()
        (st, j), lines = self._audit(lambda: self.c.req("PUT", "/api/integrations/wled/voice", {"on": True}))
        self.assertEqual(st, 200, j)
        self.assertTrue(ga.gsi.load()["integrations"]["wled"]["voice"])
        self.assertIn("audit persona=- action=voice-home-control old=off new=on session=%s last4=- time=%d" % (sid, CLOCK[0]), lines)
        (st, j), lines = self._audit(lambda: self.c.req("PUT", "/api/system/password-grace", {"password_grace_min": 10}))
        self.assertEqual(st, 200, j)
        self.assertIn("audit persona=- action=password-grace old=session new=10 session=%s last4=- time=%d" % (sid, CLOCK[0]), lines)
        for ln in lines:
            self.assertNotIn(tok, ln)
            self.assertNotIn(hashlib.sha256(tok.encode()).hexdigest(), ln)
        # Now in a timed mode with no window: it asks again.
        self.assertEqual(self.c.req("PUT", "/api/system/password-grace", {"password_grace_min": "session"})[0], 403)

    def test_password_change_still_asks_and_checks_still_apply(self):
        self._fresh_session("session")
        self.assertEqual(self.c.req("POST", "/api/password", {"new": "a brand new pass 7"})[0], 400)
        self.assertEqual(self.c.req("PUT", "/api/privacy", {"pii_mode": False}, csrf=False)[0], 403)
        self.assertEqual(self.c.req("PUT", "/api/privacy", {"pii_mode": False}, ctype="text/plain")[0], 403)
        self.assertEqual(C().req("PUT", "/api/privacy", {"pii_mode": False})[0], 401)
        self.assertEqual(C().req("PUT", "/api/system/password-grace", {"password_grace_min": 0})[0], 401)
        self.assertTrue(self.pii[0])

    def test_state_flag_and_ui_skip_the_prompt(self):
        self._fresh_session("session")
        self.assertIs(self.c.req("GET", "/api/state")[1]["grace_session"], True)
        self._fresh_session(10)
        self.assertIs(self.c.req("GET", "/api/state")[1]["grace_session"], False)
        self.assertIsNone(C().req("GET", "/api/state")[1]["grace_session"])
        self.assertIn("function graceSession() { return !!(S.state && S.state.grace_session === true); }", JS)
        self.assertIn("privacyAsksFirst(next, stepupOpen(), graceSession())", JS)
        self.assertIn('if (graceSession()) saveGrace("")', JS)
        self.assertIn('if (!on || graceSession()) send("")', JS)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_privacy_prompt_rule(self):
        self.assertFalse(_run("privacyAsksFirst(false, false, true)"))
        self.assertTrue(_run("privacyAsksFirst(false, true, false)"))

    def test_other_modes_still_reask(self):
        for grace in (0, 10):
            self._fresh_session(grace)
            self.assertEqual(self.c.req("PUT", "/api/privacy", {"pii_mode": False})[0], 403, grace)
            self.assertEqual(self.c.req("PUT", "/api/system/password-grace", {"password_grace_min": 5})[0], 403, grace)
            self._seed_wled()
            self.assertEqual(self.c.req("PUT", "/api/integrations/wled/voice", {"on": True})[0], 403, grace)
