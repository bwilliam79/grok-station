"""0.13.10 Nix review: updates_pause only confirms when the station accepted it; app.js TODO names 0.13.11."""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("app", "relay", "wake", "tests"):
    sys.path.insert(0, os.path.join(ROOT, sub))
os.environ.setdefault("RELAY_TOKEN", "test-token")
os.environ.setdefault("XAI_API_KEY", "test-key")
os.environ.setdefault("ALERT_DIR", os.path.join(tempfile.gettempdir(), "gs1310n-alerts"))
os.environ.setdefault("PERSONAS_FILE", os.path.join(tempfile.gettempdir(), "gs1310n-personas.json"))
import relay  # noqa: E402


class UpdatesPauseConfirmsOnlyOnAccept(unittest.TestCase):
    def say(self, status, action="pause"):
        hit = (action, relay.UPDATES_PAUSE_SAY[action])
        sent = []
        env = mock.patch.object(relay, "_station_env_values",
                                lambda keys: {"GROK_STATION_TOKEN": "t", "GROK_STATION_PORT": "8787"})
        with env:
            out = relay.updates_pause_send(hit, post=lambda body, tok, port: sent.append(body) or status) \
                if hasattr(relay, "updates_pause_send") else hit[1]
        return out

    def test_accepted_says_paused(self):
        self.assertEqual(self.say(200), "Updates paused.")
        self.assertEqual(self.say(200, "resume"), "Updates resumed.")

    def test_unreachable_does_not_say_paused(self):
        for status in (503, 401, 500):
            out = self.say(status)
            self.assertNotEqual(out, "Updates paused.")
            self.assertEqual(out, "Can't reach the station.")

    def test_turn_uses_the_checked_send(self):
        src = open(os.path.join(ROOT, "relay", "relay.py")).read()
        self.assertNotRegex(src, r'post_station_show, \{"v": 1, "op": "updates_pause"')

    def test_wording_is_existing_admin_copy(self):
        js = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        self.assertIn("Can't reach the station.", js)


class AppJsTodoNamesNextRelease(unittest.TestCase):
    def test_no_stale_todo(self):
        js = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        self.assertNotIn("TODO(0.13.10)", js)
        self.assertRegex(js, r"TODO\(0\.13\.11\): the owner rejected this separate Set up bots")


if __name__ == "__main__":
    unittest.main()
