#!/usr/bin/env python3
"""Sealed bot replies: signature, schema, pending binding, rate, stats, revoke, and the bot helper."""
import base64, importlib.machinery, importlib.util, io, json, os, ssl, stat, sys, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.signing import SigningKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import botreply  # noqa: E402
import handoff  # noqa: E402

_helper_path = os.path.join(ROOT, "tools", "grok-station-reply")
_loader = importlib.machinery.SourceFileLoader("grok_station_reply", _helper_path)
_spec = importlib.util.spec_from_loader("grok_station_reply", _loader)
helper = importlib.util.module_from_spec(_spec)
_loader.exec_module(helper)

_UNSET = object()
RID = "vs_" + "a" * 22
PUB = base64.b64encode(b"\x11" * 32).decode()


def _b64(raw):
    return base64.b64encode(raw).decode()


def _seal(station, signer, message, bot="jules", station_pub=None):
    signed = signer.sign(message) if isinstance(message, (bytes, bytearray)) else signer.sign(message.encode())
    pub = station.public_key if station_pub is None else station_pub
    sealed = SealedBox(pub).encrypt(signed)
    return json.dumps({"v": 1, "bot": bot, "sealed": _b64(sealed)}, separators=(",", ":")).encode()


def _reply_bytes(rid=RID, text="Tacos on Tuesday.", **extra):
    body = {"action_class": "answer", "kind": "reply", "request_id": rid, "text": text, "tier": 0}
    body.update(extra)
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()


class Check(unittest.TestCase):
    def setUp(self):
        self.station = PrivateKey.generate()
        self.signer = SigningKey.generate()
        self.other = SigningKey.generate()
        self.keys = {"killed": False, "keys": {"jules": _b64(bytes(self.signer.verify_key)),
                                               "hank": _b64(bytes(self.other.verify_key))}}
        self.known = {"jules", "hank"}
        self.seen = set()
        self.pending = {"request_id": RID, "persona": "jules", "nonce": "n0", "expires_ms": int(time.time() * 1000) + 60000,
                        "pii_mode": True}
        self.limiter = botreply.Limiter(6, 30)
        self.now = int(time.time() * 1000)

    def _go(self, raw, pending=_UNSET, seen=None, keys=None, known=None, limiter=None, now_ms=None, rate_now=None):
        # pending=None is a missing request. Omitting it uses the open pending file from setUp.
        looked = self.pending if pending is _UNSET else pending
        return botreply.ingest(raw, self.station, self.keys if keys is None else keys, self.known if known is None else known,
                               (lambda _rid: looked), self.seen if seen is None else seen,
                               self.limiter if limiter is None else limiter, now_ms=self.now if now_ms is None else now_ms,
                               rate_now=rate_now)

    def _ok(self, text="Tacos on Tuesday.", **kw):
        raw = _seal(self.station, self.signer, _reply_bytes(text=text))
        return self._go(raw, **kw)

    def test_helper_round_trip_verifies(self):
        seed = _b64(bytes(self.signer))
        raw = helper.build_outer("jules", RID, "Tacos on Tuesday.", seed, botreply.station_pub_b64(self.station))
        got = self._go(raw)
        self.assertEqual(got["text"], "Tacos on Tuesday.")
        self.assertEqual(got["request_id"], RID)
        self.assertEqual((got["tier"], got["action_class"], got["nonce"]), (0, "answer", "n0"))
        self.assertNotIn(RID, self.seen)  # ingest does not consume the request

    def test_signature_reject_and_wrong_bot_key(self):
        signed = self.other.sign(b"not-json")
        sealed = SealedBox(self.station.public_key).encrypt(signed)
        raw = json.dumps({"v": 1, "bot": "jules", "sealed": _b64(sealed)}).encode()
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw)
        self.assertEqual((cm.exception.status, cm.exception.verdict), (403, "signature"))
        raw = _seal(self.station, self.other, _reply_bytes())  # hank's key, pinned for jules is the other one
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw)
        self.assertEqual(cm.exception.verdict, "signature")
        flipped = bytearray(self.signer.sign(_reply_bytes()))
        flipped[0] ^= 0x01
        sealed = SealedBox(self.station.public_key).encrypt(bytes(flipped))
        raw = json.dumps({"v": 1, "bot": "jules", "sealed": _b64(sealed)}).encode()
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw)
        self.assertEqual(cm.exception.verdict, "signature")

    def test_signature_is_checked_before_json(self):
        raw = _seal(self.station, self.signer, b"not-json")
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw)
        self.assertEqual((cm.exception.status, cm.exception.verdict), (400, "schema"))

    def test_unseal_failure(self):
        other = PrivateKey.generate()
        raw = _seal(self.station, self.signer, _reply_bytes(), station_pub=other.public_key)
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw)
        self.assertEqual((cm.exception.status, cm.exception.verdict), (403, "unseal"))

    def test_unknown_revoked_expired_duplicate_and_request(self):
        raw = _seal(self.station, self.signer, _reply_bytes())
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw, known=set())
        self.assertEqual(cm.exception.verdict, "unknown")
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw, keys={"killed": False, "keys": {}})
        self.assertEqual(cm.exception.verdict, "revoked")
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw, pending=None)
        self.assertEqual(cm.exception.verdict, "request")
        other = dict(self.pending, persona="hank")
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw, pending=other)
        self.assertEqual(cm.exception.verdict, "request")
        stale = dict(self.pending, expires_ms=self.now - 1)
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw, pending=stale)
        self.assertEqual((cm.exception.status, cm.exception.verdict), (403, "expired"))
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw, seen={RID}, pending=None)
        self.assertEqual((cm.exception.status, cm.exception.verdict), (409, "duplicate"))

    def test_schema_rejects_extra_tier_and_long_text(self):
        raw = _seal(self.station, self.signer, _reply_bytes(extra=1))
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw)
        self.assertEqual(cm.exception.verdict, "schema")
        body = {"action_class": "answer", "request_id": RID, "text": "ok", "tier": 1}
        raw = _seal(self.station, self.signer, json.dumps(body, sort_keys=True).encode())
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw)
        self.assertEqual(cm.exception.verdict, "schema")
        raw = _seal(self.station, self.signer, _reply_bytes(text="x" * 401))
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw)
        self.assertEqual(cm.exception.verdict, "schema")
        self.assertEqual(self._ok("  hello   there ")["text"], "hello there")
        swapped = json.loads(_reply_bytes())
        swapped["kind"] = "show"
        raw = _seal(self.station, self.signer, json.dumps(swapped, separators=(",", ":"), sort_keys=True).encode())
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw)
        self.assertEqual(cm.exception.verdict, "schema")

    def test_lint_blocks_a_token_and_fails_closed(self):
        raw = _seal(self.station, self.signer, _reply_bytes(text="token is abcdefghijklmnop"))
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw)
        self.assertEqual((cm.exception.status, cm.exception.verdict), (400, "lint"))
        real = handoff.lint_reply

        def boom(*_a, **_k):
            raise RuntimeError("nope")

        handoff.lint_reply = boom
        try:
            with self.assertRaises(botreply.Reject) as cm:
                self._ok("plain words")
            self.assertEqual(cm.exception.verdict, "lint")
        finally:
            handoff.lint_reply = real

    def test_kill_switch_and_unknown_do_not_spend_the_bucket(self):
        lim = botreply.Limiter(1, 30)
        raw = _seal(self.station, self.signer, _reply_bytes())
        killed = {"killed": True, "keys": self.keys["keys"]}
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw, keys=killed, limiter=lim, rate_now=0)
        self.assertEqual(cm.exception.verdict, "killed")
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw, known=set(), limiter=lim, rate_now=0)
        self.assertEqual(cm.exception.verdict, "unknown")
        self.assertEqual(self._go(raw, limiter=lim, rate_now=0)["text"], "Tacos on Tuesday.")
        junk = json.dumps({"v": 1, "bot": "jules", "sealed": _b64(b"not-a-box")}).encode()
        with self.assertRaises(botreply.Reject) as cm:
            self._go(junk, limiter=lim, rate_now=0)
        self.assertEqual((cm.exception.status, cm.exception.verdict), (429, "rate"))

    def test_rate_limit_per_bot_and_global(self):
        per = botreply.Limiter(6, 100)
        for i in range(6):
            self.assertTrue(per.allow("jules", now=0))
        self.assertFalse(per.allow("jules", now=0))
        self.assertTrue(per.allow("hank", now=0))
        self.assertTrue(per.allow("jules", now=60))
        glob = botreply.Limiter(100, 30)
        for i in range(30):
            self.assertTrue(glob.allow("b%d" % (i % 5), now=0), i)
        self.assertFalse(glob.allow("fresh", now=0))
        self.assertTrue(glob.allow("fresh", now=60))

    def test_outer_shape_and_size(self):
        with self.assertRaises(botreply.Reject) as cm:
            self._go(b"{" * 10)
        self.assertEqual(cm.exception.verdict, "shape")
        with self.assertRaises(botreply.Reject) as cm:
            self._go(b"x" * (botreply.OUTER_MAX + 1))
        self.assertEqual((cm.exception.status, cm.exception.bot), (400, "-"))
        raw = json.dumps({"v": True, "bot": "jules", "sealed": _b64(b"abcd")}).encode()
        with self.assertRaises(botreply.Reject) as cm:
            self._go(raw)
        self.assertEqual(cm.exception.verdict, "shape")

    def test_stats_average_timeout_and_last_ten(self):
        path = os.path.join(tempfile.mkdtemp(), "rtt.json")
        stats = botreply.Stats(path)
        stats.record("jules", rtt_ms=1000)
        stats.record("jules", rtt_ms=3000)
        stats.record("jules", timeout=True)
        self.assertEqual(stats.bot_view("jules"), {"avg_rtt_ms": 2000, "timeouts": 1})
        self.assertEqual(stats.overall_view(), {"avg_rtt_ms": 2000, "timeouts": 1})
        again = botreply.Stats(path)
        self.assertEqual(again.bot_view("jules")["avg_rtt_ms"], 2000)
        fresh = botreply.Stats(os.path.join(tempfile.mkdtemp(), "rtt.json"))
        fresh.record("jules", rtt_ms=5000)
        for _ in range(10):
            fresh.record("jules", rtt_ms=1000)
        self.assertEqual(fresh.bot_view("jules"), {"avg_rtt_ms": 1000, "timeouts": 0})
        self.assertEqual(len(fresh.data["overall"]), 10)

    def test_pin_revoke_and_kill_switch(self):
        d = tempfile.mkdtemp()
        path = botreply.keys_path(d)
        self.assertTrue(botreply.pin_key(path, "jules", PUB))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        doc = botreply.load_keys(path)
        self.assertFalse(doc["killed"])
        self.assertEqual(len(botreply.pinned_bytes(doc, "jules")), 32)
        botreply.revoke_one(path, "jules")
        self.assertIsNone(botreply.pinned_bytes(botreply.load_keys(path), "jules"))
        self.assertFalse(botreply.load_keys(path)["killed"])
        self.assertTrue(botreply.pin_key(path, "jules", PUB))
        self.assertTrue(botreply.pin_key(path, "hank", PUB))
        botreply.revoke_all(path)
        doc = botreply.load_keys(path)
        self.assertTrue(doc["killed"])
        self.assertEqual(doc["keys"], {})
        self.assertFalse(botreply.pin_key(path, "nope", "not-a-key"))
        self.assertTrue(botreply.pin_key(path, "jules", "\n" + PUB + "\n"))
        self.assertFalse(botreply.load_keys(path)["killed"])  # pairing clears the switch

    def test_station_key_is_0600_and_a_bad_file_is_kept(self):
        d = tempfile.mkdtemp()
        old = os.environ.get("GROK_SHARED_STATE")
        os.environ["GROK_SHARED_STATE"] = "1"
        try:
            sk = botreply.load_or_create_station(d)
            path = botreply.station_key_path(d)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(len(bytes(sk)), 32)
            again = botreply.load_or_create_station(d)
            self.assertEqual(bytes(sk), bytes(again))
            with open(path, "wb") as f:
                f.write(b"short")
            with self.assertRaises(botreply.StationKeyError):
                botreply.load_or_create_station(d)
            self.assertEqual(open(path, "rb").read(), b"short")
        finally:
            if old is None:
                os.environ.pop("GROK_SHARED_STATE", None)
            else:
                os.environ["GROK_SHARED_STATE"] = old

    def test_payload_puts_the_question_on_the_wire(self):
        q = handoff.clean("what\u200b's for dinner", handoff.Q_MAX)
        rid = handoff.new_request_id()
        reply_by = "2026-10-22T16:00:00Z"
        wire = json.loads(json.dumps(handoff.payload(rid, "jules", q, reply_by)))
        self.assertEqual(set(wire), {"v", "request_id", "persona", "question", "reply_by"})
        self.assertEqual(wire["v"], 1)
        self.assertEqual(wire["question"], q)
        self.assertEqual(wire["reply_by"], reply_by)
        self.assertEqual(handoff.payload(rid, "jules", q, reply_by)["tier_max"], 0)
        flagged = json.loads(json.dumps(handoff.payload(rid, "jules", q, reply_by, test=True, include_avatar=True)))
        self.assertEqual(set(flagged), {"v", "request_id", "persona", "question", "reply_by", "test"})
        self.assertNotIn("include_avatar", flagged)
        self.assertNotIn("source", flagged)

    def test_helper_refuses_argv_keys_and_pins_the_cert(self):
        self.assertEqual(helper.main(["send", "--key", "nope"]), 2)
        self.assertEqual(helper.main(["send", "GROK_STATION_BOT_KEY=nope"]), 2)
        lines = helper.keygen_lines()
        self.assertTrue(lines[0].startswith("GROK_STATION_BOT_KEY="))
        self.assertTrue(lines[1].startswith("public: "))
        self.assertEqual(len(base64.b64decode(lines[0].split("=", 1)[1])), 32)
        self.assertEqual(len(base64.b64decode(lines[1].split(" ", 1)[1])), 32)
        buf = io.StringIO()
        old = sys.stdout
        try:
            sys.stdout = buf
            self.assertEqual(helper.main(["keygen"]), 0)
        finally:
            sys.stdout = old
        out = buf.getvalue().splitlines()
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0].startswith("GROK_STATION_BOT_KEY="))
        pub = _b64(bytes(PrivateKey.generate().public_key))
        old_in, old_out = sys.stdin, sys.stdout
        try:
            sys.stdin = io.StringIO("")
            sys.stdout = io.StringIO()
            rc = helper.main(["send", "--station", "https://127.0.0.1", "--bot", "jules", "--station-pub", pub,
                              "--request-id", RID])
            text = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        self.assertEqual(rc, 1)
        self.assertEqual(text.strip(), "400 bad request")
        old_in, old_out, old_err = sys.stdin, sys.stdout, sys.stderr
        try:
            sys.stdin = io.StringIO("hello")
            sys.stdout = io.StringIO()
            sys.stderr = io.StringIO()
            rc = helper.main(["send", "--station", "https://127.0.0.1", "--bot", "jules", "--station-pub", pub,
                              "--request-id", RID, "--insecure"])
            err = sys.stderr.getvalue()
        finally:
            sys.stdin, sys.stdout, sys.stderr = old_in, old_out, old_err
        self.assertEqual(rc, 2)
        self.assertIn("pin", err)
        self.assertNotIn("hello", err)
        fp = "ab" * 32
        colon = ":".join(fp[i:i + 2] for i in range(0, 64, 2))
        self.assertEqual(helper._pin(colon.upper()), fp)
        self._assert_pin(fp)

    def _assert_pin(self, _fp_unused):
        d = tempfile.mkdtemp()
        cert, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
        import subprocess
        subprocess.check_call(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key, "-out", cert,
                               "-days", "1", "-nodes", "-subj", "/CN=localhost"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        der = subprocess.check_output(["openssl", "x509", "-in", cert, "-outform", "DER"])
        fp = __import__("hashlib").sha256(der).hexdigest()
        colon = ":".join(fp[i:i + 2] for i in range(0, 64, 2)).upper()

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *_a):
                pass

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            seed = _b64(bytes(SigningKey.generate()))
            pub = botreply.station_pub_b64(PrivateKey.generate())
            body = helper.build_outer("jules", RID, "ok", seed, pub)
            status, verdict = helper.post_reply(f"https://127.0.0.1:{port}", body, insecure=True, pin=fp)
            self.assertEqual((status, verdict), (200, "ok"))
            status, verdict = helper.post_reply("https://127.0.0.1:%d" % port, body, insecure=True, pin=helper._pin(colon))
            self.assertEqual(status, 200)
            with self.assertRaises(ssl.SSLError):
                helper.post_reply(f"https://127.0.0.1:{port}", body, insecure=True, pin="ab" * 32)
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()


def _run_bytes(argv, stdin=b"", env=None):
    """Like _run, but stdin is raw bytes (a picture has no text encoding)."""
    old = (sys.stdin, sys.stdout, sys.stderr, os.environ.get("GROK_STATION_BOT_KEY"))
    try:
        sys.stdin, sys.stdout, sys.stderr = io.BytesIO(stdin), io.StringIO(), io.StringIO()
        os.environ.pop("GROK_STATION_BOT_KEY", None)
        if env:
            os.environ["GROK_STATION_BOT_KEY"] = env
        rc = helper.main(argv)
        return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
    finally:
        sys.stdin, sys.stdout, sys.stderr, key = old
        os.environ.pop("GROK_STATION_BOT_KEY", None)
        if key is not None:
            os.environ["GROK_STATION_BOT_KEY"] = key


def _run(argv, stdin="", env=None):
    old = (sys.stdin, sys.stdout, sys.stderr, os.environ.get("GROK_STATION_BOT_KEY"))
    try:
        sys.stdin, sys.stdout, sys.stderr = io.StringIO(stdin), io.StringIO(), io.StringIO()
        os.environ.pop("GROK_STATION_BOT_KEY", None)
        if env:
            os.environ["GROK_STATION_BOT_KEY"] = env
        rc = helper.main(argv)
        return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
    finally:
        sys.stdin, sys.stdout, sys.stderr, key = old
        os.environ.pop("GROK_STATION_BOT_KEY", None)
        if key is not None:
            os.environ["GROK_STATION_BOT_KEY"] = key


class SealPost(unittest.TestCase):
    setUp = Check.setUp
    _go = Check._go

    def _seal_args(self):
        return ["seal", "--bot", "jules", "--station-pub", botreply.station_pub_b64(self.station), "--request-id", RID]

    def test_seal_then_post_round_trip_verifies(self):
        seed = _b64(bytes(self.signer))
        rc, out, err = _run(self._seal_args(), "Tacos on Tuesday.\n", env=seed)
        self.assertEqual(rc, 0)
        self.assertNotIn("Tacos", out + err)
        self.assertNotIn(seed, out + err)
        self.assertEqual(set(json.loads(out)), {"v", "bot", "sealed"})
        sent = {}

        def fake_post(origin, body, insecure=False, pin="", path="/voice/reply"):
            sent.update(origin=origin, body=body, insecure=insecure, pin=pin, path=path)
            return 200, "ok"

        real = helper.post_reply
        helper.post_reply = fake_post
        try:
            rc, pout, _ = _run(["post", "--station", "https://127.0.0.1", "--insecure", "--pin-sha256", "ab" * 32], out)
        finally:
            helper.post_reply = real
        self.assertEqual((rc, pout.strip()), (0, "200 ok"))
        self.assertEqual(sent["pin"], "ab" * 32)
        self.assertEqual(sent["path"], "/voice/reply")
        got = self._go(sent["body"])
        self.assertEqual(got["text"], "Tacos on Tuesday.")
        self.assertEqual(got["request_id"], RID)

    def test_post_rejects_extra_fields_bad_shape_and_oversize(self):
        good = json.dumps({"v": 1, "bot": "jules", "sealed": _b64(b"x" * 100)})
        self.assertIsNotNone(helper.check_outer(good))
        bad = [json.dumps({"v": 1, "bot": "jules", "sealed": _b64(b"x"), "text": "hi"}),
               json.dumps({"v": 1, "bot": "jules"}),
               json.dumps({"v": 2, "bot": "jules", "sealed": _b64(b"x")}),
               json.dumps({"v": True, "bot": "jules", "sealed": _b64(b"x")}),
               json.dumps({"v": 1, "bot": "jules", "sealed": "not base64!"}),
               json.dumps({"v": 1, "bot": "jules", "sealed": _b64(b"x" * 7000)}),
               "[]", "", "nope"]
        for body in bad:
            self.assertIsNone(helper.check_outer(body), body[:40])
            called = []
            real = helper.post_reply
            helper.post_reply = lambda *a, **k: called.append(1) or (200, "ok")
            try:
                rc, out, _ = _run(["post", "--station", "https://127.0.0.1"], body)
            finally:
                helper.post_reply = real
            self.assertEqual((rc, out.strip(), called), (1, "400 bad request", []))
        rc, _, err = _run(["post", "--station", "https://127.0.0.1", "--insecure"], good)
        self.assertEqual(rc, 2)
        self.assertIn("pin", err)

    def test_seal_refuses_key_args_and_needs_env(self):
        seed = _b64(bytes(self.signer))
        for extra in (["--key", seed], ["--seed=" + seed], ["GROK_STATION_BOT_KEY=" + seed]):
            rc, out, err = _run(self._seal_args() + extra, "hi", env=seed)
            self.assertEqual(rc, 2)
            self.assertNotIn(seed, out + err)
        rc, out, err = _run(self._seal_args(), "hi")
        self.assertEqual(rc, 2)
        self.assertIn("GROK_STATION_BOT_KEY", err)
        self.assertEqual(out, "")

    def test_post_runs_without_pynacl(self):
        import subprocess
        code = ("import sys; sys.modules['nacl']=None; sys.argv=['x','post','--station','https://127.0.0.1:1'];"
                "import runpy; runpy.run_path(%r, run_name='__main__')" % _helper_path)
        body = json.dumps({"v": 1, "bot": "jules", "sealed": _b64(b"x" * 10)})
        env = {k: v for k, v in os.environ.items() if k != "GROK_STATION_BOT_KEY"}
        p = subprocess.run([sys.executable, "-c", code], input=body, capture_output=True, text=True, env=env, timeout=30)
        self.assertNotIn("nacl", p.stderr.lower())
        self.assertIn("tls error", p.stderr)

    def test_seal_avatar_then_post_avatar(self):
        seed = _b64(bytes(self.signer))
        pub = botreply.station_pub_b64(self.station)
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        rc, out, err = _run_bytes(["seal-avatar", "--bot", "jules", "--station-pub", pub], png, env=seed)
        self.assertEqual(rc, 0, err)
        self.assertNotIn(seed, out + err)
        self.assertNotIn("image_b64", out)
        self.assertEqual(set(json.loads(out)), {"v", "bot", "sealed"})
        opened = botreply.ingest_avatar(out.encode(), self.station, self.keys, self.known)
        self.assertEqual(opened["image"], png)
        self.assertEqual(opened["content_type"], "image/png")
        self.assertEqual(opened["bot"], "jules")
        again, out2, _ = _run_bytes(["seal-avatar", "--bot", "jules", "--station-pub", pub], png, env=seed)
        self.assertEqual(again, 0)
        self.assertNotEqual(out, out2)  # a fresh nonce, so the same file is not a replay
        sent = {}

        def fake_post(origin, body, insecure=False, pin="", path="/voice/reply"):
            sent.update(origin=origin, body=body, insecure=insecure, pin=pin, path=path)
            return 200, "ok"

        real = helper.post_reply
        helper.post_reply = fake_post
        try:
            rc, pout, _ = _run(["post", "--avatar", "--station", "https://127.0.0.1",
                                "--insecure", "--pin-sha256", "ab" * 32], out)
            self.assertEqual((rc, pout.strip(), sent.get("path"), sent.get("pin")),
                             (0, "200 ok", "/voice/avatar", "ab" * 32))
            called = []
            helper.post_reply = lambda *a, **k: called.append(1) or (200, "ok")
            rc, bout, _ = _run(["post", "--show", "--avatar", "--station", "https://127.0.0.1"], out)
            self.assertEqual((rc, bout.strip(), called), (1, "400 bad request", []))
        finally:
            helper.post_reply = real
        rc, bad, err = _run_bytes(["seal-avatar", "--bot", "jules", "--station-pub", pub], b"hello", env=seed)
        self.assertEqual((rc, bad.strip()), (1, "400 bad request"))
        self.assertNotIn(seed, bad + err)
        rc, empty, err = _run_bytes(["seal-avatar", "--bot", "jules", "--station-pub", pub], png)
        self.assertEqual(rc, 2)
        self.assertEqual(empty, "")
        self.assertIn("GROK_STATION_BOT_KEY", err)
        huge = b"\x89PNG\r\n\x1a\n" + b"x" * helper.AVATAR_IMAGE_MAX
        rc, _, _ = _run_bytes(["seal-avatar", "--bot", "jules", "--station-pub", pub], huge, env=seed)
        self.assertEqual(rc, 1)
        big = json.dumps({"v": 1, "bot": "jules", "sealed": _b64(b"x" * 7000)})
        self.assertIsNone(helper.check_outer(big))
        self.assertIsNotNone(helper.check_outer(big, limit=helper.AVATAR_OUTER_MAX))

    def test_picture_commands_stay_on_the_reply_tools(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tool = open(os.path.join(root, "tools", "grok-station-reply")).read()
        home = open(os.path.join(root, "scripts", "gs-seal-reply")).read()
        self.assertIn('sub.add_parser("seal-avatar")', tool)
        self.assertIn('"/voice/show" if args.show else "/voice/reply"', tool)
        self.assertIn('"/voice/avatar" if args.avatar else', tool)
        self.assertIn('add_parser("avatar")', home)
        self.assertIn("STDIN_MAX = 1024 * 1024", home)
        self.assertEqual(home.count(".request("), 1)
