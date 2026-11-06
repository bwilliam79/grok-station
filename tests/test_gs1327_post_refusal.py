"""0.13.27: a station post refusal names no key / not paired, switch off, or unreachable (bq-0062).

Gates stay. A missing key does not post. Updates off stays 403 and does not forward the card.
The printed line is a fixed phrase. Secret key material is not in it.
"""
import base64
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest

from nacl.public import PrivateKey, SealedBox
from nacl.signing import SigningKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, rel):
    # gs-seal-reply replaces sys.path with the stdlib on import. Put it back so the relay still imports.
    saved = sys.path[:]
    try:
        loader = importlib.machinery.SourceFileLoader(name, os.path.join(ROOT, rel))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
        loader.exec_module(mod)
        return mod
    finally:
        sys.path[:] = saved


def _seal(station, signer, doc, bot="jules"):
    raw = json.dumps(doc, separators=(",", ":")).encode()
    sealed = SealedBox(station.public_key).encrypt(signer.sign(raw))
    return json.dumps({"v": 1, "bot": bot, "sealed": base64.b64encode(sealed).decode()},
                      separators=(",", ":")).encode()


class HelperPhrases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = _load("gs1327_home", "scripts/gs-seal-reply")
        cls.cloud = _load("gs1327_cloud", "tools/grok-station-reply")

    def test_named_lines_and_unknown_stays_bare(self):
        for mod in (self.home, self.cloud):
            self.assertEqual(mod.refusal_line("nokey"), "refused: no key")
            self.assertEqual(mod.refusal_line("revoked"), "refused: not paired")
            self.assertEqual(mod.refusal_line("killed"), "refused: not paired")
            self.assertEqual(mod.refusal_line("unknown"), "refused: not paired")
            self.assertEqual(mod.refusal_line("updates_off"), "refused: switch off")
            self.assertEqual(mod.refusal_line("unreachable"), "refused: station unreachable")
            self.assertEqual(mod.refusal_line("pii"), "refused: blocked by station PII filter (personal info in reply)")
            self.assertEqual(mod.refusal_line("<script>"), "refused")
            self.assertEqual(mod.reason_from_body(b'{"error":"updates_off"}'), "updates_off")
            self.assertEqual(mod.reason_from_body(b'{"error":"forbidden","reason":"revoked"}'), "revoked")
            self.assertIsNone(mod.reason_from_body(b'{"error":"forbidden"}'))
            self.assertIsNone(mod.reason_from_body(b'{"reason":"sk-secret-key-material"}'))

    def test_missing_key_is_named_and_does_not_open_a_file(self):
        missing = os.path.join(tempfile.mkdtemp(), "no-such-key")
        self.home.key_path = lambda slug: missing
        with self.assertRaises(self.home.Refused) as caught:
            self.home._require_paired_key("kai")
        self.assertEqual(self.home.refusal_line(caught.exception.reason), "refused: no key")
        self.assertNotIn("kai", self.home.refusal_line(caught.exception.reason))

    def test_post_failure_names_the_three_and_keeps_other_refusals_bare(self):
        home = self.home
        self.assertEqual(home.post_failure(0, None), "unreachable")
        self.assertEqual(home.post_failure(403, "updates_off"), "updates_off")
        self.assertEqual(home.post_failure(403, "revoked"), "revoked")
        self.assertIsNone(home.post_failure(500, None))
        self.assertIsNone(home.post_failure(400, "not-a-code"))


class StationGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        import botreply
        import relay
        cls.botreply = botreply
        cls.relay = relay
        cls.station = PrivateKey.generate()
        cls.signer = SigningKey.generate()
        cls.pub = base64.b64encode(bytes(cls.signer.verify_key)).decode()

    def _show(self, keys, known):
        raw = _seal(self.station, self.signer, {"kind": "show", "v": 1})
        posts = []
        status, body, info = self.relay.accept_show(
            raw, self.station, keys, known, self.botreply.Limiter(12, 60),
            lambda show: posts.append(show) or 200, ttl_max=3600)
        return status, body, posts, info

    def test_no_key_is_forbidden_and_named_and_not_forwarded(self):
        seed = base64.b64encode(bytes(self.signer)).decode()
        for keys, known, verdict in (
            ({"killed": False, "keys": {}}, {"jules"}, "revoked"),
            ({"killed": True, "keys": {"jules": self.pub}}, {"jules"}, "killed"),
            ({"killed": False, "keys": {"jules": self.pub}}, set(), "unknown"),
        ):
            status, body, posts, info = self._show(keys, known)
            self.assertEqual(status, 403, verdict)
            self.assertEqual(posts, [], verdict)
            self.assertEqual(body.get("error"), "forbidden", verdict)
            self.assertEqual(body.get("reason"), verdict, verdict)
            self.assertEqual(getattr(info, "verdict", None), verdict)
            blob = json.dumps(body)
            self.assertNotIn(seed, blob)
            self.assertNotIn(self.pub, blob)

    def test_updates_off_stays_a_403_and_does_not_forward(self):
        import time
        nonce = base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")
        doc = {"v": 1, "op": "update", "id": "mail1", "kind": "show", "nonce": nonce,
               "exp_ms": int(time.time() * 1000) + 60_000,
               "update": {"type": "update", "kind": "Mail", "line": "Booked the table",
                          "time": "2026-11-06T12:00:00+00:00"}}
        raw = _seal(self.station, self.signer, doc)
        tmp = tempfile.mkdtemp()
        real = self.relay.updates_path
        self.relay.updates_path = lambda: os.path.join(tmp, "updates.json")
        posts = []
        keys = {"killed": False, "keys": {"jules": self.pub}}
        try:
            status, body, _ = self.relay.accept_show(
                raw, self.station, keys, {"jules"}, self.botreply.Limiter(12, 60),
                lambda show: posts.append(show) or 200, ttl_max=3600)
        finally:
            self.relay.updates_path = real
        self.assertEqual(status, 403)
        self.assertEqual(body, {"error": "updates_off"})
        self.assertEqual(posts, [])
        self.assertNotIn(base64.b64encode(bytes(self.signer)).decode(), json.dumps(body))


if __name__ == "__main__":
    unittest.main()
