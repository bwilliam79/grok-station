"""0.13.29: a refused station feed post names a content-free reason.

The relay's /voice/show answer carries one fixed code per refusal: schema, lint, rate,
busy, unavailable, expired, signature, duplicate (plus the existing revoked and
updates_off). The home helper and the cloud post tool turn each code into one fixed line,
so a bare "refused" no longer hides the cause. The codes are a closed set: no card text,
key, nonce or verdict detail reaches the body or the printed line.
"""
import asyncio
import base64
import importlib.machinery
import importlib.util
import json
import os
import sys
import time
import unittest

from nacl.public import PrivateKey, SealedBox
from nacl.signing import SigningKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_gs130_home_flow as thf  # noqa: E402  (imports relay with the shared test env)

relay, _home = thf.load_mods()
import botreply  # noqa: E402

# Lint bait: the relay content filter refuses a key-shaped title. The bait is joined at run
# time so no committed line holds key-shaped text for the repo leak gates to flag.
LINT_BAIT = "".join(("s", "k-", "test-", "abcdef1234"))

SHOW_CODES = {"schema", "lint", "rate", "busy", "unavailable", "expired", "signature", "duplicate"}


def _load(name, rel):
    saved = sys.path[:]
    try:
        loader = importlib.machinery.SourceFileLoader(name, os.path.join(ROOT, rel))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
        loader.exec_module(mod)
        return mod
    finally:
        sys.path[:] = saved


def _nonce():
    return base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")


def _put(title="Weeknight soup", exp_ms=None):
    card = {"v": 1, "type": "recipe", "title": title, "servings": 2,
            "ingredients": ["onion", "stock"], "steps": ["Chop the onion.", "Simmer."], "step": 1}
    return {"v": 1, "op": "put", "id": "soup", "priority": 10, "ttl_s": 3600, "card": card,
            "kind": "show", "nonce": _nonce(),
            "exp_ms": int(time.time() * 1000) + 60_000 if exp_ms is None else exp_ms}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.station = PrivateKey.generate()
        cls.signer = SigningKey.generate()
        cls.seed = base64.b64encode(bytes(cls.signer)).decode()
        cls.keys = {"killed": False, "keys": {"jules": base64.b64encode(bytes(cls.signer.verify_key)).decode()}}
        cls.known = {"jules"}
        cls.home = _load("gs1329_home", "scripts/gs-seal-reply")
        cls.cloud = _load("gs1329_cloud", "tools/grok-station-reply")

    def seal(self, doc, signer=None, station=None):
        raw = json.dumps(doc, separators=(",", ":")).encode()
        pub = (station or self.station).public_key
        sealed = SealedBox(pub).encrypt((signer or self.signer).sign(raw))
        return json.dumps({"v": 1, "bot": "jules", "sealed": base64.b64encode(sealed).decode()},
                          separators=(",", ":")).encode()

    def accept(self, raw, limiter=None, post=None, seen=None):
        return relay.accept_show(raw, self.station, self.keys, self.known,
                                 limiter or botreply.Limiter(30, 60),
                                 post or (lambda show: 200), ttl_max=3600, seen=seen)

    def assert_clean(self, body, *secrets):
        blob = json.dumps(body)
        for s in (self.seed,) + secrets:
            self.assertNotIn(s, blob)
        self.assertLessEqual(set(body), {"error", "reason"})


class RelayReasons(Base):
    def test_schema(self):
        doc = _put()
        doc["card"]["unknown_field"] = "x"
        status, body, info = self.accept(self.seal(doc))
        self.assertEqual((status, body.get("reason")), (400, "schema"))
        self.assert_clean(body)

    def test_lint(self):
        status, body, info = self.accept(self.seal(_put(title=LINT_BAIT)))
        self.assertEqual((status, body.get("reason")), (422, "lint"))
        self.assert_clean(body, LINT_BAIT)

    def test_rate(self):
        lim = botreply.Limiter(1, 60)
        self.assertEqual(self.accept(self.seal(_put()), limiter=lim)[0], 200)
        status, body, info = self.accept(self.seal(_put()), limiter=lim)
        self.assertEqual((status, body.get("reason")), (429, "rate"))
        self.assertEqual(info.verdict, "rate")

    def test_busy_when_the_seen_set_is_full(self):
        import tempfile
        store = botreply.ShowSeen(os.path.join(tempfile.mkdtemp(prefix="gs1329-"), "seen.json"), max_n=1)
        now = int(time.time() * 1000)
        self.assertTrue(store.reserve("other", "x" * 22, now + 100000, now))
        status, body, info = self.accept(self.seal(_put()), seen=store)
        self.assertEqual((status, body.get("reason")), (503, "busy"))

    def test_unavailable_when_the_display_does_not_take_it(self):
        status, body, info = self.accept(self.seal(_put()), post=lambda show: 503)
        self.assertEqual((status, body.get("reason")), (503, "unavailable"))
        status, body, info = self.accept(self.seal(_put()), post=lambda show: 413)
        self.assertEqual((status, body.get("reason")), (400, "schema"))

    def test_expired(self):
        status, body, info = self.accept(self.seal(_put(exp_ms=int(time.time() * 1000) - 1000)))
        self.assertEqual((status, body.get("reason")), (403, "expired"))

    def test_signature_and_unseal_share_one_code(self):
        status, body, info = self.accept(self.seal(_put(), signer=SigningKey.generate()))
        self.assertEqual((status, body.get("reason"), info.verdict), (403, "signature", "signature"))
        status, body, info = self.accept(self.seal(_put(), station=PrivateKey.generate()))
        self.assertEqual((status, body.get("reason"), info.verdict), (403, "signature", "unseal"))

    def test_duplicate(self):
        seen = set()
        raw = self.seal(_put())
        self.assertEqual(self.accept(raw, seen=seen)[0], 200)
        status, body, info = self.accept(raw, seen=seen)
        self.assertEqual((status, body.get("reason")), (409, "duplicate"))

    def test_existing_codes_unchanged(self):
        keys = {"killed": False, "keys": {}}
        status, body, info = relay.accept_show(self.seal(_put()), self.station, keys, self.known,
                                               botreply.Limiter(30, 60), lambda s: 200, ttl_max=3600)
        self.assertEqual((status, body), (403, {"error": "forbidden", "reason": "revoked"}))


class VoiceShowHandler(Base):
    """The aiohttp handler passes the code through, including on 503 (it used to drop it)."""

    class Req:
        def __init__(self, raw):
            self._raw, self.content_length = raw, len(raw)

        async def read(self):
            return self._raw

    def call(self, result):
        saved = {}
        stubs = {
            "accept_show": lambda *a, **k: result,
            "audit_reply": lambda *a, **k: None,
            "bot_keys_path": lambda: "/nonexistent",
            "known_bot_slugs": lambda: set(),
            "card_limiter": lambda: None,
            "card_ttl_max": lambda: 3600,
            "show_seen": lambda: None,
            "pii_mode": lambda: True,
        }
        for name, fn in stubs.items():
            saved[name] = getattr(relay, name)
            setattr(relay, name, fn)
        saved_load = (botreply.load_or_create_station, botreply.load_keys)
        botreply.load_or_create_station = lambda d: self.station
        botreply.load_keys = lambda p: {}
        try:
            resp = asyncio.run(relay.voice_show(self.Req(b"{}")))
        finally:
            for name, fn in saved.items():
                setattr(relay, name, fn)
            botreply.load_or_create_station, botreply.load_keys = saved_load
        return resp.status, json.loads(resp.body)

    def test_503_keeps_its_reason(self):
        rej = botreply.Reject(503, "busy", "jules")
        self.assertEqual(self.call((503, {"error": "unavailable", "reason": "busy"}, rej)),
                         (503, {"error": "unavailable", "reason": "busy"}))
        rej = botreply.Reject(503, "error", "jules")
        self.assertEqual(self.call((503, {"error": "unavailable", "reason": "unavailable"}, rej)),
                         (503, {"error": "unavailable", "reason": "unavailable"}))

    def test_4xx_reasons_pass_and_unknown_codes_are_dropped(self):
        for status, code in ((400, "schema"), (422, "lint"), (429, "rate"), (403, "expired"),
                             (403, "signature"), (409, "duplicate"), (403, "revoked")):
            got = self.call((status, {"error": "x", "reason": code}, botreply.Reject(status, code, "jules")))
            self.assertEqual(got[0], status)
            self.assertEqual(got[1].get("reason"), code, code)
        got = self.call((400, {"error": "x", "reason": "lint-bait-not-a-key"}, botreply.Reject(400, "schema", "jules")))
        self.assertNotIn("reason", got[1])
        self.assertNotIn("lint-bait-not-a-key", json.dumps(got[1]))

    def test_updates_off_unchanged(self):
        self.assertEqual(self.call((403, {"error": "updates_off"}, botreply.Reject(403, "updates_off", "jules"))),
                         (403, {"error": "updates_off"}))


class HelperLines(Base):
    def test_each_code_has_its_own_fixed_line(self):
        for mod in (self.home, self.cloud):
            lines = {code: mod.refusal_line(code) for code in SHOW_CODES}
            for code, line in lines.items():
                self.assertTrue(line.startswith("refused: "), (code, line))
                self.assertTrue(all(32 <= ord(c) < 127 for c in line), line)
            self.assertEqual(mod.refusal_line("lint"), "refused: blocked by station content filter")
            self.assertEqual(mod.refusal_line("rate"), "refused: too many posts")
            self.assertEqual(mod.refusal_line("busy"), "refused: station busy")
            self.assertEqual(mod.refusal_line("unavailable"), "refused: station could not show it")
            self.assertEqual(mod.refusal_line("signature"), "refused: signature not accepted")
            self.assertEqual(mod.refusal_line("schema"), "refused: reply format not accepted")
            # existing codes keep their lines
            self.assertEqual(mod.refusal_line("updates_off"), "refused: switch off")
            self.assertEqual(mod.refusal_line("revoked"), "refused: not paired")
            self.assertEqual(mod.refusal_line("unreachable"), "refused: station unreachable")
            self.assertEqual(mod.refusal_line("not-a-code"), "refused")

    def test_relay_body_maps_end_to_end(self):
        cases = (
            (lambda: self.accept(self.seal(dict(_put(), card={"v": 1}))), "refused: reply format not accepted"),
            (lambda: self.accept(self.seal(_put(title=LINT_BAIT))), "refused: blocked by station content filter"),
            (lambda: self.accept(self.seal(_put()), post=lambda s: 503), "refused: station could not show it"),
        )
        for run, want in cases:
            status, body, _ = run()
            raw = json.dumps(body).encode()
            for mod in (self.home, self.cloud):
                self.assertEqual(mod.refusal_line(mod.reason_from_body(raw)), want, (mod.__name__, body))
        lim = botreply.Limiter(1, 60)
        self.accept(self.seal(_put()), limiter=lim)
        status, body, _ = self.accept(self.seal(_put()), limiter=lim)
        self.assertEqual(self.home.refusal_line(self.home.post_failure(status, self.home.reason_from_body(
            json.dumps(body).encode()))), "refused: too many posts")

    def test_older_station_without_a_reason_still_gets_a_line_from_the_status(self):
        home = self.home
        self.assertEqual(home.post_failure(429, None), "rate")
        self.assertEqual(home.post_failure(422, None), "lint")
        self.assertEqual(home.post_failure(503, None), "unavailable")
        self.assertEqual(home.post_failure(422, "pii"), "pii")  # a spoken-reply PII block keeps its own line
        self.assertIsNone(home.post_failure(500, None))
        self.assertIsNone(home.post_failure(403, None))
        self.assertEqual(home.reason_from_body(b'{"error":"lint"}'), "lint")

    def test_reason_text_is_never_echoed(self):
        for mod in (self.home, self.cloud):
            self.assertIsNone(mod.reason_from_body(b'{"error":"bad request","reason":"lint-bait-not-a-key"}'))
            self.assertEqual(mod.refusal_line(mod.reason_from_body(b'{"reason":"Weeknight soup"}')), "refused")


if __name__ == "__main__":
    unittest.main()
