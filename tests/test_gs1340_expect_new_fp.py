"""0.13.40 Nix N1: Accept must name the fingerprint the page showed.

A second tab can post a new key while this tab still shows the old fingerprint.
Accept sends that shown fingerprint as expect_new_fp. The relay, under the setup
lock, accepts only when it equals _pubkey_fp of the stored pending key. A missing
or different value is refused and the page can show the reason. Reject is unchanged.

Fails before the check (a mismatch still pins) and passes after.
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
sys.path.insert(0, os.path.join(ROOT, "admin"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import relay  # noqa: E402
import test_admin  # noqa: E402

ga = test_admin.ga

REASON = "The fingerprint on this page is not the key waiting to be accepted. Reload and compare it again."


def _pub():
    return base64.b64encode(bytes(range(1, 33))).decode()


class RelayExpectNewFp(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.d, ignore_errors=True))
        self.state = os.path.join(self.d, "setup-relay.json")
        self.pending = os.path.join(self.d, "pending")
        os.makedirs(self.pending)
        self.keyf = os.path.join(self.d, "keys.json")
        self.paired = os.path.join(self.d, "paired.json")
        self.pub = _pub()
        self.fp = relay._pubkey_fp(self.pub)
        self.other = "0123456789abcdef"
        self.assertEqual(len(self.fp), 16)
        self.assertNotEqual(self.fp, self.other)
        rid = "vs_" + "e" * 22
        exp = int(time.time() * 1000) + 60000
        # The stored fp field is deliberately not the pending key, so a pass that
        # trusts the field instead of _pubkey_fp(pending_pub) accepts the wrong value.
        row = {"state": "review", "rid": rid, "at": 1, "expires_ms": exp,
               "computer": "kitchen", "pending_pub": self.pub, "fp": self.other, "path": "first"}
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump({"targets": {"otto": row}}, fh)
        self.patches = mock.patch.multiple(
            relay,
            SETUP_STATE_FILE=self.state,
            PENDING_DIR=self.pending,
            PAIRED_FILE=self.paired,
            station_release=lambda: "0.13.40",
            load_overlay_doc=lambda: {"reply_host": {}, "added": {}, "renamed": {},
                                      "hidden": [], "voices": {}, "list_source": "", "key_mode": {}},
            save_overlay=lambda doc: None,
            forget_bot_status=lambda slug: None,
        )
        self.patches.start()
        self.addCleanup(self.patches.stop)
        self._keys = mock.patch.object(relay, "bot_keys_path", lambda: self.keyf)
        self._keys.start()
        self.addCleanup(self._keys.stop)

    def _still_review(self):
        with open(self.state, encoding="utf-8") as fh:
            row = json.load(fh)["targets"]["otto"]
        self.assertEqual(row["state"], "review")
        self.assertEqual(row["pending_pub"], self.pub)
        self.assertFalse(os.path.exists(self.keyf))

    def test_mismatch_is_refused_and_does_not_pin(self):
        got = relay.setup_pair_decide("otto", "accept", expect_old_fp="-", expect_new_fp=self.other)
        self.assertEqual(got.get("fp_stale"), True)
        self.assertNotEqual(got.get("state"), "accepted")
        self._still_review()

    def test_missing_is_refused_and_does_not_pin(self):
        got = relay.setup_pair_decide("otto", "accept", expect_old_fp="-")
        self.assertEqual(got.get("fp_stale"), True)
        self.assertNotEqual(got.get("state"), "accepted")
        self._still_review()
        blank = relay.setup_pair_decide("otto", "accept", expect_old_fp="-", expect_new_fp="")
        self.assertEqual(blank.get("fp_stale"), True)
        self._still_review()

    def test_match_against_the_pending_key_is_accepted(self):
        # Matches _pubkey_fp(pending_pub), not the stored fp field (self.other).
        got = relay.setup_pair_decide("otto", "accept", expect_old_fp="-", expect_new_fp=self.fp)
        self.assertEqual(got.get("state"), "accepted")
        self.assertTrue(os.path.exists(self.keyf))

    def test_reject_ignores_expect_new_fp(self):
        got = relay.setup_pair_decide("otto", "reject")
        self.assertEqual(got["state"], "rejected")
        self.assertFalse(os.path.exists(self.keyf))
        with open(self.state, encoding="utf-8") as fh:
            row = json.load(fh)["targets"]["otto"]
        self.assertNotIn("pending_pub", row)


class AdminExpectNewFp(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._rc = ga.relay_call
        self.reply = (200, {"ok": True, "state": "accepted"})

        def fake(method, path, payload=None, timeout=10):
            self.calls.append((method, path, dict(payload or {})))
            return self.reply

        ga.relay_call = fake

    def tearDown(self):
        ga.relay_call = self._rc

    def call(self, body):
        class H(ga.Handler):
            def __init__(self):
                self.out = []

            def send(self, code, payload):
                self.out.append((code, payload))
                return code

            def need_password(self, j, always=False, session_ok=False):
                return True

        h = H()
        code = ga.Handler.bots_setup_pair(h, body)
        return code, (h.out[-1][1] if h.out else None)

    def test_missing_is_refused_with_the_page_reason(self):
        code, body = self.call({"slug": "otto", "decision": "accept", "expect_old_fp": "-"})
        self.assertNotEqual(code, 200)
        self.assertEqual(body["error"], REASON)
        self.assertEqual(self.calls, [])

    def test_match_is_forwarded_and_reject_omits_it(self):
        code, body = self.call({"slug": "otto", "decision": "accept", "expect_old_fp": "-",
                                 "expect_new_fp": "ABCD 1234 4567 89ef"})
        self.assertEqual(code, 200, body)
        sent = [c[2] for c in self.calls if c[1] == "/admin/bots/setup-pair"][-1]
        self.assertEqual(sent["expect_new_fp"], "abcd1234456789ef")
        self.assertEqual(sent["expect_old_fp"], "-")
        code, body = self.call({"slug": "otto", "decision": "reject"})
        self.assertEqual(code, 200, body)
        sent = [c[2] for c in self.calls if c[1] == "/admin/bots/setup-pair"][-1]
        self.assertNotIn("expect_new_fp", sent)
        self.assertNotIn("expect_old_fp", sent)

    def test_relay_reason_reaches_the_page(self):
        self.reply = (409, {"error": REASON})
        code, body = self.call({"slug": "otto", "decision": "accept", "expect_old_fp": "-",
                                 "expect_new_fp": "0123456789abcdef"})
        self.assertEqual(code, 409)
        self.assertEqual(body["error"], REASON)


class PageSendsExpectNewFp(unittest.TestCase):
    def test_accept_posts_the_displayed_fingerprint_and_shows_the_refusal(self):
        with open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        start = js.index("async function decideSetupPair(")
        fn = js[start:js.index("\nasync function ", start)]
        prog = (
            "const sent = []; const toasts = [];\n"
            "function api(method, url, body) { const copy = JSON.parse(JSON.stringify(body)); sent.push(copy);"
            " if (copy.decision === 'reject') return Promise.resolve({ok:true, status:200, data:{ok:true}});"
            " return Promise.resolve({ok:false, status:409, data:{error:%s}}); }\n"
            "function stepupOpen(){ return true; }\n"
            "function askPassword(){ throw new Error('password'); }\n"
            "function toast(msg, kind){ toasts.push([msg, kind]); }\n"
            "function paintBots(){}\n"
            "function expectOldFp(){ return '-'; }\n"
            "function botBySlug(){ return null; }\n"
            "%s\n"
            "(async () => {\n"
            "  await decideSetupPair({slug:'otto', name:'Otto', relay_fp:'ABCD 1234 4567 89ef'}, {}, 'accept');\n"
            "  await decideSetupPair({slug:'otto', name:'Otto', relay_fp:'abcd1234456789ef'}, {}, 'reject');\n"
            "  process.stdout.write(JSON.stringify({sent: sent, toasts: toasts}));\n"
            "})();\n"
        ) % (json.dumps(REASON), fn)
        import subprocess
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["sent"][0]["expect_new_fp"], "abcd1234456789ef")
        self.assertEqual(got["sent"][0]["expect_old_fp"], "-")
        self.assertEqual(got["sent"][0]["decision"], "accept")
        self.assertNotIn("expect_new_fp", got["sent"][1])
        self.assertEqual(got["toasts"], [[REASON, "bad"]])


if __name__ == "__main__":
    unittest.main()
