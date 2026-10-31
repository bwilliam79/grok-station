"""0.13.13: the Updates switch saves under the installed layout (top of STATE_DIR is root 0755, state/ is relay-owned)."""
import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_cards as tc  # noqa: E402,F401  (same env as the other relay tests)
import relay  # noqa: E402


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory modes")
class InstalledLayout(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp()
        os.mkdir(os.path.join(self.top, "state"), 0o770)
        os.mkdir(os.path.join(self.top, "bots"), 0o770)
        os.chmod(self.top, 0o555)  # like root:root 0755 as seen by grok-relay
        self.saved = (relay.STATE_DIR, relay.admin_gate, relay.admin_json, relay._known, relay.log,
                      relay.post_station_updates_drop)
        relay.STATE_DIR = self.top
        self.logs = []

        async def gate(req):
            return None

        async def aj(req):
            return self.body
        relay.admin_gate, relay.admin_json = gate, aj
        relay._known = lambda s: s == "jules"
        relay.log = lambda *a: self.logs.append(" ".join(map(str, a)))
        relay.post_station_updates_drop = lambda slug: True

    def tearDown(self):
        (relay.STATE_DIR, relay.admin_gate, relay.admin_json, relay._known, relay.log,
         relay.post_station_updates_drop) = self.saved
        os.chmod(self.top, 0o700)
        shutil.rmtree(self.top)

    def put(self, on):
        self.body = {"on": on}
        req = type("Q", (), {"match_info": {"slug": "jules"}})()
        resp = asyncio.run(relay.admin_updates_put(req))
        return resp.status, json.loads(resp.body)

    def test_switch_on_saves_under_state(self):
        self.assertEqual(self.put(True), (200, {"ok": True, "on": True, "was": False}))
        path = os.path.join(self.top, "state", "updates.json")
        with open(path) as f:
            self.assertEqual(json.load(f)["bots"], {"jules": True})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertTrue(relay.updates_on("jules"))
        self.assertEqual(self.put(False)[0], 200)
        self.assertFalse(relay.updates_on("jules"))

    def test_failed_save_is_logged_without_contents(self):
        os.chmod(os.path.join(self.top, "state"), 0o550)
        try:
            self.assertEqual(self.put(True)[0], 500)
        finally:
            os.chmod(os.path.join(self.top, "state"), 0o770)
        self.assertTrue(any("updates save failed" in l and "PermissionError" in l for l in self.logs), self.logs)


class PathShape(unittest.TestCase):
    def test_not_the_signed_update_status_file_and_under_state(self):
        p = relay.updates_path()
        self.assertNotEqual(os.path.abspath(p), os.path.abspath(relay.UPDATES_FILE))
        self.assertEqual(os.path.dirname(p), os.path.join(relay.STATE_DIR, "state"))

    def test_avatar_auto_file_is_under_state(self):
        # same bug class: the top of STATE_DIR is not writable by grok-relay
        self.assertEqual(os.path.dirname(relay.AVATAR_AUTO_FILE), os.path.join(relay.STATE_DIR, "state"))


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory modes")
class VoicesOfferedInstalledLayout(unittest.TestCase):
    """Same bug class: offered.json was under STATE_DIR/voices, which install.sh never creates."""

    def setUp(self):
        self.top = tempfile.mkdtemp()
        os.mkdir(os.path.join(self.top, "state"), 0o770)
        os.chmod(self.top, 0o555)  # like root:root 0755 as seen by grok-relay
        self.saved = (relay.STATE_DIR, relay.log, relay.ClientSession, set(relay.VOICES), relay.persona_list)
        relay.STATE_DIR = self.top
        self.logs = []
        relay.log = lambda *a: self.logs.append(" ".join(map(str, a)))

        def no_net(*a, **k):
            raise OSError("no network in tests")
        relay.ClientSession = no_net
        relay.persona_list = lambda: {}

    def tearDown(self):
        relay.STATE_DIR, relay.log, relay.ClientSession, voices, relay.persona_list = self.saved
        relay.VOICES.clear()
        relay.VOICES.update(voices)
        os.chmod(self.top, 0o700)
        shutil.rmtree(self.top)

    def test_fetch_writes_offered_and_seed_reads_it_back(self):
        relay.VOICES.clear()
        relay.VOICES.update({"ara", "customvoice"})
        asyncio.run(relay.fetch_voices())
        path = os.path.join(self.top, "state", "voices-offered.json")
        with open(path) as f:
            self.assertEqual(json.load(f)["voices"], ["ara", "customvoice"])
        relay.VOICES.clear()
        self.assertTrue(relay.seed_offered_voices())
        self.assertEqual(relay.VOICES, {"ara", "customvoice"})

    def test_failed_write_is_logged_with_type_and_path_only(self):
        os.chmod(os.path.join(self.top, "state"), 0o550)
        relay.VOICES.clear()
        relay.VOICES.add("ara")
        try:
            asyncio.run(relay.fetch_voices())
        finally:
            os.chmod(os.path.join(self.top, "state"), 0o770)
        hit = [l for l in self.logs if "offered save failed" in l]
        self.assertTrue(hit and "PermissionError" in hit[0] and relay.offered_path() in hit[0], self.logs)
        self.assertNotIn("ara", hit[0])

    def test_offered_path_is_under_state(self):
        self.assertEqual(relay.offered_path(), os.path.join(self.top, "state", "voices-offered.json"))


if __name__ == "__main__":
    unittest.main()
