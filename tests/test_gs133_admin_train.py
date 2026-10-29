"""0.13.3: open-tile needs a step-up, control stays a strict allow-list, status fails closed.

The wake listener and the relay are fakes. Nothing is recorded and nothing leaves the machine.
"""
import json, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_admin import GOOD, signed_in  # noqa: E402  sets the admin env, then imports grok_admin
import grok_admin as ga  # noqa: E402


def _bot(slug, answered=None, error_at=None, **extra):
    st = {"state": "connected" if answered else "not_configured", "last_error": None}
    if answered is not None:
        st["last_answered_at"] = answered
    if error_at is not None:
        st["last_error_at"] = error_at
    row = {"slug": slug, "name": slug.title(), "voice": "eve", "source": "added", "status": st}
    row.update(extra)
    return row


class OpenTileAndStatus(unittest.TestCase):
    def setUp(self):
        self.wake, self.relay = ga.wake, ga.relay_call
        self.sent = []

        def fake(method, path, body=None, timeout=3):
            self.sent.append((method, path, body))
            if path == "/enroll/open-tile":
                return 200, {"ok": True, "phrase": (body or {}).get("phrase")}
            return 202, {"ok": True}

        ga.wake = fake
        self.c = signed_in()

    def tearDown(self):
        ga.wake, ga.relay_call = self.wake, self.relay

    def test_open_tile_rejects_extra_keys_before_password(self):
        st, j = self.c.req("POST", "/api/enroll/open-tile",
                           {"persona": "otto", "name": "Otto", "count": 9, "password": GOOD})
        self.assertEqual(st, 400, j)
        self.assertEqual(self.sent, [])

    def test_open_tile_password_only_is_rejected(self):
        st, _ = self.c.req("POST", "/api/enroll/open-tile", {"password": GOOD})
        self.assertEqual(st, 400)
        self.assertEqual(self.sent, [])

    def test_open_tile_needs_the_password(self):
        st, j = self.c.req("POST", "/api/enroll/open-tile",
                           {"persona": "otto", "name": "Otto", "phrase": "hey_otto"})
        self.assertEqual(st, 403, j)
        self.assertEqual(self.sent, [])
        st, j = self.c.req("POST", "/api/enroll/open-tile",
                           {"persona": "otto", "name": "Otto", "phrase": "hey_otto", "password": "not the password"})
        self.assertEqual(st, 403, j)
        self.assertEqual(self.sent, [])

    def test_open_tile_posts_only_the_allow_list(self):
        st, j = self.c.req("POST", "/api/enroll/open-tile",
                           {"persona": "otto", "name": "Otto", "phrase": "hey_otto", "password": GOOD})
        self.assertEqual(st, 200, j)
        self.assertEqual(self.sent, [("POST", "/enroll/open-tile",
                                      {"persona": "otto", "name": "Otto", "phrase": "hey_otto"})])
        self.assertNotIn("phrase", j)
        self.assertIn("stepup_until", j)
        self.assertIsInstance(j["stepup_until"], (int, float))

    def test_open_tile_passes_a_busy_listener(self):
        def busy(method, path, body=None, timeout=3):
            self.sent.append((method, path, body))
            return 409, {"error": "enrollment in progress"}
        ga.wake = busy
        st, j = self.c.req("POST", "/api/enroll/open-tile",
                           {"persona": "otto", "phrase": "hey_otto", "password": GOOD})
        self.assertEqual(st, 409, j)
        self.assertNotIn("phrase", j)

    def test_open_tile_fails_closed_when_the_listener_is_down(self):
        def down(method, path, body=None, timeout=3):
            self.sent.append(body)
            return 503, {"error": "wake listener unreachable"}
        ga.wake = down
        st, j = self.c.req("POST", "/api/enroll/open-tile", {"persona": "otto", "password": GOOD})
        self.assertEqual(st, 503, j)
        self.assertEqual(self.sent, [{"persona": "otto"}])
        self.assertNotIn("password", json.dumps(self.sent))

    def test_bad_phrase_still_needs_no_password(self):
        st, j = self.c.req("POST", "/api/enroll", {"phrase": "cat", "persona": "otto"})
        self.assertEqual(st, 400, j)
        self.assertEqual(self.sent, [])

    def test_start_without_password_does_not_record(self):
        st, j = self.c.req("POST", "/api/enroll", {"phrase": "Hey Pax", "persona": "pax"})
        self.assertEqual(st, 403, j)
        self.assertEqual(self.sent, [])
        ov = ga.read_json(ga.path("wake-overrides.json"), {})
        self.assertNotIn("hey_pax", ov)

    def test_control_rejects_extra_keys_and_stays_without_step_up(self):
        st, _ = self.c.req("POST", "/api/enroll/control", {"op": "discard", "password": GOOD})
        self.assertEqual(st, 400)
        self.assertEqual(self.sent, [])
        ga.wake = lambda method, path, body=None, timeout=3: self.sent.append(body) or (200, {"ok": True})
        st, j = self.c.req("POST", "/api/enroll/control", {"op": "pause"})
        self.assertEqual((st, j.get("op")), (200, "pause"))
        self.assertEqual(self.sent, [{"op": "pause"}])

    def test_status_flags_fail_closed(self):
        ga.write_enroll_status({"hey_otto": {"state": "idle", "message": "", "updated_at": 1}})
        cases = [
            (None, {}),
            ((503, {"error": "down"}), {}),
            ((200, {"ok": True}), {}),
            ((200, {"bots": "nope"}), {}),
            ((200, {"bots": [{"slug": "otto", "name": "Otto", "can_train": True, "online": True}]}),
             {"otto": {"can_train": False, "online": False}}),
            ((200, {"bots": [_bot("otto", answered=True)]}),
             {"otto": {"can_train": False, "online": False}}),
            ((200, {"bots": [_bot("otto", answered=0)]}),
             {"otto": {"can_train": False, "online": False}}),
            ((200, {"bots": [_bot("otto", answered=1_700_000_000, error_at=1_700_000_100)]}),
             {"otto": {"can_train": True, "online": False}}),
            ((200, {"bots": [_bot("otto", answered=1_700_000_100, error_at=1_700_000_000)]}),
             {"otto": {"can_train": True, "online": True}}),
            ((200, {"bots": [_bot("juniper", answered=1_700_000_000)]}),
             {"juniper": {"can_train": True, "online": True}}),
        ]
        for got, expect in cases:
            ga.relay_call = (lambda *a, **k: got) if got is not None else (lambda *a, **k: None)
            st, j = self.c.req("GET", "/api/enroll/status")
            self.assertEqual(st, 200, got)
            self.assertIn("hey_otto", j["words"])
            self.assertEqual(j["bots"], expect, got)


if __name__ == "__main__":
    unittest.main()
