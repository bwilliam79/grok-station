"""0.13.30: PII lint follows the live setting, and an update status line skips PII lint.

A valid status line is `0.13.29 instr=<16 hex> helper=<ver> computer=<16 hex>`. The word
"helper" in that line matches a private host of the same name, and update_bot pending
files are written with pii_mode defaulting to true, so the station reports "pii" even
after the admin setting is off.
"""
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import botreply  # noqa: E402
import handoff  # noqa: E402
import relay  # noqa: E402

RID = "vs_" + "d" * 22
STATUS = "0.13.29 instr=0123456789abcdef helper=0.13.29 computer=fedcba9876543210"
SECRET = "the password is swordfish"


def _pending(pii=True, kind=""):
    doc = {"request_id": RID, "persona": "sol", "nonce": "n" * 16,
           "expires_ms": int(time.time() * 1000) + 60000, "pii_mode": pii}
    if kind:
        doc["kind"] = kind
    return doc


def _answer(text):
    return {"action_class": "answer", "kind": "reply", "request_id": RID, "text": text, "tier": 0}


def _go(text, pii_stored=True, kind="", hosts=(), names=(), live=None):
    pend = _pending(pii_stored, kind)
    kw = {}
    if live is not None:
        kw["pii"] = live
    return botreply._accept_answer(_answer(text), "sol", (lambda _rid: pend), set(), hosts, names,
                                   int(time.time() * 1000), **kw)


class LivePii(unittest.TestCase):
    def test_status_line_collides_with_the_word_helper(self):
        self.assertIsNotNone(relay.UPDATE_STATUS.match(STATUS))
        self.assertEqual(handoff.lint_reply(STATUS, hosts=("helper",), names=(), pii=True), "host")
        self.assertIsNone(handoff.lint_reply(STATUS, hosts=("helper",), names=(), pii=False))

    def test_matrix(self):
        # Text reply: live off lets a household name through even if the pending file says on.
        got = _go("see ada tonight", pii_stored=True, names=("ada",), live=False)
        self.assertEqual(got["text"], "see ada tonight")
        with self.assertRaises(botreply.Reject) as on:
            _go("see ada tonight", pii_stored=False, names=("ada",), live=True)
        self.assertEqual(on.exception.reason, "name")
        # Secrets stay blocked with the setting on or off.
        for live in (False, True):
            with self.assertRaises(botreply.Reject) as sec:
                _go(SECRET, pii_stored=False, live=live)
            self.assertEqual(sec.exception.reason, "secret")
        # Update status skips PII lint. A host named helper must not refuse the line.
        for live in (False, True):
            got = _go(STATUS, pii_stored=True, kind="update_bot", hosts=("helper",), live=live)
            self.assertTrue(got["text"].startswith("0.13.29 "))
        with self.assertRaises(botreply.Reject) as bad:
            _go("helper " + SECRET, pii_stored=True, kind="update_bot", hosts=("helper",), live=False)
        self.assertEqual(bad.exception.reason, "secret")
        # bot_list uses the live flag, not a hardcoded on.
        doc = {"action_class": "bot_list", "kind": "bot_list", "request_id": RID, "tier": 0,
               "bots": [{"name": "Ada"}]}
        pend = _pending(True, "bot_list")
        ok = botreply._accept_list(doc, "sol", (lambda _rid: pend), set(), (), ("ada",),
                                   int(time.time() * 1000), "sol", pii=False)
        self.assertEqual(ok["names"], ["Ada"])
        with self.assertRaises(botreply.Reject) as listed:
            botreply._accept_list(doc, "sol", (lambda _rid: pend), set(), (), ("ada",),
                                  int(time.time() * 1000), "sol", pii=True)
        self.assertEqual(listed.exception.reason, "name")
        # Feed put and feed update already take the live flag. Off lets personal info through.
        title = {"op": "put", "card": {"type": "calendar", "events": [{"title": "ada at noon"}]}}
        self.assertIsNone(relay.lint_show(title, names=("ada",), pii=False))
        self.assertEqual(relay.lint_show(title, names=("ada",), pii=True), "name")
        self.assertEqual(relay.lint_show(
            {"op": "put", "card": {"type": "calendar", "events": [{"title": SECRET}]}}, pii=False), "secret")
        upd = {"op": "update", "update": {"line": "ada checked in", "kind": "note"}}
        self.assertIsNone(relay.lint_show(upd, names=("ada",), pii=False))
        self.assertEqual(relay.lint_show(upd, names=("ada",), pii=True), "name")
        self.assertIsNone(relay.lint_update_texts(["ada checked in"], names=("ada",), pii=False))
        self.assertEqual(relay.lint_update_texts(["ada checked in"], names=("ada",), pii=True), "name")
        self.assertEqual(relay.lint_update_texts([SECRET], pii=False), "secret")

    def test_host_hit_is_not_called_pii(self):
        with self.assertRaises(botreply.Reject) as cm:
            _go("the helper is on", hosts=("helper",), live=True)
        self.assertEqual(cm.exception.reason, "host")
        self.assertNotEqual(cm.exception.reason, "pii")

    def test_lint_exception_is_not_called_pii(self):
        real = handoff.lint_reply

        def boom(*a, **k):
            raise RuntimeError("checker")

        handoff.lint_reply = boom
        try:
            with self.assertRaises(botreply.Reject) as cm:
                _go("hello", live=True)
        finally:
            handoff.lint_reply = real
        self.assertEqual(cm.exception.reason, "lint")
        self.assertNotEqual(cm.exception.reason, "pii")

    def test_real_request_follows_the_live_setting(self):
        # Stored on, live off: turning the filter off is honored.
        got = _go("see ada tonight", pii_stored=True, names=("ada",), live=False)
        self.assertEqual(got["text"], "see ada tonight")
        # Stored off, live on: a mid-flight flip to on takes effect.
        with self.assertRaises(botreply.Reject) as flipped:
            _go("see ada tonight", pii_stored=False, names=("ada",), live=True)
        self.assertEqual(flipped.exception.reason, "name")

    def test_test_pending_keeps_its_stored_flag(self):
        off = _pending(False)
        off["test"] = True
        got = botreply._accept_answer(_answer("see ada tonight"), "sol", (lambda _rid: off), set(), (), ("ada",),
                                      int(time.time() * 1000), pii=True)
        self.assertEqual(got["text"], "see ada tonight")
        on = _pending(True)
        on["test"] = True
        with self.assertRaises(botreply.Reject) as blocked:
            botreply._accept_answer(_answer("see ada tonight"), "sol", (lambda _rid: on), set(), (), ("ada",),
                                    int(time.time() * 1000), pii=False)
        self.assertEqual(blocked.exception.reason, "name")

    def test_update_pending_stores_the_live_flag(self):
        src = open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8").read()
        for kind in ('kind="update_bot"', 'kind="update_helper"', 'kind="bot_list"'):
            i = src.index(kind)
            window = src[max(0, i - 500):i + 40]
            self.assertIn("pii_mode=pii_mode()", window, kind)


if __name__ == "__main__":
    unittest.main()
