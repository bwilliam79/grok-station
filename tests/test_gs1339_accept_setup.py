"""0.13.39: Accept on Set up records the pairing release, so Finish update is not "copy".

A bot paired through Set up then Accept, with a webhook and no signed update status, used to fall
through to state "copy" ("Set up before 0.11.8") because bots_setup_pair never called note_setup.
Reject records nothing. Remove clears the stamp, so the same id comes back with no pairing record.
"""
import hashlib, os, sys, unittest

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


if __name__ == "__main__":
    unittest.main()
