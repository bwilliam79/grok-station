"""Station relay pairing API, update API, CA permissions, and the pairing screen.

No network: relay status, the release manifest, and the display post are injected.
The pairing listener binds 127.0.0.1 on port 0. Peers outside local ranges are dropped.
"""
import hashlib, http.client, importlib.machinery, importlib.util, json, os, ssl, stat, sys, tempfile, threading, unittest
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import test_admin
from test_admin import C, CLOCK, GOOD, ROOT, ga, signed_in

import pairing
import pair_client
import pair_listener
import release

APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

_ORIG = {
    "host": ga.PAIR_HOST,
    "port": ga.PAIR_PORT,
    "tls": ga.TLS_DIR,
    "ver": ga.VERSION_FILE,
    "env": ga.RELAY_ENV,
}
TLS_DIR = tempfile.mkdtemp(prefix="gs-tls-")
VER_DIR = tempfile.mkdtemp(prefix="gs-ver-")
VERSION_PATH = os.path.join(VER_DIR, "VERSION")
CANARY_Q = "QUESTION-CANARY-9c2e"
CANARY_TOK = "TOKEN-CANARY-9c2e"
ENV_TOKEN = "example-token"
ENV_HOST = "relay.example.invalid"


def _write(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as handle:
        handle.write(data)


def make_station_tls(directory):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "station.example.invalid")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    _write(os.path.join(directory, "key.pem"), key_pem, 0o600)
    _write(os.path.join(directory, "cert.pem"), cert_pem, 0o644)
    return cert_pem


make_station_tls(TLS_DIR)
_write(VERSION_PATH, "v1.0.0\n", 0o644)


def _iso(when):
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def manifest_at(when, **overrides):
    issued = when - timedelta(minutes=1)
    doc = {
        "version": "v1.2.0",
        "min_version_floor": "v1.0.0",
        "issued_at": _iso(issued),
        "expires": _iso(issued + timedelta(days=20)),
        "components": {
            "station": {"tarball": "station-v1.2.0.tar.gz", "sha256": "ab" * 32},
            "relay": {"images": {"relay": "sha256:" + "cd" * 32}},
        },
        "compat": {"station_min_relay": "v1.0.0", "relay_min_station": "v1.0.0"},
    }
    doc.update(overrides)
    return doc


def tls_call(host, port, method, path, body=None, headers=None):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(host, port, timeout=5, context=ctx)
    try:
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, raw
    finally:
        conn.close()


def helper_log():
    try:
        return open(os.environ["FAKE_LOG"], encoding="utf-8").read()
    except OSError:
        return ""


def clear_helper_log():
    open(os.environ["FAKE_LOG"], "w").close()


def audit_text():
    path = os.path.join(os.environ["ADMIN_STATE_DIR"], "relay-audit.log")
    try:
        return open(path, encoding="utf-8").read()
    except OSError:
        return ""


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def __call__(self):
        return self.t


def tearDownModule():
    ga.PAIR_HOST = _ORIG["host"]
    ga.PAIR_PORT = _ORIG["port"]
    ga.TLS_DIR = _ORIG["tls"]
    ga.VERSION_FILE = _ORIG["ver"]
    ga.RELAY_ENV = _ORIG["env"]
    ga.reset_pair_runtime()


class StationCaTests(unittest.TestCase):
    def test_perms_and_reload_keep_revocations(self):
        directory = tempfile.mkdtemp(prefix="gs-ca-")
        ca = pairing.StationCA.load_or_create(directory)
        key_path = os.path.join(directory, "ca-key.pem")
        self.assertEqual(stat.S_IMODE(os.stat(key_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode) & 0o077, 0)
        blob = open(key_path, "rb").read()
        self.assertTrue(blob.startswith(b"-----BEGIN"))
        cert_der = ca.cert.public_bytes(serialization.Encoding.DER)
        key = ec.generate_private_key(ec.SECP256R1())
        pid = "ab" * 8
        issued, _serial8 = ca.sign_csr(pair_client.build_relay_csr(key, pid), pid)
        ca.revoke(issued.serial_number)
        os.chmod(key_path, 0o640)
        with self.assertRaises(pairing.PairingError) as caught:
            pairing.StationCA.load_or_create(directory)
        self.assertEqual(caught.exception.reason, "ca-key-perms")
        self.assertEqual(open(key_path, "rb").read(), blob)
        os.chmod(key_path, 0o600)
        again = pairing.StationCA.load_or_create(directory)
        self.assertEqual(again.cert.public_bytes(serialization.Encoding.DER), cert_der)
        self.assertTrue(again.is_revoked(issued.serial_number))
        self.assertTrue(again.is_revoked(format(issued.serial_number, "x")))


class PairListenerTests(unittest.TestCase):
    def _listener(self, clock, source_ok=None):
        cert_pem = open(os.path.join(TLS_DIR, "cert.pem"), "rb").read()
        ca = pairing.StationCA.load_or_create(tempfile.mkdtemp(prefix="gs-ca-"))
        window = pairing.PairingWindow(cert_pem, ca, clock=clock)
        issuance = pair_listener.Issuance()
        listener = pair_listener.PairListener(
            window, issuance,
            os.path.join(TLS_DIR, "cert.pem"), os.path.join(TLS_DIR, "key.pem"),
            host="127.0.0.1", port=0, source_ok=source_ok,
        )
        return window, issuance, listener

    def test_binds_only_while_open_and_drops_other_sources(self):
        clock = Clock()
        window, issuance, listener = self._listener(clock)
        try:
            listener.poll()
            self.assertFalse(listener.bound)
            code = window.open(clock.t)
            listener.poll()
            self.assertTrue(listener.bound)
            host, port = listener.bound_address
            self.assertEqual(host, "127.0.0.1")
            status, raw = tls_call(host, port, "GET", "/pair/cert-check")
            self.assertEqual(status, 200)
            self.assertNotIn(b"PRIVATE", raw)
            self.assertNotIn(b"BEGIN CERTIFICATE", raw)
            self.assertNotIn(code.encode(), raw)
            status, raw = tls_call(host, port, "POST", "/pair/submit", headers={"Content-Length": "20000"})
            self.assertEqual(status, 413, raw)
            dead = _Closer()
            listener._httpd.finish_request(dead, ("192.0.2.1", 9))
            self.assertTrue(dead.closed)
            self.assertFalse(pairing.lan_source_ok("192.0.2.1"))
            key = ec.generate_private_key(ec.SECP256R1())
            pid = "cd" * 8
            csr = pair_client.build_relay_csr(key, pid)
            proof = pairing.code_proof(code, window.fingerprint).hex()
            status, raw = tls_call(host, port, "POST", "/pair/submit", {
                "csr_pem": csr.public_bytes(serialization.Encoding.PEM).decode("ascii"),
                "proof": proof,
            })
            self.assertEqual(status, 200, raw)
            status, raw = tls_call(host, port, "GET", "/pair/result?id=" + pid)
            self.assertEqual(status, 404)
            issued = window.confirm_match()
            self.assertIsNotNone(issued)
            cert, _short = issued
            issuance.put(
                pid,
                cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
                window.ca.cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
            )
            listener.poll()
            self.assertTrue(listener.bound)
            status, raw = tls_call(host, port, "GET", "/pair/result?id=" + pid)
            self.assertEqual(status, 200, raw)
            body = json.loads(raw)
            self.assertIn("BEGIN CERTIFICATE", body["cert_pem"])
            self.assertIn("BEGIN CERTIFICATE", body["ca_pem"])
            self.assertNotIn("PRIVATE", raw.decode())
            listener.poll()
            self.assertFalse(listener.bound)
            window.cancel()
            listener.poll()
            self.assertFalse(listener.bound)
        finally:
            listener.unbind()

    def test_rejecting_source_closes_the_socket(self):
        clock = Clock()
        _window, _issuance, listener = self._listener(clock, source_ok=lambda _ip: False)
        try:
            listener.window.open(clock.t)
            listener.poll()
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            raw = __import__("socket").create_connection(listener.bound_address, timeout=2)
            try:
                with self.assertRaises((ssl.SSLError, OSError)):
                    ctx.wrap_socket(raw, server_hostname="example.invalid")
            finally:
                raw.close()
        finally:
            listener.unbind()


class _Closer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def settimeout(self, _timeout):
        raise AssertionError("non-local peer reached TLS")


class _AdminCase(unittest.TestCase):
    def setUp(self):
        self.c = signed_in()
        ga.PAIR_HOST = "127.0.0.1"
        ga.PAIR_PORT = 0
        ga.TLS_DIR = TLS_DIR
        ga.VERSION_FILE = VERSION_PATH
        _write(VERSION_PATH, "v1.0.0\n", 0o644)
        clear_helper_log()
        self.posts = []
        self.relay_calls = []
        self.fail_update = False
        self.manifest = lambda: manifest_at(datetime.fromtimestamp(ga.now(), timezone.utc))
        ga.STATION_POST = self.posts.append
        ga.RELAY_FETCHER = self._fetch
        ga.MANIFEST_FETCHER = lambda: self.manifest()
        ga.RELAY_ENV = _ORIG["env"]

    def tearDown(self):
        ga.RELAY_ENV = _ORIG["env"]
        ga.VERSION_FILE = VERSION_PATH
        ga.reset_pair_runtime()

    def _fetch(self, method, path, payload):
        self.relay_calls.append((method, path, payload))
        if method == "GET" and path == "/admin/status":
            return 200, {
                "version": "v1.0.0",
                "last_seen": 1_700_000_000,
                "xai": "ok",
                "key_last4": "ab12",
                "cert_expiry": "2027-01-01T00:00:00Z",
                "pii_mode": True,
                "rate": "steady",
                "question": CANARY_Q,
                "token": CANARY_TOK,
                "host": ENV_HOST,
            }
        if method == "POST" and path == "/admin/update":
            if self.fail_update:
                return 500, {"error": "nope"}
            return 200, {"ok": True}
        return 404, {"error": "missing"}

    def _no_secrets(self, blob):
        self.assertNotIn(CANARY_Q, blob)
        self.assertNotIn(CANARY_TOK, blob)
        self.assertNotIn(ENV_HOST, blob)
        self.assertNotIn(ENV_TOKEN, blob)


class RelayApiTests(_AdminCase):
    def test_session_and_password_required(self):
        fresh = C()
        self.assertEqual(fresh.req("GET", "/api/relay")[0], 401)
        self.assertEqual(fresh.req("POST", "/api/relay/pair/start", {"password": GOOD})[0], 401)
        self.assertEqual(fresh.req("GET", "/api/updates")[0], 401)
        self.assertEqual(self.c.req("POST", "/api/relay/pair/start", {})[0], 403)
        self.assertEqual(self.c.req("POST", "/api/relay/pair/start", {"password": "not-the-password"})[0], 403)
        self.assertIsNone(ga.PAIR_LISTENER)
        self.assertNotIn("pair-fw-open", helper_log())
        self.assertEqual(self.c.req("GET", "/api/system/update")[0], 404)
        self.assertEqual(self.c.req("POST", "/api/system/update", {"sha": "a" * 40})[0], 404)
        self.assertEqual(self.c.req("POST", "/api/secrets/relay_token", {"value": "example-token"})[0], 404)
        self.assertEqual(self.c.req("POST", "/api/secrets/xai_key", {"value": "example-token"})[0], 404)

    def test_missing_station_cert_is_503(self):
        empty = tempfile.mkdtemp(prefix="gs-empty-tls-")
        ga.TLS_DIR = empty
        status, body = self.c.req("POST", "/api/relay/pair/start", {"password": GOOD})
        self.assertEqual(status, 503)
        self.assertIn("certificate", body["error"].lower())

    def test_pair_confirm_unpair_and_env_fallback(self):
        env_path, env_bytes = _relay_env()
        ga.RELAY_ENV = env_path
        before = ga.relay_mtls_config()
        self.assertEqual(before["source"], "env")
        self.assertTrue(before["cert"].endswith("client.crt"))
        status, started = self.c.req("POST", "/api/relay/pair/start", {"password": GOOD})
        self.assertEqual(status, 200, started)
        code = started["pairing"]["code"]
        self.assertRegex(code, r"^[A-Z2-7]{4}-[A-Z2-7]{4}$")
        self.assertEqual(started["state"], "pairing")
        again_s, again = self.c.req("POST", "/api/relay/pair/start", {"password": GOOD})
        self.assertEqual(again_s, 409)
        self.assertNotIn(code, json.dumps(again))
        self.assertNotIn(code.replace("-", ""), json.dumps(again))
        status, newer = self.c.req("POST", "/api/relay/pair/new-code", {})
        self.assertEqual(status, 200, newer)
        code = newer["pairing"]["code"]
        other = C()
        self.assertEqual(other.req("POST", "/api/login", {"username": "admin", "password": GOOD})[0], 200)
        self.assertEqual(other.req("POST", "/api/relay/pair/new-code", {})[0], 403)
        view_s, view = self.c.req("GET", "/api/relay")
        self.assertEqual(view_s, 200)
        self.assertEqual(view["state"], "pairing")
        self.assertNotIn("code", view["pairing"])
        self.assertNotIn(code, json.dumps(view))
        self.assertNotIn(code.replace("-", ""), json.dumps(view))
        self._no_secrets(json.dumps(view))
        lines = audit_text()
        self.assertNotIn(code, lines)
        self.assertNotIn(code.replace("-", ""), lines)
        self._no_secrets(lines)
        self.assertIn("pair-fw-open", helper_log())
        fp = pairing.fingerprint_bytes(open(os.path.join(TLS_DIR, "cert.pem"), "rb").read()).hex()
        self.assertEqual(self.posts, [{"fingerprint": fp}])
        self.assertTrue(ga.PAIR_LISTENER.bound)
        self.assertEqual(ga.PAIR_LISTENER.bound_address[0], "127.0.0.1")
        key = ec.generate_private_key(ec.SECP256R1())
        pid = "ef" * 8
        csr = pair_client.build_relay_csr(key, pid)
        host, port = ga.PAIR_LISTENER.bound_address
        proof = pairing.code_proof(code, ga.PAIR_WINDOW.fingerprint).hex()
        status, raw = tls_call(host, port, "POST", "/pair/submit", {
            "csr_pem": csr.public_bytes(serialization.Encoding.PEM).decode("ascii"),
            "proof": proof,
        })
        self.assertEqual(status, 200, raw)
        view = self.c.req("GET", "/api/relay")[1]
        shown = pairing.format_fingerprint(pairing.spki_fingerprint(key.public_key()))
        self.assertEqual(view["pairing"]["relay_fingerprint"], shown)
        self.assertNotIn("code", view["pairing"])
        status, confirmed = self.c.req("POST", "/api/relay/pair/confirm", {})
        self.assertEqual(status, 200, confirmed)
        self.assertTrue(ga.PAIR_LISTENER.bound)
        status, raw = tls_call(host, port, "GET", "/pair/result?id=" + pid)
        self.assertEqual(status, 200, raw)
        body = json.loads(raw)
        self.assertIn("BEGIN CERTIFICATE", body["cert_pem"])
        self.assertIn("BEGIN CERTIFICATE", body["ca_pem"])
        self.assertNotIn("PRIVATE", raw.decode())
        ga.sync_pair_port()
        self.assertFalse(ga.PAIR_LISTENER.bound)
        self.assertIn("pair-fw-close", helper_log())
        self.assertIn({"clear": True}, self.posts)
        key_path = os.path.join(ga.paired_dir(), "client-key.pem")
        self.assertEqual(stat.S_IMODE(os.stat(key_path).st_mode), 0o600)
        paired = ga.relay_mtls_config()
        self.assertEqual(paired["source"], "paired")
        self.assertTrue(paired["cert"].endswith("client-cert.pem"))
        self.assertEqual(open(env_path, "rb").read(), env_bytes)
        ca_der = open(os.path.join(ga.ca_dir(), "ca-cert.pem"), "rb").read()
        serial = json.load(open(os.path.join(ga.paired_dir(), "relay.json")))["serial"]
        self.assertEqual(self.c.req("POST", "/api/relay/unpair", {})[0], 403)
        self.assertTrue(os.path.isfile(key_path))
        status, body = self.c.req("POST", "/api/relay/unpair", {"password": GOOD})
        self.assertEqual(status, 200, body)
        self.assertFalse(os.path.exists(key_path))
        self.assertEqual(open(env_path, "rb").read(), env_bytes)
        reloaded = pairing.StationCA.load_or_create(ga.ca_dir())
        self.assertEqual(open(os.path.join(ga.ca_dir(), "ca-cert.pem"), "rb").read(), ca_der)
        self.assertTrue(reloaded.is_revoked(serial))
        self._no_secrets(audit_text())

    def test_cancel_and_timeout_unbind(self):
        status, started = self.c.req("POST", "/api/relay/pair/start", {"password": GOOD})
        self.assertEqual(status, 200, started)
        self.assertTrue(ga.PAIR_LISTENER.bound)
        self.assertEqual(self.c.req("POST", "/api/relay/pair/cancel", {})[0], 200)
        self.assertFalse(ga.PAIR_LISTENER.bound)
        self.assertIn("pair-fw-close", helper_log())
        self.assertIn({"clear": True}, self.posts)
        clear_helper_log()
        self.posts.clear()
        status, started = self.c.req("POST", "/api/relay/pair/start", {"password": GOOD})
        self.assertEqual(status, 200, started)
        self.assertTrue(ga.PAIR_LISTENER.bound)
        mark = CLOCK[0]
        CLOCK[0] = mark + 601
        try:
            view = self.c.req("GET", "/api/relay")[1]
            self.assertNotEqual(view["state"], "pairing")
            self.assertFalse(ga.PAIR_LISTENER.bound)
            self.assertIn("pair-fw-close", helper_log())
        finally:
            CLOCK[0] = mark


class UpdatesApiTests(_AdminCase):
    def test_check_apply_autocheck_and_failures(self):
        fresh = C()
        self.assertEqual(fresh.req("GET", "/api/updates")[0], 401)
        self.assertEqual(fresh.req("POST", "/api/updates/apply", {"version": "v1.2.0"})[0], 401)
        self.assertEqual(self.c.req("PUT", "/api/updates/autocheck", {"on": True})[0], 403)
        self.assertEqual(self.c.req("POST", "/api/updates/check", {})[0], 403)
        self.assertEqual(
            release.MANIFEST_URL,
            "https://github.com/bwilliam79/grok-station/releases/latest/download/manifest.json",
        )
        status, view = self.c.req("POST", "/api/updates/check", {"password": GOOD})
        self.assertEqual(status, 200, view)
        self.assertEqual(view["available"], "v1.2.0")
        self.assertIsNone(view["cant_check"])
        self.assertIn("relay", view["plan"])
        self.assertIn("soak:600", view["plan"])
        self.assertIn("station", view["plan"])
        self.assertTrue(view["station_only_allowed"])
        self.assertEqual(view["installed"]["station"], "v1.0.0")
        self.assertEqual(view["installed"]["relay"], "v1.0.0")
        self.assertFalse(view["autocheck"])
        relay = self.c.req("GET", "/api/relay")[1]
        self.assertEqual(relay["state"], "outdated")
        self.assertEqual(relay["relay"]["xai"], "ok")
        self.assertEqual(relay["relay"]["key_last4"], "ab12")
        self._no_secrets(json.dumps(relay))
        status, rejected = self.c.req("POST", "/api/updates/apply", {
            "version": "v1.2.0", "password": GOOD, "url": "https://example.invalid/pkg",
        })
        self.assertEqual(status, 400, rejected)
        self.assertFalse(any(call[1] == "/admin/update" for call in self.relay_calls))
        clear_helper_log()
        self.relay_calls.clear()
        status, applied = self.c.req("POST", "/api/updates/apply", {"version": "v1.2.0"})
        self.assertEqual(status, 202, applied)
        updates = [call for call in self.relay_calls if call[1] == "/admin/update"]
        self.assertEqual(updates, [("POST", "/admin/update", {"version": "v1.2.0"})])
        self.assertIn("station-update", helper_log())
        self.assertIn("v1.2.0", helper_log())
        self.assertNotIn(GOOD, helper_log())
        clear_helper_log()
        self.fail_update = True
        status, failed = self.c.req("POST", "/api/updates/apply", {"version": "v1.2.0"})
        self.assertEqual(status, 502, failed)
        self.assertNotIn("station-update", helper_log())
        self.fail_update = False
        status, turned = self.c.req("PUT", "/api/updates/autocheck", {"on": True, "password": GOOD})
        self.assertEqual(status, 200, turned)
        self.assertTrue(turned["autocheck"])
        status, off = self.c.req("PUT", "/api/updates/autocheck", {"on": False})
        self.assertEqual(status, 200, off)
        self.assertFalse(self.c.req("GET", "/api/updates")[1]["autocheck"])

    def test_expired_manifest_and_dev_version(self):
        when = datetime.fromtimestamp(ga.now(), timezone.utc)
        exp = when - timedelta(days=1)
        self.manifest = lambda: manifest_at(
            when,
            issued_at=_iso(exp - timedelta(days=10)),
            expires=_iso(exp),
        )
        status, view = self.c.req("POST", "/api/updates/check", {"password": GOOD})
        self.assertEqual(status, 200, view)
        self.assertEqual(view["cant_check"]["title"], release.EXPIRED_TITLE)
        self.assertEqual(view["cant_check"]["message"], release.EXPIRED_MESSAGE)
        self.assertIsNone(view["available"])
        status, body = self.c.req("POST", "/api/updates/apply", {"version": "v1.2.0"})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"], release.EXPIRED_MESSAGE)
        self.manifest = lambda: manifest_at(datetime.fromtimestamp(ga.now(), timezone.utc))
        _write(VERSION_PATH, "dev\n", 0o644)
        status, body = self.c.req("POST", "/api/updates/apply", {"version": "v1.2.0", "password": GOOD})
        self.assertEqual(status, 400, body)
        self.assertIn("isn't a release", body["error"])
        _write(VERSION_PATH, "v1.0.0\n", 0o644)
        self.assertEqual(self.c.req("POST", "/api/updates/apply", {"version": "latest", "password": GOOD})[0], 400)


def _relay_env():
    directory = tempfile.mkdtemp(prefix="gs-env-")
    _write(os.path.join(directory, "ca.pem"), "ca-example\n", 0o644)
    _write(os.path.join(directory, "client.crt"), "cert-example\n", 0o644)
    _write(os.path.join(directory, "client.key"), "key-example\n", 0o600)
    _write(os.path.join(directory, "token"), ENV_TOKEN + "\n", 0o600)
    text = "\n".join([
        "ADMIN_RELAY_URL=https://%s:8443" % ENV_HOST,
        "ADMIN_RELAY_CA=%s" % os.path.join(directory, "ca.pem"),
        "ADMIN_RELAY_CERT=%s" % os.path.join(directory, "client.crt"),
        "ADMIN_RELAY_KEY=%s" % os.path.join(directory, "client.key"),
        "ADMIN_RELAY_TOKEN_FILE=%s" % os.path.join(directory, "token"),
        "",
    ])
    path = os.path.join(directory, "admin-relay.env")
    _write(path, text, 0o600)
    return path, open(path, "rb").read()


class FirewallTests(unittest.TestCase):
    def test_ruleset_is_fixed(self):
        path = os.path.join(ROOT, "admin", "grok-station-helper")
        loader = importlib.machinery.SourceFileLoader("gs_helper_rules", path)
        spec = importlib.util.spec_from_loader("gs_helper_rules", loader)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        text = mod.pair_fw_ruleset()
        self.assertIn("table inet grok_pair", text)
        self.assertIn("tcp dport 8443", text)
        self.assertIn("drop", text)
        for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10"):
            self.assertIn(cidr, text)
        self.assertNotIn("192.0.2.", text)
        calls = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))

            class Done:
                returncode = 0
                stdout = b""
                stderr = b""

            return Done()

        real_run = mod.subprocess.run
        mod.subprocess.run = fake_run
        self.addCleanup(setattr, mod.subprocess, "run", real_run)
        with self.assertRaises(SystemExit) as caught:
            mod.pair_fw_open()
        self.assertEqual(caught.exception.code, 0)
        piped = [item for item in calls if item[0][0][:2] == ["nft", "-f"]]
        self.assertEqual(piped[0][1]["input"], text.encode())
        calls.clear()
        with self.assertRaises(SystemExit) as caught:
            mod.pair_fw_close()
        self.assertEqual(caught.exception.code, 0)
        self.assertIn((["nft", "delete", "table", "inet", "grok_pair"],), [(list(item[0][0]),) for item in calls])
        with self.assertRaises(SystemExit) as caught:
            mod.station_update("v1.2.0")
        self.assertEqual(caught.exception.code, 0)
        for bad in ("latest", "v1.2", "v1.2.3;reboot", "../v1.2.0", ""):
            with self.assertRaises(SystemExit) as caught:
                mod.station_update(bad)
            self.assertEqual(caught.exception.code, 1, bad)


class PairingScreenTests(unittest.TestCase):
    def test_demo_draws_every_digit_at_least_28px(self):
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        import pygame
        import grok_station as gs
        pygame.display.init()
        pygame.font.init()
        fp = "0123456789abcdef" * 4
        seen = []
        real_mono = gs.pairing_mono
        real_font = gs.font

        class Wrap:
            def __init__(self, inner, tag, px):
                self._inner = inner
                self._tag = tag
                self._px = px

            def render(self, text, antialias, color, *args, **kwargs):
                surf = self._inner.render(text, antialias, color, *args, **kwargs)
                seen.append((self._tag, self._px, text, surf.get_height(), surf.get_width()))
                return surf

            def __getattr__(self, name):
                return getattr(self._inner, name)

        def mono(px):
            return Wrap(real_mono(px), "pair", px)

        def font(kind, px):
            return Wrap(real_font(kind, px), kind, px)

        gs.pairing_mono = mono
        gs.font = font
        gs._pair_fonts.clear()
        out = tempfile.mkdtemp(prefix="gs-demo-")
        try:
            gs.demo(out, (800, 480))
        finally:
            gs.pairing_mono = real_mono
            gs.font = real_font
            gs._pair_fonts.clear()
        png = os.path.join(out, "pairing-check.png")
        self.assertTrue(os.path.isfile(png))
        self.assertEqual(pygame.image.load(png).get_size(), (800, 480))
        lines = [row for row in seen if row[0] == "pair"]
        self.assertEqual(len(lines), 4)
        digits = "".join("".join(ch for ch in row[2] if ch in "0123456789ABCDEF") for row in lines)
        self.assertEqual(digits, fp.upper())
        for _tag, px, text, height, width in lines:
            self.assertGreaterEqual(px, 28)
            self.assertGreaterEqual(height, 28)
            self.assertLessEqual(width, 800)
            self.assertEqual(len([ch for ch in text if ch in "0123456789ABCDEF"]), 16)
        heads = [row for row in seen if row[2] == "Check this matches"]
        self.assertTrue(heads)
        self.assertGreaterEqual(heads[0][1], 28)
        self.assertGreater(gs.pairing_font_px(1080), gs.pairing_font_px(480))
        self.assertGreaterEqual(gs.pairing_font_px(480), 28)

    def test_pairing_route_is_token_authenticated(self):
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        import pygame
        import grok_station as gs
        pygame.display.init()
        pygame.font.init()
        old = gs.TOKEN
        gs.TOKEN = "example-display-token"
        gs.PAIRING["fingerprint"] = None
        srv = gs.ThreadingHTTPServer(("127.0.0.1", 0), gs.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        fp = "Aa" * 32

        def post(body, token=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Content-Type": "application/json"}
            if token is not None:
                headers["Authorization"] = "Bearer " + token
            conn.request("POST", "/pairing", json.dumps(body).encode(), headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
            conn.close()
            return status, raw

        try:
            self.assertEqual(post({"fingerprint": fp})[0], 401)
            self.assertEqual(post({"fingerprint": "abcd"}, gs.TOKEN)[0], 400)
            self.assertIsNone(gs.PAIRING["fingerprint"])
            status, raw = post({"fingerprint": fp}, gs.TOKEN)
            self.assertEqual(status, 200, raw)
            self.assertEqual(gs.PAIRING["fingerprint"], fp.lower())
            status, raw = post({"clear": True}, gs.TOKEN)
            self.assertEqual(status, 200, raw)
            self.assertIsNone(gs.PAIRING["fingerprint"])
        finally:
            srv.shutdown()
            srv.server_close()
            gs.TOKEN = old
            gs.PAIRING["fingerprint"] = None


if __name__ == "__main__":
    unittest.main()
