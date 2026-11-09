"""0.13.29: Allowed networks. Extra private IPv4 client ranges for the admin page and the bot endpoints.

Default empty: only loopback and this station's interface subnets, as before. A saved range is RFC 1918,
/16 to /32, at most 4, normalized, and matched against the TCP peer only (never X-Forwarded-For).
Saving always asks for the password (also during a grace window and in "session" mode) and writes one
audit line with the old and new list.
"""
import io
import ipaddress
import json
import unittest
from contextlib import redirect_stdout

from test_admin import CLOCK, GOOD, ga, signed_in

LOOP_ONLY = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
HOME = LOOP_ONLY + [ipaddress.ip_network("10.10.10.0/24")]


class Parse(unittest.TestCase):
    def test_valid_and_normalized(self):
        got, err = ga.parse_allowed_networks([" 192.168.77.7/24", "10.0.0.0/16", "172.16.3.9", "192.168.77.0/24"])
        self.assertIsNone(err)
        self.assertEqual(got, ["192.168.77.0/24", "10.0.0.0/16", "172.16.3.9/32"])
        self.assertEqual(ga.parse_allowed_networks([]), ([], None))

    def test_rejects_public_wide_ipv6_and_junk(self):
        for bad in (["0.0.0.0/0"], ["10.0.0.0/8"], ["192.168.4.0/15"], ["172.16.3.0/12"], ["8.8.8.0/24"],
                    ["100.64.0.0/16"], ["169.254.1.0/24"], ["127.0.0.0/16"], ["172.32.0.0/16"],
                    ["fd00::/64"], ["::ffff:192.168.77.0/120"], ["192.168.77.0/33"], ["192.168.77.0/24 x"],
                    ["192.168.77.0/024"], ["192.168.077.0/24"], ["192.168.77.256/24"], [""], [None], [24],
                    "192.168.77.0/24", None, {"a": 1},
                    ["10.0.0.0/24", "10.10.10.0/24", "10.42.0.0/24", "10.64.0.0/24", "192.168.77.0/24"]):
            got, err = ga.parse_allowed_networks(bad)
            self.assertIsNone(got, bad)
            self.assertEqual(err, ga.ALLOWED_NETS_ERROR, bad)


class LanOk(unittest.TestCase):
    def test_default_empty_is_unchanged(self):
        self.assertFalse(ga.lan_ok("192.168.77.7", nets=HOME, allowed=[]))
        self.assertTrue(ga.lan_ok("10.10.10.9", nets=HOME, allowed=[]))
        self.assertTrue(ga.lan_ok("127.0.0.1", nets=[], allowed=[]))

    def test_configured_range_accepted_and_outside_rejected(self):
        allowed = ["192.168.77.0/24"]
        self.assertTrue(ga.lan_ok("192.168.77.7", nets=HOME, allowed=allowed))
        self.assertTrue(ga.lan_ok("::ffff:192.168.77.200", nets=HOME, allowed=allowed))
        self.assertFalse(ga.lan_ok("192.168.4.7", nets=HOME, allowed=allowed))
        self.assertFalse(ga.lan_ok("203.0.113.5", nets=HOME, allowed=allowed))
        self.assertFalse(ga.lan_ok("fd00::1", nets=HOME, allowed=allowed))
        self.assertFalse(ga.lan_ok("not an ip", nets=HOME, allowed=allowed))

    def test_saved_value_is_read_and_a_bad_stored_value_is_ignored(self):
        signed_in()
        self.assertEqual(ga.allowed_networks(), [])
        self.assertFalse(ga.lan_ok("192.168.77.7", nets=HOME))
        ga.store_allowed_networks(["192.168.77.0/24"])
        self.assertTrue(ga.lan_ok("192.168.77.7", nets=HOME))
        ga.store_allowed_networks(["0.0.0.0/0"])  # a hand-edited file: fail closed, as if empty
        self.assertEqual(ga.allowed_networks(), [])
        self.assertFalse(ga.lan_ok("192.168.77.7", nets=HOME))
        ga.store_allowed_networks([])


class Guard(unittest.TestCase):
    """The request gate on the admin port: page, API and the /voice/* bot endpoints."""

    def _handler(self, ip, headers=None):
        h = ga.Handler.__new__(ga.Handler)
        h.client_address = (ip, 50000)
        h.headers = headers or {}
        h.sent = []
        h.send = lambda code, obj=None, **k: h.sent.append((code, obj))
        h.command, h.path = "GET", "/api/state"
        return h

    def setUp(self):
        signed_in()
        self._subnets = ga.home_subnets
        ga.home_subnets = lambda *a, **k: list(HOME)

    def tearDown(self):
        ga.home_subnets = self._subnets
        ga.store_allowed_networks([])

    def test_guard_and_voice_ingress_follow_the_setting(self):
        h = self._handler("192.168.77.7")
        self.assertFalse(h.guard(False))
        self.assertEqual(h.sent, [(403, {"error": "LAN only"})])
        v = self._handler("192.168.77.7")
        v.voice_ingress("POST", "/voice/show")
        self.assertEqual(v.sent, [(403, {"error": "LAN only"})])
        ga.store_allowed_networks(["192.168.77.0/24"])
        h = self._handler("192.168.77.7")
        self.assertTrue(h.guard(False))
        self.assertEqual(h.sent, [])
        v = self._handler("192.168.77.7", {"Transfer-Encoding": "chunked"})
        v.voice_ingress("POST", "/voice/show")
        self.assertEqual(v.sent, [(411, {"error": "length required"})])  # past the LAN gate
        h = self._handler("192.168.4.7")
        self.assertFalse(h.guard(False))

    def test_forwarded_for_is_never_trusted(self):
        ga.store_allowed_networks(["192.168.77.0/24"])
        h = self._handler("203.0.113.5", {"X-Forwarded-For": "192.168.77.7", "X-Real-IP": "192.168.77.7",
                                          "Forwarded": "for=192.168.77.7"})
        self.assertFalse(h.guard(False))
        self.assertEqual(h.sent, [(403, {"error": "LAN only"})])


class Api(unittest.TestCase):
    def setUp(self):
        self._clock = CLOCK[0]
        CLOCK[0] = 1_700_000_000
        self.c = signed_in()

    def tearDown(self):
        ga.store_allowed_networks([])
        CLOCK[0] = self._clock

    def put(self, nets, password=None):
        body = {"allowed_networks": nets}
        if password is not None:
            body["password"] = password
        return self.c.req("PUT", "/api/system/allowed-networks", body)

    def test_default_empty_in_system_view(self):
        st, j = self.c.req("GET", "/api/system")
        self.assertEqual((st, j.get("allowed_networks")), (200, []))

    def test_save_always_asks_and_logs_old_and_new(self):
        # Open a grace window first: it must not be enough for this setting.
        ga.AUTH.open_stepup(self.c.jar["gs_session"])
        self.assertIsNotNone(self.c.req("GET", "/api/state")[1].get("stepup_until"))
        self.assertEqual(self.put(["192.168.77.0/24"])[0], 403)
        self.assertEqual(ga.allowed_networks(), [])
        self.assertEqual(self.put(["192.168.77.0/24"], "wrong password 1")[0], 403)
        self.assertEqual(ga.allowed_networks(), [])
        out = io.StringIO()
        with redirect_stdout(out):
            st, j = self.put(["192.168.77.9/24"], GOOD)
        self.assertEqual((st, j), (200, {"ok": True, "allowed_networks": ["192.168.77.0/24"]}))
        self.assertEqual(ga.allowed_networks(), ["192.168.77.0/24"])
        line = [ln for ln in out.getvalue().splitlines() if "action=allowed-networks" in ln]
        self.assertEqual(len(line), 1, out.getvalue())
        self.assertIn("old=- new=192.168.77.0/24 ", line[0])
        self.assertNotIn(GOOD, out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self.put([], GOOD)[0], 200)
        self.assertIn("old=192.168.77.0/24 new=- ", out.getvalue())

    def test_session_mode_still_asks(self):
        self.assertEqual(self.c.req("PUT", "/api/system/password-grace",
                                    {"password_grace_min": "session", "password": GOOD})[0], 200)
        self.assertEqual(self.put(["192.168.77.0/24"])[0], 403)
        self.assertEqual(self.put(["192.168.77.0/24"], GOOD)[0], 200)

    def test_invalid_is_rejected_before_the_password(self):
        for bad in (["0.0.0.0/0"], ["8.8.8.0/24"], ["10.0.0.0/8"], "192.168.77.0/24"):
            st, j = self.put(bad, GOOD)
            self.assertEqual(st, 400, bad)
            self.assertEqual(j["error"], ga.ALLOWED_NETS_ERROR)
        self.assertEqual(ga.allowed_networks(), [])

    def test_needs_a_signed_in_session(self):
        from test_admin import C
        self.assertEqual(C().req("PUT", "/api/system/allowed-networks",
                                 {"allowed_networks": ["192.168.77.0/24"], "password": GOOD})[0], 401)


if __name__ == "__main__":
    unittest.main()
