#!/usr/bin/env python3
"""0.13.37 regression: Set up is home-only, and a cloud-mode request from before the upgrade is not actionable.

At 0.13.36 the admin poster sent mode "cloud" for any bot whose stored key location was not "home", so a bot that
was removed (or reset) and rescanned got cloud setup details. A cloud request left open by 0.13.36 stayed
actionable after the upgrade: its frozen payload stayed on disk and a key posted on it could still be held for
Accept. Now:
  * the poster sends mode "home" for every bot, with or without a stored key location;
  * the relay refuses any other mode before freezing anything;
  * retire_cloud_setup_requests drops every non-home pending and expires its row (no posted key kept);
  * _pending_doc refuses a non-home pending found later;
  * setup_note_review takes no key on an expired or retired row.
"""
import json, os, shutil, sys, tempfile, time, unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import test_admin as ta  # noqa: E402  (sets the admin env, then imports grok_admin)
import handoff  # noqa: E402
import relay  # noqa: E402

ga = ta.ga
RID_OLD = "vs_" + "c" * 22
RID_NEW = "vs_" + "h" * 22
PUB = "A" * 43 + "="


class _Fake:
    def __init__(self, modes):
        self.sent, self.modes = [], modes

    def send(self, code, body):
        self.sent.append((code, body))

    def bots_unconfigured(self):
        self.sent.append((503, {"error": "unconfigured"}))

    def _setup_ctx(self):
        return {"known": {"rhea", "otto"}, "main": "rhea", "main_name": "Rhea", "outdated": False,
                "names": {"rhea": "Rhea", "otto": "Otto"}, "hosts": {}, "configured": {"rhea"},
                "modes": self.modes}


class AdminPosterSendsHome(unittest.TestCase):
    def setUp(self):
        ta.reset_state()
        self.posts = []
        saved = ga.relay_call

        def fake(method, path, body=None, timeout=None):
            if method == "POST" and path == "/admin/bots/setup-relay":
                self.posts.append(body)
                return (202, {"ok": True})
            if method == "GET" and path == "/admin/bots":
                return (200, {"bots": []})
            return (200, {})
        ga.relay_call = fake
        self.addCleanup(setattr, ga, "relay_call", saved)

    def _send(self, modes):
        h = _Fake(modes)
        ga.Handler.bots_setup_send(h, {"slug": "otto"})
        return h

    def test_no_stored_key_location_gets_home(self):
        # The 0.13.36 bug: a removed and rescanned bot has no key_mode, and the poster fell back to cloud.
        self._send({})
        self.assertEqual([p.get("mode") for p in self.posts], ["home"])

    def test_old_cloud_key_location_still_gets_home(self):
        self._send({"otto": "cloud"})
        self.assertEqual([p.get("mode") for p in self.posts], ["home"])


class RelayState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pending = os.path.join(self.tmp, "pending")
        os.makedirs(self.pending)
        self.state = os.path.join(self.tmp, "setup-relay.json")
        for p in (mock.patch.object(relay, "PENDING_DIR", self.pending),
                  mock.patch.object(relay, "SETUP_STATE_FILE", self.state),
                  mock.patch.object(relay, "log", lambda *a: None)):
            p.start()
            self.addCleanup(p.stop)
        self.now = int(time.time() * 1000)

    def _pending(self, rid, target, mode):
        frozen = {"target": target, "pin_sha256": "ab" * 32}
        if mode is not None:
            frozen["mode"] = mode
        doc = {"request_id": rid, "kind": "setup_relay", "target": target, "persona": "rhea",
               "expires_ms": self.now + 600000, "nonce": "n", "frozen": frozen}
        with open(os.path.join(self.pending, rid + ".json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def _row(self, target, rid, state="sent", **extra):
        st = relay.setup_state_load()
        row = {"state": state, "rid": rid, "at": int(time.time()), "expires_ms": self.now + 600000, "path": "first"}
        row.update(extra)
        st[target] = row
        relay.setup_state_save(st)

    def _retire(self):
        fn = getattr(relay, "retire_cloud_setup_requests", None)
        self.assertIsNotNone(fn, "relay has no retire_cloud_setup_requests")
        return fn()

    def test_relay_refuses_cloud_mode(self):
        self.assertFalse(handoff.setup_field_ok("mode", "cloud"))
        self.assertTrue(handoff.setup_field_ok("mode", "home"))

    def test_retire_drops_cloud_pending_and_expires_row(self):
        self._pending(RID_OLD, "nix", "cloud")
        self._pending(RID_NEW, "otto", "home")
        self._row("nix", RID_OLD, state="review", pending_pub=PUB, fp="0123456789abcdef", computer="den")
        self._row("otto", RID_NEW)
        self.assertEqual(self._retire(), ["nix"])
        self.assertFalse(os.path.exists(os.path.join(self.pending, RID_OLD + ".json")))
        self.assertTrue(os.path.exists(os.path.join(self.pending, RID_NEW + ".json")))
        st = relay.setup_state_load()
        self.assertEqual(st["nix"]["expires_ms"], 0)
        self.assertNotIn("pending_pub", st["nix"])
        self.assertNotIn("fp", st["nix"])
        self.assertEqual(relay.setup_state_public()["nix"]["state"], "expired")
        self.assertEqual(relay.setup_state_public()["otto"]["state"], "sent")

    def test_retire_keeps_a_pending_with_no_mode(self):
        self._pending(RID_OLD, "nix", None)  # not a frozen setup payload from the removed path
        self.assertEqual(self._retire(), [])
        self.assertTrue(os.path.exists(os.path.join(self.pending, RID_OLD + ".json")))

    def test_pending_doc_refuses_cloud_found_later(self):
        self._pending(RID_OLD, "nix", "cloud")
        self.assertIsNone(relay._pending_doc(RID_OLD))
        self.assertFalse(os.path.exists(os.path.join(self.pending, RID_OLD + ".json")))
        self._pending(RID_NEW, "otto", "home")
        self.assertEqual(relay._pending_doc(RID_NEW)["target"], "otto")

    def test_note_review_refuses_a_retired_row(self):
        self._row("nix", RID_OLD, expires_ms=0)
        with mock.patch.object(relay, "_pubkey_fp", lambda pub: "0123456789abcdef"), \
                mock.patch.object(relay.botreply, "_pair_host_ok", lambda c: True), \
                mock.patch.object(relay.botreply, "_PAIR_PUB", type("P", (), {"fullmatch": staticmethod(lambda s: True)})):
            self.assertFalse(relay.setup_note_review(RID_OLD, "nix", PUB, "den"))
            self.assertEqual(relay.setup_state_load()["nix"]["state"], "sent")
            self._row("otto", RID_NEW)
            self.assertTrue(relay.setup_note_review(RID_NEW, "otto", PUB, "den"))


if __name__ == "__main__":
    unittest.main()
