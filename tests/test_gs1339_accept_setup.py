"""0.13.39: Accept on Set up records the pairing release, so Finish update is not "copy".

A bot paired through Set up then Accept, with a webhook and no signed update status, used to fall
through to state "copy" ("Set up before 0.11.8") because bots_setup_pair never called note_setup.
Reject records nothing. Remove clears the stamp, so the same id comes back with no pairing record.
"""
import base64, hashlib, json, os, sys, tempfile, time, unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_finish_update as fin  # noqa: E402


def _ga():
    return fin._ga()


class _H:
    def __init__(self, ga):
        self.ga = ga
        self.out = []

    def send(self, code, body):
        self.out.append((code, body))
        return code

    def need_password(self, j, always=False, session_ok=False):
        return True


class AcceptRecordsSetup(unittest.TestCase):
    """Accept with a webhook and no signed status is done, not copy. Fails on 0.13.38."""

    def setUp(self):
        self.ga = _ga()
        self.finish_path = os.path.join(os.environ["ADMIN_STATE_DIR"], "finish-update.json")
        self._had = open(self.finish_path).read() if os.path.exists(self.finish_path) else None
        if os.path.exists(self.finish_path):
            os.remove(self.finish_path)
        self._rc = self.ga.relay_call
        self._ver = self.ga.VERSION_FILE
        self.ga.VERSION_FILE = os.path.join(ROOT, "VERSION")
        self.reply = (200, {"ok": True, "state": "accepted"})

        def fake(method, path, payload=None, timeout=10):
            if path == "/admin/bots/setup-pair":
                return self.reply
            return (200, {"ok": True, "bots": [], "targets": {}})

        self.ga.relay_call = fake
        with open(fin.SCRIPT, "rb") as fh:
            script_sha = hashlib.sha256(fh.read()).hexdigest()
        self.pins = {"commit": fin.U, "update_commit": fin.C,
                     "files": {"scripts/gs-update.sh": script_sha}}

    def tearDown(self):
        self.ga.relay_call = self._rc
        self.ga.VERSION_FILE = self._ver
        if self._had is None:
            if os.path.exists(self.finish_path):
                os.remove(self.finish_path)
        else:
            open(self.finish_path, "w").write(self._had)

    def pair(self, slug, decision):
        h = _H(self.ga)
        body = {"slug": slug, "decision": decision, "password": "secret-secret-1"}
        if decision == "accept":
            body["expect_old_fp"] = "-"
        # Bind the real method so a subclass is not required. need_password is on the instance.
        code = self.ga.Handler.bots_setup_pair(h, body)
        return code, (h.out[-1][1] if h.out else None)

    def row(self, slug):
        bot = {"slug": slug, "name": slug.title(), "paired": True, "webhook": {"configured": True},
               "reply_host": "den", "update": None}
        view = self.ga.finish_rows([bot], "https://st.example", fin.FP, fin.PUB, self.pins)
        return [r for g in view["computers"] for r in g["bots"] if r["slug"] == slug][0]

    def test_accept_with_webhook_and_no_status_is_done_not_copy(self):
        code, body = self.pair("nix", "accept")
        self.assertEqual(code, 200, body)
        row = self.row("nix")
        self.assertNotEqual(row["state"], "copy", row)
        self.assertEqual(row["state"], "done", row)
        stamp = (self.ga.read_finish().get("setup") or {}).get("nix") or {}
        self.assertEqual(stamp.get("release"), self.ga.version())

    def test_accept_stores_a_frozen_release_from_the_relay(self):
        self.reply = (200, {"ok": True, "state": "accepted", "release": "0.13.20"})
        code, body = self.pair("otto", "accept")
        self.assertEqual(code, 200, body)
        stamp = (self.ga.read_finish().get("setup") or {}).get("otto") or {}
        self.assertEqual(stamp.get("release"), "0.13.20")
        self.assertNotEqual(self.row("otto")["state"], "copy")

    def test_reject_records_nothing(self):
        code, body = self.pair("kai", "reject")
        self.assertEqual(code, 200, body)
        self.assertNotIn("kai", self.ga.read_finish().get("setup") or {})

    def test_remove_clears_the_stamp_so_the_bot_is_copy_again(self):
        self.assertEqual(self.pair("pip", "accept")[0], 200)
        self.assertEqual(self.row("pip")["state"], "done")
        self.ga.forget_bot_admin("pip")
        self.assertNotIn("pip", self.ga.read_finish().get("setup") or {})
        self.assertEqual(self.row("pip")["state"], "copy")


class RelayAcceptRelease(unittest.TestCase):
    """The relay returns the frozen release, and refuses Accept when the review row has no fingerprint."""

    def test_missing_fp_is_refused_and_a_frozen_release_is_returned(self):
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        import relay
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        state = os.path.join(d, "setup-relay.json")
        pending = os.path.join(d, "pending")
        os.makedirs(pending)
        keyf = os.path.join(d, "keys.json")
        paired = os.path.join(d, "paired.json")
        pub = base64.b64encode(bytes(range(1, 33))).decode()
        fp = relay._pubkey_fp(pub)
        self.assertEqual(len(fp), 16)
        rid = "vs_" + "c" * 22
        exp = int(time.time() * 1000) + 60000
        row = {"state": "review", "rid": rid, "at": 1, "expires_ms": exp,
               "computer": "kitchen", "pending_pub": pub, "fp": fp, "path": "first"}
        bare = "vs_" + "d" * 22
        nofp = {"state": "review", "rid": bare, "at": 1, "expires_ms": exp,
                "computer": "kitchen", "pending_pub": pub, "path": "first"}
        with open(state, "w", encoding="utf-8") as fh:
            json.dump({"targets": {"otto": row, "kai": nofp}}, fh)
        with open(os.path.join(pending, rid + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"request_id": rid, "kind": "setup_relay", "target": "otto",
                       "frozen": {"release": "0.13.20", "mode": "home", "target": "otto", "request_id": rid}}, fh)
        overlay = {"reply_host": {}, "added": {}, "renamed": {}, "hidden": [], "voices": {},
                   "list_source": "", "key_mode": {}}
        with mock.patch.object(relay, "SETUP_STATE_FILE", state), \
                mock.patch.object(relay, "PENDING_DIR", pending), \
                mock.patch.object(relay, "bot_keys_path", lambda: keyf), \
                mock.patch.object(relay, "PAIRED_FILE", paired), \
                mock.patch.object(relay, "station_release", lambda: "0.13.38"), \
                mock.patch.object(relay, "load_overlay_doc", lambda: overlay), \
                mock.patch.object(relay, "save_overlay", lambda doc: None), \
                mock.patch.object(relay, "forget_bot_status", lambda slug: None):
            self.assertIsNone(relay.setup_pair_decide("kai", "accept", expect_old_fp="-"))
            self.assertFalse(os.path.exists(keyf))
            rejected = relay.setup_pair_decide("kai", "reject")
            self.assertEqual(rejected["state"], "rejected")
            got = relay.setup_pair_decide("otto", "accept", expect_old_fp="-")
        self.assertEqual(got["state"], "accepted")
        self.assertEqual(got.get("release"), "0.13.20")
        self.assertNotEqual(got.get("release"), "0.13.38")


if __name__ == "__main__":
    unittest.main()
