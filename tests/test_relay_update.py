"""Relay control channel, host updater, manifest tool, and installers.

No sockets. Fetchers, the xAI probe, docker, and the clock are injected.
"""
import base64
import hashlib
import io
import json
import os
import ssl
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
import control  # noqa: E402
import grok_relay_cli as cli  # noqa: E402
import host_update  # noqa: E402
import pair_transport  # noqa: E402
import release  # noqa: E402
import release_fetch  # noqa: E402
import relay  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "xai-test-secret-wxyz"
DECOY = "sha256:" + ("11" * 32)
RELAY_DIGEST = "sha256:" + ("cd" * 32)
CADDY_DIGEST = "sha256:" + ("ef" * 32)


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(when):
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def manifest_doc(version="v1.2.0", when=None):
    when = when or _now()
    return {
        "version": version,
        "min_version_floor": "v1.0.0",
        "issued_at": _iso(when),
        "expires": _iso(when + timedelta(days=30)),
        "components": {
            "station": {"tarball": "station-v1.2.0.tar.gz", "sha256": "ab" * 32},
            "relay": {"images": {"relay": RELAY_DIGEST, "caddy": CADDY_DIGEST}},
        },
        "compat": {"station_min_relay": "v1.1.0", "relay_min_station": "v1.0.0"},
    }


def _cert(san, serial, eku, ca_key, ca_name, when):
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, san)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(when - timedelta(days=1))
        .not_valid_after(when + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage(eku), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return leaf


def issue_ca():
    when = _now()
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "station-pairing-ca")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(when - timedelta(days=1))
        .not_valid_after(when + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    return ca_key, ca, ca_name, when


def issue_station(san="station-0123456789abcdef", serial=0x10, eku=None, parent=None):
    if parent is None:
        ca_key, ca, ca_name, when = issue_ca()
    else:
        ca_key, ca, ca_name, when = parent
    leaf = _cert(san, serial, eku or [ExtendedKeyUsageOID.CLIENT_AUTH], ca_key, ca_name, when)
    return ca, leaf


def cert_headers(leaf, fp=None):
    der = leaf.public_bytes(serialization.Encoding.DER)
    return {
        "X-Client-Cert": base64.b64encode(der).decode("ascii"),
        "X-Client-Cert-Fp": hashlib.sha256(der).hexdigest() if fp is None else fp,
    }


class Mem:
    def __init__(self):
        self.files = {}
        self.runs = []
        self.disk = 8000

    def platform(self):
        return "linux"

    def now(self):
        return _now()

    def read_bytes(self, path):
        return self.files.get(path)

    def write_file(self, path, data):
        self.files[path] = bytes(data)

    def remove_file(self, path):
        self.files.pop(path, None)

    def run(self, argv):
        self.runs.append(list(argv))
        return cli.RunResult(0, b"", b"")

    def disk_free_mb(self):
        return self.disk

    def append_audit(self, path, line):
        self.files[path] = self.files.get(path, b"") + line.encode("ascii")


class ControlChannel(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = {
            "KEY": relay.KEY,
            "WEBHOOKS_FILE": relay.WEBHOOKS_FILE,
            "XAI_PROBE": relay.XAI_PROBE,
            "MANIFEST_FETCHER": relay.MANIFEST_FETCHER,
            "busy": relay.stats["busy"],
            "env": {name: os.environ.get(name) for name in (
                "STATION_CA_FILE", "STATION_REVOCATION_FILE", "RELAY_INSTALLED",
                "UPDATE_REQUEST", "VOICE_BUSY")},
        }
        self.probes = {"n": 0}
        relay.KEY = KEY
        relay.WEBHOOKS_FILE = os.path.join(self.tmp, "webhooks.json")
        relay.stats["busy"] = 0
        relay._seen.clear()
        relay._xai_cache.update(mono=None, value=None)
        relay.MANIFEST_FETCHER = None

        def probe():
            self.probes["n"] += 1
            return "ok"

        relay.XAI_PROBE = probe
        os.environ["STATION_CA_FILE"] = os.path.join(self.tmp, "ca.crt")
        os.environ["STATION_REVOCATION_FILE"] = os.path.join(self.tmp, "revocation.json")
        os.environ["RELAY_INSTALLED"] = os.path.join(self.tmp, "installed.json")
        os.environ["UPDATE_REQUEST"] = os.path.join(self.tmp, "update-request.json")
        os.environ["VOICE_BUSY"] = os.path.join(self.tmp, "voice-busy")
        self.parent = issue_ca()
        self.ca, self.leaf = issue_station(parent=self.parent)
        with open(os.environ["STATION_CA_FILE"], "wb") as handle:
            handle.write(self.ca.public_bytes(serialization.Encoding.PEM))
        installed = {
            "version": "v1.0.0", "relay": "v1.0.0", "station": "v1.0.0",
            "compat": {"station_min_relay": "v1.0.0", "relay_min_station": "v1.0.0"},
        }
        with open(os.environ["RELAY_INSTALLED"], "w", encoding="utf-8") as handle:
            json.dump(installed, handle)
        with open(os.path.join(self.tmp, "audit-2026-10.log"), "w", encoding="utf-8") as handle:
            handle.write("2026-10-20T11:00:00Z bot ash ask what?\n")
            handle.write("2026-10-20T11:00:01Z bot ash leak " + KEY + "\n")
            handle.write("2026-10-20T11:00:02Z station relay update-queued v1.2.0\n")
            handle.write("2026-10-20T11:00:03Z bot ash ask what is the weather today\n")

    def tearDown(self):
        relay.KEY = self.saved["KEY"]
        relay.WEBHOOKS_FILE = self.saved["WEBHOOKS_FILE"]
        relay.XAI_PROBE = self.saved["XAI_PROBE"]
        relay.MANIFEST_FETCHER = self.saved["MANIFEST_FETCHER"]
        relay.stats["busy"] = self.saved["busy"]
        relay._xai_cache.update(mono=None, value=None)
        relay._seen.clear()
        for name, value in self.saved["env"].items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    async def _call(self, method, path, handler, headers, body=None):
        # Handlers are called directly. This environment cannot bind a socket.
        del method, path

        class _Req:
            def __init__(self, hdrs, payload):
                self.headers = hdrs
                self._body = payload

            async def read(self):
                return self._body

        resp = await handler(_Req(headers, body if body is not None else b""))
        raw = resp.body if isinstance(getattr(resp, "body", None), (bytes, bytearray)) else b""
        return resp.status, bytes(raw)

    async def test_status_has_no_secrets(self):
        status, raw = await self._call("GET", "/admin/status", relay.admin_status, cert_headers(self.leaf))
        self.assertEqual(status, 200)
        text = raw.decode()
        doc = json.loads(text)
        self.assertEqual(set(doc), {
            "version", "signed_tag", "compat", "last_seen", "xai", "key_last4",
            "cert_expiry", "pii_mode", "rate_cap", "audit", "rate",
        })
        self.assertIsNone(doc["signed_tag"])
        self.assertEqual(doc["version"], "v1.0.0")
        self.assertEqual(doc["xai"], "ok")
        self.assertEqual(doc["key_last4"], "wxyz")
        self.assertNotIn(KEY, text)
        self.assertNotIn("what?", text)
        self.assertNotIn("weather", text)
        self.assertEqual(doc["audit"], ["2026-10-20T11:00:02Z station relay update-queued v1.2.0"])
        self.assertIsInstance(doc["pii_mode"], bool)
        self.assertIn("used_s", doc["rate_cap"])
        self.assertEqual(doc["cert_expiry"], _iso(self.leaf.not_valid_after_utc))
        self.assertRegex(doc["last_seen"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertEqual(self.probes["n"], 1)
        status, _raw = await self._call("GET", "/admin/status", relay.admin_status, cert_headers(self.leaf))
        self.assertEqual(status, 200)
        self.assertEqual(self.probes["n"], 1)
        relay._xai_cache["mono"] = 0
        await self._call("GET", "/admin/status", relay.admin_status, cert_headers(self.leaf))
        self.assertEqual(self.probes["n"], 2)

    async def test_relay_san_revoked_and_bad_fingerprint_are_refused(self):
        _ca, relay_leaf = issue_station(san="relay-0123456789abcdef", serial=0x22, parent=self.parent)
        status, _raw = await self._call("GET", "/admin/status", relay.admin_status, cert_headers(relay_leaf))
        self.assertEqual(status, 403)
        status, _raw = await self._call(
            "GET", "/admin/status", relay.admin_status, cert_headers(self.leaf, fp="ab" * 32))
        self.assertEqual(status, 403)
        with open(os.environ["STATION_REVOCATION_FILE"], "w", encoding="utf-8") as handle:
            json.dump({"serials": [format(self.leaf.serial_number, "x")]}, handle)
        status, _raw = await self._call("GET", "/admin/status", relay.admin_status, cert_headers(self.leaf))
        self.assertEqual(status, 403)
        with open(os.environ["STATION_REVOCATION_FILE"], "w", encoding="utf-8") as handle:
            handle.write("{")
        status, _raw = await self._call("GET", "/admin/status", relay.admin_status, cert_headers(self.leaf))
        self.assertEqual(status, 403)

    async def test_bad_args_do_not_fetch(self):
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return json.dumps(manifest_doc()).encode()

        relay.MANIFEST_FETCHER = fetch
        bad = b'{"version":"v1.2.0","digest":"' + DECOY.encode() + b'"}'
        status, raw = await self._call("POST", "/admin/update", relay.admin_update, cert_headers(self.leaf), bad)
        self.assertEqual(status, 400)
        self.assertEqual(calls["n"], 0)
        self.assertNotIn(DECOY, raw.decode())
        audit = ""
        for name in os.listdir(self.tmp):
            if name.startswith("audit-"):
                audit += open(os.path.join(self.tmp, name), encoding="utf-8").read()
        self.assertIn("update-rejected bad-args", audit)
        self.assertFalse(os.path.exists(os.environ["UPDATE_REQUEST"]))

    async def test_busy_is_deferred_without_a_fetch(self):
        calls = {"n": 0}
        relay.MANIFEST_FETCHER = lambda: calls.__setitem__("n", calls["n"] + 1)
        relay.stats["busy"] = 1
        status, raw = await self._call(
            "POST", "/admin/update", relay.admin_update, cert_headers(self.leaf),
            b'{"version":"v1.2.0"}')
        self.assertEqual(status, 409)
        self.assertEqual(calls["n"], 0)
        self.assertIn(b"deferred", raw)
        self.assertFalse(os.path.exists(os.environ["UPDATE_REQUEST"]))

    async def test_update_enqueues_version_only(self):
        calls = {"n": 0}
        doc = manifest_doc()

        def fetch():
            calls["n"] += 1
            return json.dumps(doc).encode()

        relay.MANIFEST_FETCHER = fetch
        status, raw = await self._call(
            "POST", "/admin/update", relay.admin_update, cert_headers(self.leaf),
            b'{"version":"v1.2.0"}')
        self.assertEqual(status, 200, raw)
        self.assertEqual(calls["n"], 1)
        body = open(os.environ["UPDATE_REQUEST"], encoding="utf-8").read()
        self.assertEqual(json.loads(body), {"version": "v1.2.0"})
        self.assertNotIn("sha256", body)
        self.assertNotIn(KEY, raw.decode())
        mode = os.stat(os.environ["UPDATE_REQUEST"]).st_mode & 0o777
        self.assertEqual(mode, 0o600)


class HostUpdate(unittest.TestCase):
    def test_override_digests_come_only_from_the_manifest(self):
        mem = Mem()
        paths = cli.paths_for("linux")
        doc = manifest_doc()
        mem.files[host_update.installed_path(paths)] = json.dumps({
            "version": "v1.0.0", "relay": "v1.0.0", "station": "v1.0.0", "note": DECOY,
        }).encode()
        mem.files[host_update.request_path(paths)] = b'{"version":"v1.2.0"}\n'
        audits = []

        def fetch(_url, _context):
            return json.dumps(doc).encode()

        code = host_update.run_update(
            mem, paths, fetch, lambda event, detail: audits.append((event, detail)),
            health_get=lambda: 200, now=_now(), sleep=lambda _s: None, mono=lambda: 0,
        )
        self.assertEqual(code, 0, audits)
        override = mem.files[host_update.override_path(paths)].decode()
        self.assertIn(RELAY_DIGEST, override)
        self.assertIn(CADDY_DIGEST, override)
        self.assertNotIn(DECOY, override)
        pulls = [argv for argv in mem.runs if argv[:2] == ["docker", "pull"]]
        self.assertEqual(sorted(argv[2] for argv in pulls), sorted([
            "caddy@" + CADDY_DIGEST, "relay@" + RELAY_DIGEST,
        ]))
        joined = " ".join(" ".join(argv) for argv in mem.runs)
        self.assertNotIn(DECOY, joined)
        self.assertNotIn(host_update.request_path(paths), mem.files)
        self.assertIn(("update-applied", "v1.2.0"), audits)

    def test_bad_request_does_not_fetch_or_pull(self):
        mem = Mem()
        paths = cli.paths_for("linux")
        mem.files[host_update.installed_path(paths)] = b'{"version":"v1.0.0","relay":"v1.0.0"}\n'
        mem.files[host_update.request_path(paths)] = (
            b'{"version":"v1.2.0","digest":"' + DECOY.encode() + b'"}\n'
        )
        audits = []

        def fetch(_url, _context):
            raise AssertionError("fetched")

        code = host_update.run_update(
            mem, paths, fetch, lambda event, detail: audits.append((event, detail)),
            health_get=lambda: 200,
        )
        self.assertEqual(code, 1)
        self.assertEqual(audits, [("update-rejected", "bad-args")])
        self.assertEqual(mem.runs, [])

    def test_check_update_off_by_default(self):
        mem = Mem()
        paths = cli.paths_for("linux")

        def fetch(_url, _context):
            raise AssertionError("fetched")

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["check-update"], system=mem, fetcher=fetch)
        self.assertEqual(code, 0)
        self.assertIn("autocheck off", buf.getvalue())
        mem.files[host_update.settings_path(paths)] = b'{"autocheck": false}\n'
        with redirect_stdout(io.StringIO()):
            code = cli.main(["check-update"], system=mem, fetcher=fetch)
        self.assertEqual(code, 0)

    def test_check_update_on_fetches(self):
        mem = Mem()
        paths = cli.paths_for("linux")
        mem.files[host_update.settings_path(paths)] = b'{"autocheck": true}\n'
        mem.files[host_update.installed_path(paths)] = b'{"relay":"v1.0.0"}\n'
        seen = {"n": 0}

        def fetch(_url, _context):
            seen["n"] += 1
            return json.dumps(manifest_doc()).encode()

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["check-update"], system=mem, fetcher=fetch, now=_now())
        self.assertEqual(code, 0, buf.getvalue())
        self.assertEqual(seen["n"], 1)
        self.assertIn("v1.2.0", buf.getvalue())


class ManifestTool(unittest.TestCase):
    def test_output_passes_validation_and_is_unsigned(self):
        folder = tempfile.mkdtemp()
        blob = os.path.join(folder, "station-v1.2.0.tar.gz")
        payload = b"station-bytes"
        with open(blob, "wb") as handle:
            handle.write(payload)
        script = os.path.join(ROOT, "tools", "release", "make-manifest.py")
        proc = subprocess.run(
            [
                sys.executable, script,
                "--version", "v1.2.0",
                "--floor", "v1.0.0",
                "--station-tarball", blob,
                "--image", "relay=" + RELAY_DIGEST,
                "--image", "caddy=" + CADDY_DIGEST,
                "--station-min-relay", "v1.1.0",
                "--relay-min-station", "v1.0.0",
                "--now", "2026-10-20T12:00:00Z",
            ],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        checked = release.parse_manifest(doc, now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc))
        self.assertEqual(checked["components"]["station"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(checked["components"]["relay"]["images"]["relay"], RELAY_DIGEST)
        self.assertNotIn("signature", doc)
        self.assertIsNone(doc.get("signed_tag"))
        self.assertNotIn("minisign", proc.stdout)


class Installers(unittest.TestCase):
    def test_shell_dry_run_and_syntax(self):
        script = os.path.join(ROOT, "relay", "relay-install.sh")
        ps1 = os.path.join(ROOT, "relay", "relay-install.ps1")
        syntax = subprocess.run(["sh", "-n", script], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        folder = tempfile.mkdtemp()
        before = set(os.listdir(folder))
        proc = subprocess.run(
            ["sh", script, "--dry-run", "--prefix", folder],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for phrase in (
            "not from the station", "release.py", "grok-relay", "digest",
            "mode 600", "read -s", "pin it", "autocheck off", folder,
        ):
            self.assertIn(phrase, proc.stdout)
        self.assertEqual(set(os.listdir(folder)), before)
        source = open(script, encoding="utf-8").read()
        self.assertIn("read -s", source)
        self.assertNotIn("ExecutionPolicy", source)
        windows = open(ps1, encoding="utf-8").read()
        self.assertIn("AsSecureString", windows)
        self.assertNotIn("ExecutionPolicy", windows)
        self.assertNotIn("Bypass", windows)
        self.assertNotIn("bypass", windows.lower())

    def test_minisign_paths_are_gone(self):
        for rel in ("relay/grok_relay_cli.py", "relay/grok-relay.ps1", "tests/test_relay_cli.py"):
            text = open(os.path.join(ROOT, rel), encoding="utf-8").read().lower()
            self.assertNotIn("minisign", text, rel)

    def test_caddy_control_channel_keeps_bot_paths(self):
        text = open(os.path.join(ROOT, "relay", "Caddyfile"), encoding="utf-8").read()
        self.assertIn("require_and_verify", text)
        self.assertIn("trusted_ca_cert_file /station-ca/ca.crt", text)
        self.assertIn("https://:8445", text)
        self.assertIn("path /ws /health", text)
        self.assertIn("path /voice/avatar", text)
        self.assertIn("header_up -X-Client-Cert", text)
        self.assertIn("https://:8443", text)
        self.assertIn("https://:8444", text)

    def test_transport_fetches_unverified_then_pins(self):
        _ca, leaf = issue_station()
        pem = leaf.public_bytes(serialization.Encoding.PEM)
        seen = []

        def dial(address, verify, pin, payload):
            seen.append((address, verify, pin, payload))
            if not verify:
                self.assertIsNone(payload)
                return pem
            method, path, body = payload
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/pair/submit")
            self.assertIn(b"csr_pem", body)
            self.assertNotIn(b"PRIVATE", body)
            self.assertIn(b"-----BEGIN CERTIFICATE-----", pin)
            return b'{"ok":true}\n'

        transport = pair_transport.StationTransport("192.0.2.10:8443", dial=dial)
        transport.fetch_station_cert()
        csr = "-----BEGIN CERTIFICATE REQUEST-----\nMIIB\n-----END CERTIFICATE REQUEST-----\n"
        transport.send(csr, b"\x11" * 32)
        self.assertFalse(seen[0][1])
        self.assertTrue(seen[1][1])
        self.assertEqual(seen[0][0], "192.0.2.10:8443")


class Redirects(unittest.TestCase):
    def _context(self):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def test_github_redirects_to_objects(self):
        seen = []

        def opener(url, context):
            seen.append(url)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            if len(seen) == 1:
                return 302, "https://objects.githubusercontent.com/example/manifest.json", b""
            return 200, None, b"{\"version\":\"v1.2.0\"}"

        body = release_fetch.https_get(release.MANIFEST_URL, self._context(), opener=opener)
        self.assertEqual(body, b'{"version":"v1.2.0"}')
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[1].startswith("https://objects.githubusercontent.com/"))

    def test_off_host_http_and_sixth_hop_are_rejected(self):
        def off(_url, _context):
            return 302, "https://example.invalid/manifest.json", b""

        with self.assertRaises(release.ManifestError) as caught:
            release_fetch.https_get(release.MANIFEST_URL, self._context(), opener=off)
        self.assertEqual(caught.exception.reason, "fetch-failed")

        def plain(_url, _context):
            return 302, "http://objects.githubusercontent.com/manifest.json", b""

        with self.assertRaises(release.ManifestError) as caught:
            release_fetch.https_get(release.MANIFEST_URL, self._context(), opener=plain)
        self.assertEqual(caught.exception.reason, "fetch-failed")

        def loop(_url, _context):
            return 302, release.MANIFEST_URL, b""

        with self.assertRaises(release.ManifestError) as caught:
            release_fetch.https_get(release.MANIFEST_URL, self._context(), opener=loop)
        self.assertEqual(caught.exception.reason, "fetch-failed")

        bare = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        bare.check_hostname = False
        bare.verify_mode = ssl.CERT_NONE
        with self.assertRaises(release.ManifestError):
            release_fetch.https_get(release.MANIFEST_URL, bare, opener=loop)


class Healthz(unittest.IsolatedAsyncioTestCase):
    async def test_healthz_is_ok_without_secrets(self):
        resp = await relay.healthz(None)
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body), {"ok": True})
        self.assertNotIn(KEY, resp.body.decode())


if __name__ == "__main__":
    unittest.main()
