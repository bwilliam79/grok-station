#!/usr/bin/env python3
"""0.13.9 admin sign-in (Nix ruling, owner-accepted): "session" mode has no absolute or idle expiry.

Other grace modes keep 12 h absolute and 30 min idle (B7). Logout deletes the server-side token (B1);
a password change or reset ends every session (B2); there is no "sign out everywhere" (removed by the owner);
the cookie is HttpOnly, SameSite=Strict, Secure and fresh per login (B4); tokens are 256-bit from
secrets (B5); only a hash is stored (B6). Before 0.13.9 the session-mode tests fail.
"""
import hashlib
import json
import os
import unittest

from test_admin import C, CLOCK, GOOD, ga, signed_in


def _sessions_file():
    return os.path.join(os.environ["ADMIN_STATE_DIR"], "sessions.json")


class SessionMode(unittest.TestCase):
    def setUp(self):
        self._clock = CLOCK[0]
        CLOCK[0] = 1_700_000_000
        self.c = signed_in()

    def tearDown(self):
        CLOCK[0] = self._clock

    def _grace(self, value):
        st, j = self.c.req("PUT", "/api/system/password-grace", {"password_grace_min": value, "password": GOOD})
        self.assertEqual(st, 200, j)
        return j

    def _signed(self, c=None):
        return (c or self.c).req("GET", "/api/state")[1].get("signed_in")

    def test_session_mode_has_no_absolute_or_idle_expiry(self):
        self._grace("session")
        CLOCK[0] += ga.IDLE_TTL + 60  # idle limit passed
        self.assertTrue(self._signed())
        CLOCK[0] += ga.SESSION_TTL + 3600  # absolute limit passed too
        self.assertTrue(self._signed())
        # A restart reloads it from disk and keeps it.
        ga.AUTH._load_sessions()
        self.assertTrue(self._signed())
        # The grace window lasts the sign-in: no password for a bot change.
        self.assertIsNotNone(self.c.req("GET", "/api/state")[1].get("stepup_until"))

    def test_session_mode_cookie_outlives_twelve_hours(self):
        self._grace("session")
        self.c.req("POST", "/api/logout", {})
        self.assertEqual(self.c.req("POST", "/api/login", {"password": GOOD})[0], 200)
        cookie = self.c.last.getheader("Set-Cookie")
        age = int(cookie.split("Max-Age=")[1].split(";")[0])
        self.assertGreater(age, ga.SESSION_TTL)

    def test_other_modes_keep_twelve_hours_and_thirty_minutes(self):  # B7
        self.assertEqual((ga.SESSION_TTL, ga.IDLE_TTL), (12 * 3600, 30 * 60))
        for value in (0, 10, 120):
            self.c = signed_in()
            self._grace(value)
            CLOCK[0] += ga.IDLE_TTL + 1
            self.assertFalse(self._signed(), value)
            self.c = signed_in()
            self._grace(value)
            start = CLOCK[0]
            while CLOCK[0] + ga.IDLE_TTL - 60 < start + ga.SESSION_TTL:
                CLOCK[0] += ga.IDLE_TTL - 60
                self.assertTrue(self._signed(), value)
            CLOCK[0] = start + ga.SESSION_TTL + 1
            self.assertFalse(self._signed(), value)
            cookie_age = None
            self.c.req("POST", "/api/logout", {})
            self.c.req("POST", "/api/login", {"password": GOOD})
            cookie_age = int(self.c.last.getheader("Set-Cookie").split("Max-Age=")[1].split(";")[0])
            self.assertEqual(cookie_age, ga.SESSION_TTL)

    def test_leaving_session_mode_restores_limits(self):
        self._grace("session")
        CLOCK[0] += ga.SESSION_TTL + 10
        self.assertTrue(self._signed())
        self._grace(10)
        self.assertFalse(self._signed())


class Tokens(unittest.TestCase):
    def setUp(self):
        self._clock = CLOCK[0]
        CLOCK[0] = 1_700_000_000
        self.c = signed_in()

    def tearDown(self):
        CLOCK[0] = self._clock

    def test_logout_deletes_server_side_token(self):  # B1
        tok = self.c.jar["gs_session"]
        key = hashlib.sha256(tok.encode()).hexdigest()
        self.assertIn(key, ga.AUTH.sessions)
        self.assertEqual(self.c.req("POST", "/api/logout", {})[0], 200)
        self.assertNotIn(key, ga.AUTH.sessions)
        self.assertNotIn(key, json.load(open(_sessions_file())))
        replay = C()
        replay.jar["gs_session"] = tok
        self.assertEqual(replay.req("GET", "/api/system")[0], 401)

    def test_password_change_and_reset_end_every_session(self):  # B2
        other = C()
        self.assertEqual(other.req("POST", "/api/login", {"password": GOOD})[0], 200)
        self.assertEqual(len(ga.AUTH.sessions), 2)
        self.assertEqual(self.c.req("POST", "/api/password", {"old": GOOD, "new": "a brand new pass 7"})[0], 200)
        self.assertEqual(other.req("GET", "/api/system")[0], 401)
        self.assertEqual(len(ga.AUTH.sessions), 1)  # only the fresh one the change just issued
        # reset-admin (factory reset of the login) ends every session, even in session mode.
        self.assertEqual(self.c.req("PUT", "/api/system/password-grace",
                                    {"password_grace_min": "session", "password": "a brand new pass 7"})[0], 200)
        auth = os.path.join(os.environ["ADMIN_STATE_DIR"], "auth.json")
        doc = json.load(open(auth))
        doc.pop("password", None)
        doc["reset_at"] = int(CLOCK[0])
        json.dump(doc, open(auth, "w"))
        self.assertNotEqual(self.c.req("GET", "/api/system")[0], 200)
        ga.AUTH._load_sessions()
        self.assertEqual(ga.AUTH.sessions, {})

    def test_cookie_flags_and_fresh_token_per_login(self):  # B4
        seen = set()
        for _ in range(3):
            self.c.req("POST", "/api/logout", {})
            self.assertEqual(self.c.req("POST", "/api/login", {"password": GOOD})[0], 200)
            cookie = self.c.last.getheader("Set-Cookie")
            for flag in ("HttpOnly", "SameSite=Strict", "Secure"):
                self.assertIn(flag, cookie)
            seen.add(self.c.jar["gs_session"])
        self.assertEqual(len(seen), 3)

    def test_token_strength_and_only_hash_stored(self):  # B5, B6
        tok = self.c.jar["gs_session"]
        self.assertGreaterEqual(len(tok), 43)  # token_urlsafe(32): 256 bits
        src = open(ga.__file__, encoding="utf-8").read()
        self.assertIn("t = secrets.token_urlsafe(32)", src)
        raw = open(_sessions_file(), encoding="utf-8").read()
        self.assertNotIn(tok, raw)
        self.assertIn(hashlib.sha256(tok.encode()).hexdigest(), raw)


class NoLogoutAll(unittest.TestCase):
    """Sign out everywhere was removed (owner, 2026-10-30). Only a password change or reset ends other sessions."""

    def test_endpoint_is_gone(self):
        c = signed_in()
        self.assertEqual(c.req("POST", "/api/logout-all", {})[0], 404)
        self.assertEqual(len(ga.AUTH.sessions), 1)
        self.assertFalse(hasattr(ga.AUTH, "logout_all"))


class SessionPrune(unittest.TestCase):
    """Nix L1 (0.13.9): session mode never expired sessions, so every login added one forever.
    The store keeps the newest 20 by last seen and drops any not seen for 400 days. Fails on 6f18fb1."""

    def setUp(self):
        self._clock = CLOCK[0]
        CLOCK[0] = 1_700_000_000
        self.c = signed_in()
        st, _ = self.c.req("PUT", "/api/system/password-grace", {"password_grace_min": "session", "password": GOOD})
        self.assertEqual(st, 200)

    def tearDown(self):
        CLOCK[0] = self._clock

    def test_newest_twenty_by_last_seen(self):
        clients = []
        for _ in range(25):
            CLOCK[0] += 60
            c = C()
            self.assertEqual(c.req("POST", "/api/login", {"password": GOOD})[0], 200)
            clients.append(c)
        self.assertEqual(len(ga.AUTH.sessions), 20)
        self.assertEqual(len(json.load(open(_sessions_file()))), 20)
        self.assertNotEqual(clients[0].req("GET", "/api/system")[0], 200)
        self.assertEqual(clients[-1].req("GET", "/api/system")[0], 200)
        # Last seen, not age: an old login that is used stays.
        CLOCK[0] += 60
        self.assertEqual(clients[6].req("GET", "/api/system")[0], 200)
        CLOCK[0] += 60
        C().req("POST", "/api/login", {"password": GOOD})
        self.assertEqual(clients[6].req("GET", "/api/system")[0], 200)
        self.assertNotEqual(clients[5].req("GET", "/api/system")[0], 200)  # the oldest by last seen

    def test_unseen_for_400_days_is_dropped(self):
        self.assertEqual(self.c.req("GET", "/api/system")[0], 200)
        CLOCK[0] += 399 * 86400
        self.assertEqual(self.c.req("GET", "/api/system")[0], 200)
        CLOCK[0] += 400 * 86400 + 1
        self.assertEqual(self.c.req("GET", "/api/system")[0], 401)
        self.assertEqual(ga.AUTH.sessions, {})
