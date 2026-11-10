"""0.13.32: a regex-shaped update status skips SSN and card digit checks.

A 16-hex instructions or computer id can contain a 9-digit run the SSN
checker treats as an SSN, so a valid update status was refused as a secret.
Secret, PEM, prefix, and entropy checks still run. Free-text answers, and
an update_bot reply that does not fully match the status pattern, still
block SSN and card numbers.
"""
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import botreply  # noqa: E402
import handoff  # noqa: E402

RID = "vs_" + "e" * 22
# The 0.13.31 line that was refused: instr hash contains the 9-digit run 839678994.
STATUS_BUG = "0.13.31 instr=b839678994eacb57 helper=0.13.30 computer=93205df0855d1aad"
# Other 16-hex values with a 9-digit SSN-shaped run in instr and in computer.
STATUS_OTHER = "0.13.32 instr=a123456789bcdef0 helper=- computer=ff839678994abcde"
SSN_TEXT = "the number is 839-67-8994"
CARD_TEXT = "charge 4111111111111111 please"
SECRET_PREFIX = "password is swordfish " + STATUS_BUG
ALMOST = "applied " + STATUS_BUG


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


class StatusLint(unittest.TestCase):
    def test_exact_status_line_is_accepted_for_update_bot(self):
        self.assertIsNotNone(botreply.UPDATE_STATUS.match(STATUS_BUG))
        self.assertEqual(handoff.lint_reply(STATUS_BUG, pii=False), "ssn")
        got = _go(STATUS_BUG, pii_stored=True, kind="update_bot", live=True)
        self.assertEqual(got["text"], STATUS_BUG)
        self.assertIsNone(handoff.lint_reply(STATUS_BUG, pii=False, digits=False))

    def test_other_hex_ids_with_a_nine_digit_run_are_accepted(self):
        self.assertIsNotNone(botreply.UPDATE_STATUS.match(STATUS_OTHER))
        self.assertEqual(handoff.lint_reply(STATUS_OTHER, pii=False), "ssn")
        got = _go(STATUS_OTHER, pii_stored=True, kind="update_bot", live=True)
        self.assertEqual(got["text"], STATUS_OTHER)

    def test_free_text_ssn_is_still_rejected(self):
        with self.assertRaises(botreply.Reject) as cm:
            _go(SSN_TEXT, live=False)
        self.assertEqual(cm.exception.reason, "secret")
        with self.assertRaises(botreply.Reject) as plain:
            _go(SSN_TEXT, kind="reply", live=False)
        self.assertEqual(plain.exception.reason, "secret")
        self.assertEqual(handoff.lint_reply(SSN_TEXT, pii=False), "ssn")

    def test_card_number_in_a_non_status_answer_is_still_rejected(self):
        with self.assertRaises(botreply.Reject) as cm:
            _go(CARD_TEXT, live=False)
        self.assertEqual(cm.exception.reason, "secret")
        self.assertEqual(handoff.lint_reply(CARD_TEXT, pii=False), "card")

    def test_update_bot_text_that_is_not_a_status_line_is_still_rejected(self):
        self.assertIsNone(botreply.UPDATE_STATUS.match(ALMOST))
        with self.assertRaises(botreply.Reject) as cm:
            _go(ALMOST, kind="update_bot", live=False)
        self.assertEqual(cm.exception.reason, "secret")

    def test_secret_prefix_on_a_status_line_cannot_sneak_through(self):
        self.assertIsNone(botreply.UPDATE_STATUS.match(SECRET_PREFIX))
        with self.assertRaises(botreply.Reject) as cm:
            _go(SECRET_PREFIX, kind="update_bot", live=False)
        self.assertEqual(cm.exception.reason, "secret")
        self.assertEqual(handoff.lint_reply(SECRET_PREFIX, pii=False), "secret")


if __name__ == "__main__":
    unittest.main()
