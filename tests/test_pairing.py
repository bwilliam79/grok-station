"""Relay pairing: station window, installer client, and the CA that signs the client cert.

C1 aborts unless the operator types yes, and then sends nothing.
C2 an expired code is rejected and counts as a try.
C3 a used code is rejected.
C4 the 6th proof is rejected even when it is correct.
C5 a proof bound to a different fingerprint fails.
C6 confirm without a prior submit signs nothing.
C10 a revoked certificate does not verify.
C15 the wrong issuer, an expired cert, and a cert that is not clientAuth do not verify.
C18 the listener port follows the window.
C19 only local-network sources are accepted.
C22 a CSR with serverAuth or any other SAN is refused.
"""
import base64, datetime, hashlib, hmac, io, ipaddress, json, os, re, secrets, sys, unittest
from contextlib import redirect_stdout
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin"))
sys.path.insert(0, os.path.join(ROOT, "relay"))

import pairing
import pair_client

AUDIT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z "
    r"admin station "
    r"(pair-start|pair-port-open|pair-port-closed|pair-code-new|pair-cancel|pair-locked|"
    r"pair-try-bad|pair-csr-signed|unpair) "
    r"(-|[1-5]/5|[0-9a-f]{8})$"
)
ADDR = "192.0.2.10"


class Clock:
    def __init__(self, t=1_700_000_000):
        self.t = float(t)

    def __call__(self):
        return self.t


def station_cert():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "station.example.invalid")])
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(0x1000)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    return cert


def make_csr(pairing_id=None, *, eku=None, sans=None, ca_bit=False, curve=None):
    pairing_id = pairing_id or secrets.token_hex(8)
    key = ec.generate_private_key(curve or ec.SECP256R1())
    dns = f"relay-{pairing_id}"
    names = sans if sans is not None else [x509.DNSName(dns)]
    builder = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns)]))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
    )
    if ca_bit:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
    if eku is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage(list(eku)), critical=False)
    elif eku is None and sans is not None:
        pass
    else:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
    csr = builder.sign(key, hashes.SHA256())
    return csr, pairing_id, key


def pem(obj):
    return obj.public_bytes(serialization.Encoding.PEM).decode("ascii")


class Capture:
    def __init__(self, cert_pem, on_send=None):
        self.cert_pem = cert_pem
        self.on_send = on_send
        self.sent = []
        self.fetched = 0

    def fetch_station_cert(self):
        self.fetched += 1
        return self.cert_pem

    def send(self, csr_pem, proof):
        self.sent.append((csr_pem, proof))
        if self.on_send:
            return self.on_send(csr_pem, proof)


def assert_audits(case, lines):
    for line in lines:
        case.assertRegex(line, AUDIT)


def assert_code_absent(case, window, *codes):
    norms = []
    for code in codes:
        norm = code.replace("-", "").upper()
        norms.append(norm)
        case.assertNotEqual(window._proof, norm.encode("ascii"))
    case.assertIsInstance(window._proof, bytes)
    case.assertEqual(len(window._proof), 32)
    latest = norms[-1]
    case.assertEqual(
        window._proof,
        hmac.new(latest.encode("ascii"), window.fingerprint, hashlib.sha256).digest(),
    )
    for key, val in vars(window).items():
        case.assertNotIn("code", key)
        if key in ("_pending_pem", "_proof"):
            continue
        if isinstance(val, str):
            upper = val.upper()
            for norm in norms:
                case.assertNotIn(norm, upper)
                case.assertNotIn(f"{norm[:4]}-{norm[4:]}", upper)
        elif isinstance(val, list):
            blob = "\n".join(item for item in val if isinstance(item, str)).upper()
            for norm in norms:
                case.assertNotIn(norm, blob)
                case.assertNotIn(f"{norm[:4]}-{norm[4:]}", blob)


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cert = station_cert()
        self.cert_pem = pem(self.cert)
        self.ca = pairing.StationCA(clock=self.clock)
        self.window = pairing.PairingWindow(self.cert, self.ca, clock=self.clock)

    def _open(self, window=None, when=None):
        window = window or self.window
        return window.open(self.clock.t if when is None else when)

    def test_constants_and_default_rng_is_secrets(self):
        self.assertEqual(pairing.WINDOW_S, 600)
        self.assertEqual(pairing.STEPUP_S, 300)
        self.assertEqual(pairing.MAX_TRIES, 5)
        self.assertIs(pairing.PairingWindow(self.cert)._rng, secrets.token_bytes)

    def test_stepup_must_be_fresh(self):
        self.assertIsNone(self.window.open(None))
        self.assertIsNone(self.window.open(True))
        self.assertIsNone(self.window.open(False))
        self.assertIsNone(self.window.open("now"))
        self.assertIsNone(self.window.open(self.clock.t + 1))
        self.assertIsNone(self.window.open(self.clock.t - 301))
        self.assertFalse(self.window.port_should_be_open())
        self.assertEqual(self.window.audits, [])
        code = self.window.open(self.clock.t - 300)
        self.assertRegex(code, r"^[A-Z2-7]{4}-[A-Z2-7]{4}$")
        self.assertEqual(self.window.audits[0].split()[3:], ["pair-start", "-"])
        self.assertEqual(self.window.audits[1].split()[3:], ["pair-port-open", "-"])
        self.assertIsNone(self.window.open(self.clock.t))  # already open

    def test_code_entropy_format_and_not_stored(self):
        seen = []

        def rng(n):
            seen.append(n)
            return b"\xff\x10\x20\x30\x40"

        window = pairing.PairingWindow(self.cert, self.ca, clock=self.clock, rng=rng)
        code = window.open(self.clock.t)
        self.assertEqual(seen, [5])  # 5 bytes = 40 bits
        raw = base64.b32encode(b"\xff\x10\x20\x30\x40").decode("ascii")
        self.assertEqual(len(raw), 8)
        self.assertRegex(raw, r"[A-Z2-7]{8}")
        self.assertEqual(code, f"{raw[:4]}-{raw[4:]}")
        assert_code_absent(self, window, code)
        assert_audits(self, window.audits)
        codes = set()
        for _ in range(24):
            other = pairing.PairingWindow(self.cert, clock=self.clock)
            made = other.open(self.clock.t)
            self.assertRegex(made, r"^[A-Z2-7]{4}-[A-Z2-7]{4}$")
            codes.add(made)
            norm = made.replace("-", "")
            self.assertEqual(len(norm), 8)
            self.assertEqual(other._proof, pairing.code_proof(made, other.fingerprint))
        self.assertGreater(len(codes), 20)

    def test_new_code_kills_the_old_one(self):
        def rng(n):
            rng.calls += 1
            return b"\x01\x02\x03\x04\x05" if rng.calls == 1 else b"\x05\x04\x03\x02\x01"

        rng.calls = 0
        window = pairing.PairingWindow(self.cert, self.ca, clock=self.clock, rng=rng)
        first = window.open(self.clock.t)
        second = window.new_code()
        self.assertNotEqual(first, second)
        self.assertEqual(window.tries, 0)
        self.assertEqual(self.clock.t + 600, window.expires_at)
        csr, _, _ = make_csr()
        body = pem(csr)
        old = window.submit(body, pairing.code_proof(first, window.fingerprint))
        self.assertFalse(old["ok"])
        self.assertEqual(old["reason"], "bad-proof")
        self.assertEqual(window.tries, 1)
        ok = window.submit(body, pairing.code_proof(second, window.fingerprint))
        self.assertTrue(ok["ok"])
        self.assertIn("pair-code-new", "\n".join(window.audits))
        assert_code_absent(self, window, first, second)
        self.assertNotIn(first.replace("-", ""), "\n".join(window.audits))
        # A new code does not refill the try budget, and the code it replaced is dead.
        again = pairing.PairingWindow(self.cert, self.ca, clock=self.clock)
        previous = again.open(self.clock.t)
        again.tries = 4
        rotated = again.new_code()
        self.assertIsNotNone(rotated)
        self.assertNotEqual(previous, rotated)
        self.assertEqual(again.tries, 4)
        burned = again.submit(body, pairing.code_proof(previous, again.fingerprint))
        self.assertEqual(burned["reason"], "locked")
        self.assertEqual(again.tries, 5)
        self.assertFalse(again.port_should_be_open())
        self.assertIsNone(again.new_code())

    def test_fingerprint_format_is_the_full_64_hex_digits(self):
        raw = bytes(range(32))
        text = "\n".join([
            "0001 0203 0405 0607",
            "0809 0A0B 0C0D 0E0F",
            "1011 1213 1415 1617",
            "1819 1A1B 1C1D 1E1F",
        ])
        self.assertEqual(pairing.format_fingerprint(raw), text)
        self.assertEqual(pair_client.format_fingerprint(raw), text)
        lines = text.split("\n")
        self.assertEqual(len(lines), 4)
        for line in lines:
            groups = line.split(" ")
            self.assertEqual(len(groups), 4)
            for group in groups:
                self.assertRegex(group, r"^[0-9A-F]{4}$")
        self.assertEqual("".join(text.split()), raw.hex().upper())
        self.assertEqual(len("".join(text.split())), 64)
        self.assertNotIn("...", text)
        digest = hashlib.sha256(self.cert.public_bytes(serialization.Encoding.DER)).digest()
        self.assertEqual(len(digest), 32)
        shown = pairing.format_fingerprint(self.cert)
        self.assertEqual(shown, pairing.format_fingerprint(digest))
        self.assertEqual(shown, pair_client.format_fingerprint(self.cert_pem))
        self.assertEqual("".join(shown.split()), digest.hex().upper())

    def test_happy_path_station_and_installer(self):
        code = self._open()
        held = {}

        def on_send(csr_pem, proof):
            held["submit"] = self.window.submit(csr_pem, proof)
            return held["submit"]

        transport = Capture(self.cert_pem, on_send)
        prompts = []

        def prompt(message):
            prompts.append(message)
            return "yes"

        buf = io.StringIO()
        with redirect_stdout(buf):
            out = pair_client.run_pairing(transport, prompt, ADDR, code)
        printed = buf.getvalue().splitlines()
        self.assertEqual(prompts, ["Type yes to continue"])
        self.assertEqual(transport.fetched, 1)
        self.assertEqual(len(transport.sent), 1)
        sent_pem, sent_proof = transport.sent[0]
        self.assertEqual(sent_pem, out["csr_pem"])
        self.assertEqual(sent_proof, out["proof"])
        self.assertEqual(len(sent_proof), 32)
        self.assertNotIn(b"PRIVATE", sent_pem.encode())
        priv = out["key"].private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        self.assertNotIn(priv, sent_pem.encode())
        self.assertNotIn(code.replace("-", ""), sent_pem)
        self.assertNotIn(ADDR, buf.getvalue())
        self.assertNotIn(ADDR, out["csr_pem"])
        self.assertNotIn(sent_proof.hex(), buf.getvalue())
        self.assertEqual(printed[:4], out["station_fingerprint"].splitlines())
        self.assertEqual(printed[4:], out["relay_fingerprint"].splitlines())
        self.assertEqual(len(printed), 8)
        self.assertTrue(held["submit"]["ok"])
        self.assertEqual(held["submit"]["relay_fingerprint"], out["relay_fingerprint"])
        self.assertTrue(self.window.port_should_be_open())
        issued = self.window.confirm_match()
        self.assertIsNotNone(issued)
        cert, serial8 = issued
        self.assertRegex(serial8, r"^[0-9a-f]{8}$")
        self.assertEqual(serial8, format(cert.serial_number, "x")[:8])
        self.assertEqual(
            cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo),
            out["key"].public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo),
        )
        dns = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        self.assertEqual(dns, [f"relay-{out['pairing_id']}"])
        self.assertEqual(
            cert.not_valid_after_utc - cert.not_valid_before_utc, datetime.timedelta(days=365))
        eku = list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
        self.assertEqual(eku, [ExtendedKeyUsageOID.CLIENT_AUTH])
        self.assertFalse(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        self.assertTrue(self.ca.verify_client_cert(cert))
        self.assertTrue(self.ca.verify_client_cert(pem(cert)))
        self.assertFalse(self.window.port_should_be_open())
        self.assertIsNone(self.window.confirm_match())
        blob = "\n".join(self.window.audits + self.ca.audits)
        self.assertIn("pair-start", blob)
        self.assertIn("pair-port-open", blob)
        self.assertIn("pair-port-closed", blob)
        self.assertIn(f"pair-csr-signed {serial8}", blob)
        self.assertNotIn(code.replace("-", ""), "\n".join(self.window.audits))
        self.assertNotIn(ADDR, blob)
        self.assertNotIn("example.invalid", blob)
        self.assertNotIn("PRIVATE", blob)
        assert_audits(self, self.window.audits + self.ca.audits)
        assert_code_absent(self, self.window, code)

    def test_c1_abort_unless_exact_yes_sends_nothing(self):
        answers = ["Yes", "YES", "y", "yes ", " yes", "yes\n", "yes.", "", None, "no", "yess"]
        for answer in answers:
            transport = Capture(self.cert_pem)
            with mock.patch("pair_client.ec.generate_private_key", side_effect=AssertionError("key")):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    out = pair_client.run_pairing(transport, lambda _m, answer=answer: answer, ADDR, "ABCD-2345")
            self.assertFalse(out["ok"], answer)
            self.assertEqual(out["reason"], "aborted", answer)
            self.assertEqual(transport.sent, [], answer)
            self.assertEqual(transport.fetched, 1, answer)
            self.assertEqual(len(buf.getvalue().splitlines()), 4, answer)
            self.assertNotIn(ADDR, buf.getvalue())

    def test_c1_prompt_text_and_order(self):
        log = []
        transport = Capture(self.cert_pem)

        def prompt(message):
            log.append(message)
            return "no"

        with redirect_stdout(io.StringIO()):
            pair_client.run_pairing(transport, prompt, ADDR, "ABCD-2345")
        self.assertEqual(log, ["Type yes to continue"])
        self.assertEqual(transport.sent, [])

    def test_c2_expired_code_counts_a_try(self):
        code = self._open()
        csr, _, _ = make_csr()
        body = pem(csr)
        proof = pairing.code_proof(code, self.window.fingerprint)
        self.clock.t += 599
        self.assertTrue(self.window.port_should_be_open())
        self.clock.t += 1
        result = self.window.submit(body, proof)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "expired")
        self.assertEqual(self.window.tries, 1)
        self.assertEqual(result["tries"], 1)
        self.assertFalse(self.window.port_should_be_open())
        self.assertIsNone(self.window.confirm_match())
        blob = "\n".join(self.window.audits)
        self.assertIn("pair-try-bad 1/5", blob)
        self.assertIn("pair-port-closed", blob)
        self.assertNotIn(code.replace("-", ""), blob)
        # still inside the window, the same proof would have worked
        clock = Clock()
        window = pairing.PairingWindow(self.cert, self.ca, clock=clock)
        fresh = window.open(clock.t)
        clock.t += 599
        csr2, _, _ = make_csr()
        ok = window.submit(pem(csr2), pairing.code_proof(fresh, window.fingerprint))
        self.assertTrue(ok["ok"])

    def test_c3_used_code_rejected(self):
        code = self._open()
        csr, _, _ = make_csr()
        body = pem(csr)
        proof = pairing.code_proof(code.lower(), self.window.fingerprint)  # normalized
        first = self.window.submit(body, proof)
        self.assertTrue(first["ok"])
        second = self.window.submit(body, proof)
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], "used")
        self.assertEqual(self.window.tries, 0)
        issued = self.window.confirm_match()
        self.assertIsNotNone(issued)
        third = self.window.submit(body, proof)
        self.assertEqual(third["reason"], "used")
        self.assertEqual(self.window.tries, 0)

    def test_c4_sixth_try_rejected_even_if_correct(self):
        # four bad proofs, then the right one, still counts as inside the budget
        early = pairing.PairingWindow(self.cert, self.ca, clock=self.clock)
        early_code = early.open(self.clock.t)
        csr, _, _ = make_csr()
        body = pem(csr)
        for n in range(4):
            bad = early.submit(body, b"\x01" * 32)
            self.assertFalse(bad["ok"])
            self.assertEqual(bad["reason"], "bad-proof")
            self.assertEqual(early.tries, n + 1)
            self.assertTrue(early.port_should_be_open())
        good = early.submit(body, pairing.code_proof(early_code, early.fingerprint))
        self.assertTrue(good["ok"])
        self.assertIsNotNone(early.confirm_match())

        code = self._open()
        csr, _, _ = make_csr()
        body = pem(csr)
        proof = pairing.code_proof(code, self.window.fingerprint)
        calls = []
        real = hmac.compare_digest

        def wrapped(a, b):
            calls.append((a, b))
            return real(a, b)

        with mock.patch("pairing.hmac.compare_digest", wrapped):
            for n in range(5):
                bad = self.window.submit(body, bytes([n + 3]) * 32)
                self.assertFalse(bad["ok"])
            self.assertEqual(self.window.phase, "locked")
            self.assertFalse(self.window.port_should_be_open())
            calls.clear()
            sixth = self.window.submit(body, proof)
        self.assertFalse(sixth["ok"])
        self.assertEqual(sixth["reason"], "locked")
        self.assertEqual(self.window.tries, 5)
        self.assertTrue(calls)  # compared, then rejected anyway
        self.assertIsNone(self.window.confirm_match())
        blob = "\n".join(self.window.audits)
        self.assertIn("pair-try-bad 5/5", blob)
        self.assertIn("pair-locked", blob)
        self.assertIn("pair-port-closed", blob)
        self.assertEqual(sum(1 for line in self.window.audits if line.endswith("pair-locked -")), 1)
        self.assertNotIn(code.replace("-", ""), blob)
        assert_audits(self, self.window.audits)

    def test_c5_proof_for_a_different_fingerprint_fails(self):
        code = self._open()
        csr, _, _ = make_csr()
        other = hashlib.sha256(b"other-cert").digest()
        result = self.window.submit(pem(csr), pairing.code_proof(code, other))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "bad-proof")
        self.assertEqual(self.window.tries, 1)
        self.assertTrue(self.window.port_should_be_open())
        self.assertIsNone(self.window.confirm_match())
        # the proof of the certificate that was actually shown still works
        ok = self.window.submit(pem(csr), pair_client.code_proof(code, self.window.fingerprint))
        self.assertTrue(ok["ok"])

    def test_c6_confirm_without_submit_signs_nothing(self):
        self.assertIsNone(self.window.confirm_match())
        code = self._open()
        self.assertIsNone(self.window.confirm_match())
        before = list(self.ca.audits)
        csr, _, _ = make_csr()
        bad = self.window.submit(pem(csr), b"\x02" * 32)
        self.assertFalse(bad["ok"])
        self.assertIsNone(self.window.confirm_match())
        self.assertEqual(self.ca.audits, before)
        transport = Capture(self.cert_pem)

        def prompt(_message):
            return "no"

        with redirect_stdout(io.StringIO()):
            out = pair_client.run_pairing(transport, prompt, ADDR, code)
        self.assertEqual(out["reason"], "aborted")
        self.assertEqual(transport.sent, [])
        self.assertIsNone(self.window.confirm_match())
        self.assertFalse(any("pair-csr-signed" in line for line in self.ca.audits))

    def test_c10_revoked_cert_rejected(self):
        _csr, pairing_id, _key = make_csr()
        # sign the real CSR object
        csr, pairing_id, _key = make_csr(pairing_id)
        cert, serial8 = self.ca.sign_csr(csr, pairing_id)
        self.assertTrue(self.ca.verify_client_cert(cert))
        self.assertFalse(self.ca.is_revoked(cert))
        self.ca.revoke(cert.serial_number)
        self.assertTrue(self.ca.is_revoked(cert))
        self.assertTrue(self.ca.is_revoked(format(cert.serial_number, "x")))
        self.assertFalse(self.ca.verify_client_cert(cert))
        self.assertFalse(self.ca.verify_client_cert(pem(cert)))
        blob = "\n".join(self.ca.audits)
        self.assertIn(f"unpair {serial8}", blob)
        full = format(cert.serial_number, "x")
        unpair = [line for line in self.ca.audits if " unpair " in line][-1]
        if len(full) > 8:
            self.assertNotIn(full, unpair)
        doc = json.loads(self.ca.revocation_json())
        self.assertEqual(doc["serials"], [full])
        other = pairing.StationCA(clock=self.clock)
        other.load_revocation_json(self.ca.revocation_json())
        self.assertTrue(other.is_revoked(full))
        self.assertFalse(any(full in line for line in self.ca.audits if len(full) > 8))
        assert_audits(self, self.ca.audits)
        # revoking again does not write a second line
        n = len(self.ca.audits)
        self.ca.revoke(cert)
        self.assertEqual(len(self.ca.audits), n)

    def test_c15_wrong_issuer_expired_and_not_client_auth(self):
        csr, pairing_id, key = make_csr()
        cert, _serial8 = self.ca.sign_csr(csr, pairing_id)
        self.assertTrue(self.ca.verify_client_cert(cert))
        other = pairing.StationCA(clock=self.clock)
        self.assertFalse(self.ca.verify_client_cert(other.cert))
        foreign_csr, foreign_id, _foreign_key = make_csr()
        foreign, _foreign_serial = other.sign_csr(foreign_csr, foreign_id)
        self.assertTrue(other.verify_client_cert(foreign))
        self.assertFalse(self.ca.verify_client_cert(foreign))
        self.clock.t = cert.not_valid_after_utc.timestamp() + 1
        self.assertFalse(self.ca.verify_client_cert(cert))
        self.clock.t = cert.not_valid_before_utc.timestamp()
        self.assertTrue(self.ca.verify_client_cert(cert))
        server_only = _leaf(self.ca, [ExtendedKeyUsageOID.SERVER_AUTH])
        both = _leaf(self.ca, [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH])
        missing = _leaf(self.ca, None)
        self.assertFalse(self.ca.verify_client_cert(server_only))
        self.assertFalse(self.ca.verify_client_cert(both))
        self.assertFalse(self.ca.verify_client_cert(missing))
        self.assertFalse(self.ca.verify_client_cert(b"not a cert"))
        self.assertFalse(self.ca.verify_client_cert(None))
        # the key on the installer matches a cert we just checked; silence unused
        self.assertIsNotNone(key)

    def test_c18_port_follows_the_window(self):
        self.assertFalse(self.window.port_should_be_open())
        self.assertIsNone(self.window.open(self.clock.t - 301))
        self.assertFalse(self.window.port_should_be_open())
        code = self._open()
        self.assertTrue(self.window.port_should_be_open())
        self.window.submit(pem(make_csr()[0]), b"\x09" * 32)
        self.assertTrue(self.window.port_should_be_open())
        self.assertTrue(self.window.cancel())
        self.assertFalse(self.window.port_should_be_open())
        blob = "\n".join(self.window.audits)
        self.assertIn("pair-cancel", blob)
        self.assertLess(blob.index("pair-cancel"), blob.index("pair-port-closed"))
        self.assertFalse(self.window.cancel())

        locked = pairing.PairingWindow(self.cert, self.ca, clock=self.clock)
        locked.open(self.clock.t)
        for _ in range(5):
            locked.submit("nope", b"\x00" * 32)
        self.assertFalse(locked.port_should_be_open())
        self.assertIn("pair-locked", "\n".join(locked.audits))

        expired = pairing.PairingWindow(self.cert, self.ca, clock=self.clock)
        expired.open(self.clock.t)
        self.clock.t += 600
        self.assertFalse(expired.port_should_be_open())
        self.assertEqual(expired.tries, 0)
        again = expired.port_should_be_open()
        self.assertFalse(again)
        closed = [line for line in expired.audits if line.endswith("pair-port-closed -")]
        self.assertEqual(len(closed), 1)

        later = Clock()
        done = pairing.PairingWindow(self.cert, self.ca, clock=later)
        fresh = done.open(later.t)
        self.assertTrue(done.port_should_be_open())
        csr, _, _ = make_csr()
        ok = done.submit(pem(csr), pairing.code_proof(fresh, done.fingerprint))
        self.assertTrue(ok["ok"])
        self.assertTrue(done.port_should_be_open())
        self.assertIsNotNone(done.confirm_match())
        self.assertFalse(done.port_should_be_open())
        closed = [line for line in done.audits if line.endswith("pair-port-closed -")]
        self.assertEqual(len(closed), 1)

        waiting = pairing.PairingWindow(self.cert, self.ca, clock=later)
        held = waiting.open(later.t)
        pending = waiting.submit(pem(csr), pairing.code_proof(held, waiting.fingerprint))
        self.assertTrue(pending["ok"])
        self.assertTrue(waiting.cancel())
        self.assertFalse(waiting.port_should_be_open())
        self.assertIsNone(waiting.confirm_match())
        self.assertIn("pair-cancel", "\n".join(waiting.audits))

    def test_c19_lan_source_ok(self):
        allowed = [
            "10.0.0.1", "10.255.255.254", "10.0.0.0",
            "172.16.0.1", "172.31.255.254", "172.16.0.0",
            "192.168.0.1", "192.168.255.254",
            "169.254.0.1", "169.254.255.255",
            "127.0.0.1", "127.255.255.1",
            "::1", "fc00::1", "fd00::1", "fe80::1", "FE80::1",
            "febf::1", "fe80::1%zone0", "::ffff:10.0.0.1", "::ffff:127.0.0.1",
        ]
        refused = [
            "192.0.2.1", "192.0.2.10", "198.51.100.1", "203.0.113.5",
            "8.8.8.8", "1.1.1.1", "100.64.0.1", "198.18.0.1", "192.0.0.8",
            "11.0.0.1", "172.15.255.255", "172.32.0.1", "192.167.255.255", "192.169.0.1",
            "169.255.0.1", "0.0.0.0", "255.255.255.255", "224.0.0.1",
            "::", "::2", "2001:db8::1", "fb00::1", "fec0::1", "ff00::1",
            "::ffff:192.0.2.1",
            "example.invalid", "station.example.invalid", "", "   ",
            "10.0.0.1 ", "10.0.0.01", "[fe80::1]", "192.0.2.1:443",
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            for ip in allowed:
                self.assertTrue(pairing.lan_source_ok(ip), ip)
            for ip in refused:
                self.assertFalse(pairing.lan_source_ok(ip), ip)
            self.assertFalse(pairing.lan_source_ok(None))
            self.assertFalse(pairing.lan_source_ok(b"10.0.0.1"))
            self.assertFalse(pairing.lan_source_ok(2130706433))
        self.assertEqual(buf.getvalue(), "")

    def test_c22_csr_constraints_and_ca_shape(self):
        ca_cert = self.ca.cert
        bc = ca_cert.extensions.get_extension_for_class(x509.BasicConstraints)
        self.assertTrue(bc.critical)
        self.assertTrue(bc.value.ca)
        self.assertEqual(bc.value.path_length, 0)
        ku = ca_cert.extensions.get_extension_for_class(x509.KeyUsage)
        self.assertTrue(ku.critical)
        self.assertTrue(ku.value.key_cert_sign)
        self.assertTrue(ku.value.crl_sign)
        self.assertEqual(ca_cert.public_key().curve.name, "secp256r1")
        with self.assertRaises(x509.ExtensionNotFound):
            ca_cert.extensions.get_extension_for_class(x509.NameConstraints)
        self.assertEqual(ca_cert.issuer, ca_cert.subject)

        good, pairing_id, key = make_csr()
        cert, serial8 = self.ca.sign_csr(good, pairing_id)
        self.assertRegex(serial8, r"^[0-9a-f]{8}$")
        self.assertEqual(cert.not_valid_after_utc - cert.not_valid_before_utc, datetime.timedelta(days=365))
        self.assertFalse(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual(list(san), [x509.DNSName(f"relay-{pairing_id}")])
        self.assertEqual(
            set(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value),
            {ExtendedKeyUsageOID.CLIENT_AUTH},
        )
        self.assertTrue(self.ca.verify_client_cert(cert))
        self.assertIn(f"pair-csr-signed {serial8}", "\n".join(self.ca.audits))
        self.assertEqual(key.curve.name, "secp256r1")

        before = len(self.ca.audits)
        pid = secrets.token_hex(8)
        cases = [
            make_csr(pid, eku=[ExtendedKeyUsageOID.SERVER_AUTH])[:2],
            make_csr(pid, eku=[ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH])[:2],
            make_csr(pid, eku=[ExtendedKeyUsageOID.CODE_SIGNING])[:2],
            make_csr(pid, sans=[x509.DNSName(f"relay-{pid}"), x509.DNSName("other.example.invalid")])[:2],
            make_csr(pid, sans=[x509.IPAddress(ipaddress.ip_address("192.0.2.9"))])[:2],
            make_csr(pid, sans=[x509.UniformResourceIdentifier("https://example.invalid/pair")])[:2],
            make_csr(pid, sans=[x509.DNSName(f"relay-{pid}.example.invalid")])[:2],
            make_csr("ab" * 8, sans=[x509.DNSName("relay-" + "AB" * 8)])[:2],
        ]
        for csr, claimed in cases:
            with self.assertRaises(pairing.PairingError):
                self.ca.sign_csr(csr, claimed if re.fullmatch(r"[0-9a-f]{16}", claimed) else pid)
        mismatch, mid, _key = make_csr()
        with self.assertRaises(pairing.PairingError):
            self.ca.sign_csr(mismatch, _other(mid))
        with self.assertRaises(pairing.PairingError):
            self.ca.sign_csr(good, "ABCDEFabcdef0123")  # not lowercase
        with self.assertRaises(pairing.PairingError):
            self.ca.sign_csr(good, "abc")
        with self.assertRaises(pairing.PairingError):
            self.ca.sign_csr(b"not-a-csr", pid)
        self.assertEqual(len(self.ca.audits), before)

        bare, bare_id, _key = _csr_without_eku()
        cert2, _s = self.ca.sign_csr(bare, bare_id)
        self.assertEqual(
            set(cert2.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value),
            {ExtendedKeyUsageOID.CLIENT_AUTH},
        )

    def test_bad_csr_does_not_consume_a_good_proof(self):
        code = self._open()
        proof = pairing.code_proof(" " + code.lower() + " ", self.window.fingerprint)
        refused = self.window.submit(b"not-a-csr", proof)
        self.assertEqual(refused["reason"], "bad-csr")
        self.assertEqual(self.window.tries, 0)
        self.assertTrue(self.window.port_should_be_open())
        csr, _, _ = make_csr()
        self.assertTrue(self.window.submit(pem(csr), proof)["ok"])

    def test_wrong_length_proof_counts_and_compare_digest_runs(self):
        code = self._open()
        with mock.patch("pairing.hmac.compare_digest", wraps=hmac.compare_digest) as spy:
            result = self.window.submit(pem(make_csr()[0]), "abcd")
        self.assertTrue(spy.called)
        self.assertEqual(result["reason"], "bad-proof")
        self.assertEqual(self.window.tries, 1)
        self.assertNotIn(code.replace("-", ""), "\n".join(self.window.audits))

    def test_read_xai_key_is_hidden_and_last4_only(self):
        seen = []

        def getpass_fn(prompt):
            seen.append(prompt)
            return "example-key-wxyz"

        buf = io.StringIO()
        with redirect_stdout(buf):
            key = pair_client.read_xai_key(getpass_fn)
            tail = pair_client.echo_last4(key)
        self.assertEqual(seen, ["xAI key: "])
        self.assertEqual(key, "example-key-wxyz")
        self.assertEqual(tail, "wxyz")
        self.assertNotIn("example-key-wxyz", buf.getvalue())
        self.assertNotIn("example-key", buf.getvalue())
        self.assertNotIn(key, repr(vars(pair_client)))
        with self.assertRaises(ValueError):
            pair_client.read_xai_key(lambda _p: "")
        with self.assertRaises(TypeError):
            pair_client.read_xai_key(None)
        self.assertEqual(pair_client.echo_last4("ab"), "")

    def test_installer_bad_code_does_not_send(self):
        transport = Capture(self.cert_pem)
        with mock.patch("pair_client.ec.generate_private_key", side_effect=AssertionError("key")):
            with redirect_stdout(io.StringIO()):
                out = pair_client.run_pairing(transport, lambda _m: "yes", ADDR, "0000-0000")
        self.assertEqual(out["reason"], "code")
        self.assertEqual(transport.sent, [])

    def test_proofs_agree_across_modules(self):
        fp = hashlib.sha256(b"station").digest()
        self.assertEqual(pairing.code_proof("abcd-2345", fp), pair_client.code_proof("ABCD2345", fp))
        self.assertNotEqual(pairing.code_proof("abcd-2345", fp), pairing.code_proof("abcd-2345", hashlib.sha256(b"x").digest()))


def _other(pairing_id):
    other = secrets.token_hex(8)
    if other == pairing_id:
        other = "0123456789abcdef"
    return other


def _csr_without_eku():
    pairing_id = secrets.token_hex(8)
    key = ec.generate_private_key(ec.SECP256R1())
    dns = f"relay-{pairing_id}"
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns)]))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return csr, pairing_id, key


def _leaf(ca, eku_oids):
    key = ec.generate_private_key(ec.SECP256R1())
    now = ca._utc()
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "relay-0123456789abcdef")]))
        .issuer_name(ca.cert.subject)
        .public_key(key.public_key())
        .serial_number(0x55AA)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("relay-0123456789abcdef")]), critical=False)
    )
    if eku_oids is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage(list(eku_oids)), critical=True)
    return builder.sign(ca.key, hashes.SHA256())


if __name__ == "__main__":
    unittest.main()
