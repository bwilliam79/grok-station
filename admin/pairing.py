"""Station side of relay pairing. Pure logic: pass a clock, an rng, and no sockets.

The pairing code is shown once and is not kept. State stores only
HMAC-SHA256(key=normalized code, msg=sha256(station cert DER)).
A `relay-*` name constraint is not expressible in the certificate, so
issuance checks the DNS name itself.
"""
import base64, datetime, hashlib, hmac, ipaddress, json, re, secrets, time

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

WINDOW_S = 600
STEPUP_S = 300
MAX_TRIES = 5
CLIENT_DAYS = 365
CA_DAYS = 3650
ACTOR = "admin"
SIDE = "station"

_PID = re.compile(r"[0-9a-f]{16}\Z")
_DNS = re.compile(r"relay-([0-9a-f]{16})\Z")
_DETAIL = {
    "pair-start": re.compile(r"-\Z"),
    "pair-port-open": re.compile(r"-\Z"),
    "pair-port-closed": re.compile(r"-\Z"),
    "pair-code-new": re.compile(r"-\Z"),
    "pair-try-bad": re.compile(r"[1-5]/5\Z"),
    "pair-locked": re.compile(r"-\Z"),
    "pair-cancel": re.compile(r"-\Z"),
    "pair-csr-signed": re.compile(r"[0-9a-f]{8}\Z"),
    "unpair": re.compile(r"[0-9a-f]{8}\Z"),
}
_V4_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
)
_V6_NETS = (
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


class PairingError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _stamp(epoch):
    t = int(epoch)
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit_line(epoch, event, detail="-"):
    """One line: `<ts> admin station <event> <detail>`. Drops anything else."""
    rule = _DETAIL.get(event)
    if rule is None or not isinstance(detail, str) or not rule.fullmatch(detail):
        return None
    return f"{_stamp(epoch)} {ACTOR} {SIDE} {event} {detail}"


def _parse(obj, pem_loader, der_loader, kind, already):
    try:
        if isinstance(obj, already):
            return obj
        if isinstance(obj, str):
            obj = obj.encode()
        if isinstance(obj, (bytes, bytearray, memoryview)):
            data = bytes(obj)
            if b"-----BEGIN" in data:
                return pem_loader(data)
            return der_loader(data)
    except ValueError:
        raise PairingError(kind) from None
    raise PairingError(kind)


def load_cert(obj):
    return _parse(obj, x509.load_pem_x509_certificate, x509.load_der_x509_certificate, "cert", x509.Certificate)


def load_csr(obj):
    return _parse(obj, x509.load_pem_x509_csr, x509.load_der_x509_csr, "csr", x509.CertificateSigningRequest)


def fingerprint_bytes(cert):
    """SHA-256 of the certificate's DER encoding. All 32 bytes, not a prefix."""
    der = load_cert(cert).public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).digest()


def format_fingerprint(cert_or_fp):
    """4 lines x 4 groups of 4 uppercase hex. Every one of the 64 hex digits.

    A 32-byte value is the fingerprint itself. Anything else is a certificate
    and is hashed first. Longer values are not sliced to fit.
    """
    if isinstance(cert_or_fp, (bytes, bytearray, memoryview)) and len(cert_or_fp) == 32:
        raw = bytes(cert_or_fp)
    elif isinstance(cert_or_fp, str) and re.fullmatch(r"[0-9a-fA-F]{64}", cert_or_fp.strip() or ""):
        raw = bytes.fromhex(cert_or_fp.strip())
    else:
        raw = fingerprint_bytes(cert_or_fp)
    if len(raw) != 32:
        raise PairingError("fingerprint")
    hexstr = raw.hex().upper()
    groups = [hexstr[i:i + 4] for i in range(0, 64, 4)]
    return "\n".join(" ".join(groups[i:i + 4]) for i in range(0, 16, 4))


def spki_fingerprint(public_key):
    """SHA-256 of the SubjectPublicKeyInfo. Both sides can show this before the cert exists."""
    der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).digest()


def lan_source_ok(ip):
    """True only for RFC 1918, IPv6 ULA, link-local, or loopback. No sockets."""
    if not isinstance(ip, str):
        return False
    # Whitespace is not part of an address. Do not strip it into a match.
    if not ip or any(c.isspace() for c in ip):
        return False
    text = ip
    if "%" in text:
        text = text.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if addr.is_loopback:
        return True
    nets = _V4_NETS if isinstance(addr, ipaddress.IPv4Address) else _V6_NETS
    return any(addr in net for net in nets)


def normalize_code(code):
    if not isinstance(code, str):
        raise PairingError("code")
    text = "".join(code.split()).replace("-", "").upper()
    if not re.fullmatch(r"[A-Z2-7]{8}", text):
        raise PairingError("code")
    return text


def code_proof(code, fingerprint):
    """HMAC-SHA256, key = normalized code, message = 32-byte certificate fingerprint."""
    if not isinstance(fingerprint, (bytes, bytearray, memoryview)) or len(fingerprint) != 32:
        raise PairingError("fingerprint")
    return hmac.new(normalize_code(code).encode("ascii"), bytes(fingerprint), hashlib.sha256).digest()


def _check_eku(csr):
    try:
        eku = csr.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        return
    oids = set(eku)
    if ExtendedKeyUsageOID.SERVER_AUTH in oids or oids != {ExtendedKeyUsageOID.CLIENT_AUTH}:
        raise PairingError("eku")


def _check_not_ca(csr):
    try:
        bc = csr.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        return
    if bc.ca:
        raise PairingError("ca-bit")


def validate_csr(csr, pairing_id=None):
    """Return (csr, pairing id). Refuse serverAuth, any SAN other than relay-<id>, or a bad id.

    `pairing_id` None extracts the id from the SAN. A passed id must match it.
    """
    csr = load_csr(csr)
    if pairing_id is not None and (not isinstance(pairing_id, str) or not _PID.fullmatch(pairing_id)):
        raise PairingError("pairing-id")
    if not csr.is_signature_valid:
        raise PairingError("csr")
    pub = csr.public_key()
    if not isinstance(pub, ec.EllipticCurvePublicKey) or not isinstance(pub.curve, ec.SECP256R1):
        raise PairingError("key")
    _check_not_ca(csr)
    try:
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        raise PairingError("san")
    names = list(san)
    if len(names) != 1 or not isinstance(names[0], x509.DNSName):
        raise PairingError("san")
    match = _DNS.fullmatch(names[0].value)
    if not match:
        raise PairingError("san")
    found = match.group(1)
    if pairing_id is not None and found != pairing_id:
        raise PairingError("san")
    _check_eku(csr)
    return csr, found


def _serial8(number):
    text = format(number, "x")
    if len(text) < 8:
        text = text.zfill(8)
    return text[:8]


def _as_serial(serial):
    if isinstance(serial, bool):
        return None
    if isinstance(serial, int):
        return serial if serial > 0 else None
    if hasattr(serial, "serial_number") and not isinstance(serial, (bytes, str, bytearray)):
        return _as_serial(serial.serial_number)
    if isinstance(serial, str) and re.fullmatch(r"[0-9a-fA-F]+", serial.strip() or ""):
        number = int(serial.strip(), 16)
        return number if number > 0 else None
    return None


def _coerce_proof(proof):
    if isinstance(proof, str) and re.fullmatch(r"[0-9a-fA-F]{64}", proof.strip() or ""):
        return bytes.fromhex(proof.strip())
    if isinstance(proof, (bytes, bytearray, memoryview)) and len(proof) == 32:
        return bytes(proof)
    return None


class PairingWindow:
    """One admin-opened pairing window. The listener asks `port_should_be_open`."""

    def __init__(self, station_cert, ca=None, *, clock=None, rng=None, audit=None):
        self.fingerprint = fingerprint_bytes(station_cert)
        self.ca = ca
        self._clock = clock or time.time
        self._rng = rng or secrets.token_bytes
        self._sink = audit
        self.audits = []
        self.phase = "idle"
        self.tries = 0
        self.expires_at = None
        self.opened_at = None
        self._proof = None
        self._used = False
        self._pending_pem = None
        self._pending_id = None
        self._port_closed = True

    def __repr__(self):
        return f"PairingWindow(phase={self.phase}, tries={self.tries})"

    def _now(self):
        return float(self._clock())

    def _audit(self, event, detail="-"):
        line = audit_line(self._now(), event, detail)
        if line is None:
            return
        self.audits.append(line)
        sink = self._sink
        if isinstance(sink, list):
            sink.append(line)
        elif sink is not None:
            sink(line)

    def _close_port(self):
        if self._port_closed:
            return
        self._port_closed = True
        self._audit("pair-port-closed", "-")

    def _stepup_fresh(self, step_up_ok):
        if isinstance(step_up_ok, bool) or not isinstance(step_up_ok, (int, float)):
            return False
        age = self._now() - float(step_up_ok)
        return 0 <= age <= STEPUP_S

    def _mint_code(self):
        raw = self._rng(5)
        if not isinstance(raw, (bytes, bytearray, memoryview)) or len(raw) != 5:
            raise PairingError("rng")
        code = base64.b32encode(bytes(raw)).decode("ascii")
        if not re.fullmatch(r"[A-Z2-7]{8}", code):
            raise PairingError("code")
        # The code itself is not stored. Only this digest is.
        self._proof = hmac.new(code.encode("ascii"), self.fingerprint, hashlib.sha256).digest()
        return f"{code[:4]}-{code[4:]}"

    def _note_bad_try(self):
        if self.phase == "locked" or self.tries >= MAX_TRIES:
            self.phase = "locked"
            return
        self.tries += 1
        self._audit("pair-try-bad", f"{self.tries}/{MAX_TRIES}")
        if self.tries >= MAX_TRIES:
            self.phase = "locked"
            self._pending_pem = None
            self._pending_id = None
            self._audit("pair-locked", "-")
            self._close_port()

    def _expire_if_due(self):
        if self.phase in ("open", "pending") and self.expires_at is not None and self._now() >= self.expires_at:
            self._pending_pem = None
            self._pending_id = None
            self.phase = "expired"
            self._close_port()

    def _matches(self, proof):
        mac = self._proof if isinstance(self._proof, bytes) and len(self._proof) == 32 else b"\x00" * 32
        given = _coerce_proof(proof)
        if given is None:
            hmac.compare_digest(mac, mac)
            return False
        return hmac.compare_digest(given, mac)

    def _result(self, ok, reason, relay_fp=None):
        return {"ok": ok, "reason": reason, "relay_fingerprint": relay_fp, "tries": self.tries}

    def open(self, step_up_ok):
        """Open a 10-minute window. `step_up_ok` is the unix time of the admin step-up.

        The step-up must be fresh (<= 5 minutes). Returns the display code `XXXX-XXXX`,
        or None when refused. The code is not retained on this object.
        """
        self._expire_if_due()
        if not self._stepup_fresh(step_up_ok) or self.phase in ("open", "pending"):
            return None
        self.phase = "open"
        self.tries = 0
        self._used = False
        self._pending_pem = None
        self._pending_id = None
        self._port_closed = False
        self._proof = None
        self.opened_at = self._now()
        self.expires_at = self.opened_at + WINDOW_S
        try:
            code = self._mint_code()
        except Exception:
            self.phase = "idle"
            self._proof = None
            self._port_closed = True
            self.expires_at = None
            raise
        self._audit("pair-start", "-")
        self._audit("pair-port-open", "-")
        return code

    def new_code(self):
        """Replace the current code. The previous proof stops working. Tries are kept."""
        self._expire_if_due()
        if self.phase != "open":
            return None
        code = self._mint_code()
        self._audit("pair-code-new", "-")
        return code

    def cancel(self):
        self._expire_if_due()
        if self.phase not in ("open", "pending"):
            return False
        self._pending_pem = None
        self._pending_id = None
        self.phase = "cancelled"
        self._audit("pair-cancel", "-")
        self._close_port()
        return True

    def port_should_be_open(self):
        """Listener manager: listen while the window is open or waiting for confirm."""
        self._expire_if_due()
        return self.phase in ("open", "pending")

    def submit(self, csr_pem, proof):
        """Check the proof, then hold the CSR. Signing waits for `confirm_match`."""
        match = self._matches(proof)
        timed_out = (
            self.phase in ("open", "pending")
            and self.expires_at is not None
            and self._now() >= self.expires_at
        )
        if timed_out:
            self._pending_pem = None
            self._pending_id = None
            if self._used:
                self.phase = "expired"
                self._close_port()
                return self._result(False, "used")
            self._note_bad_try()
            if self.phase != "locked":
                self.phase = "expired"
                self._close_port()
            return self._result(False, "locked" if self.phase == "locked" else "expired")
        if self.phase == "locked":
            return self._result(False, "locked")
        if self.phase == "expired":
            if self._used:
                return self._result(False, "used")
            self._note_bad_try()
            return self._result(False, "locked" if self.phase == "locked" else "expired")
        if self._used or self.phase in ("pending", "done"):
            return self._result(False, "used")
        if self.phase != "open":
            return self._result(False, "closed")
        if not match:
            self._note_bad_try()
            return self._result(False, "locked" if self.phase == "locked" else "bad-proof")
        try:
            csr, pairing_id = validate_csr(csr_pem, None)
        except PairingError:
            return self._result(False, "bad-csr")
        self._used = True
        self._pending_pem = csr.public_bytes(serialization.Encoding.PEM).decode("ascii")
        self._pending_id = pairing_id
        self.phase = "pending"
        shown = format_fingerprint(spki_fingerprint(csr.public_key()))
        # The port stays up until the admin confirms, cancels, or the window ends.
        return self._result(True, "pending", shown)

    def confirm_match(self):
        """Sign the pending CSR. None unless `submit` already verified a proof."""
        self._expire_if_due()
        if self.phase != "pending" or not self._pending_pem or self.ca is None:
            return None
        try:
            issued = self.ca.sign_csr(self._pending_pem, self._pending_id)
        except PairingError:
            return None
        self._pending_pem = None
        self._pending_id = None
        self.phase = "done"
        self._close_port()
        return issued


class StationCA:
    """EC P-256 pairing CA. Path length 0. The relay name is checked in `sign_csr`, not in the cert."""

    def __init__(self, *, clock=None, rng=None, audit=None):
        self._clock = clock or time.time
        self._rng = rng or secrets.token_bytes
        self._sink = audit
        self.audits = []
        self._revoked = set()
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cert = self._self_sign()

    def __repr__(self):
        return "StationCA()"

    def _now(self):
        return float(self._clock())

    def _utc(self):
        return datetime.datetime.fromtimestamp(int(self._now()), datetime.timezone.utc)

    def _audit(self, event, detail="-"):
        line = audit_line(self._now(), event, detail)
        if line is None:
            return
        self.audits.append(line)
        sink = self._sink
        if isinstance(sink, list):
            sink.append(line)
        elif sink is not None:
            sink(line)

    def _new_serial(self):
        raw = self._rng(16)
        if not isinstance(raw, (bytes, bytearray, memoryview)) or len(raw) != 16:
            raise PairingError("rng")
        number = int.from_bytes(bytes(raw), "big")
        return number or 1

    def _self_sign(self):
        now = self._utc()
        name = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "station"),
            x509.NameAttribute(NameOID.COMMON_NAME, "station-pairing-ca"),
        ])
        usage = x509.KeyUsage(
            digital_signature=False,
            content_commitment=False,
            key_encipherment=False,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=True,
            crl_sign=True,
            encipher_only=False,
            decipher_only=False,
        )
        builder = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.key.public_key())
            .serial_number(self._new_serial())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(usage, critical=True)
        )
        return builder.sign(self.key, hashes.SHA256())

    def sign_csr(self, csr, pairing_id):
        """Sign a client cert. SAN is exactly DNS relay-<pairing-id>, EKU clientAuth, 365 days, CA false."""
        csr, pairing_id = validate_csr(csr, pairing_id)
        now = self._utc()
        dns = f"relay-{pairing_id}"
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns)])
        usage = x509.KeyUsage(
            digital_signature=True,
            content_commitment=False,
            key_encipherment=False,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=False,
            crl_sign=False,
            encipher_only=False,
            decipher_only=False,
        )
        number = self._new_serial()
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self.cert.subject)
            .public_key(csr.public_key())
            .serial_number(number)
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=CLIENT_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns)]), critical=False)
            .add_extension(usage, critical=True)
            .sign(self.key, hashes.SHA256())
        )
        short = _serial8(cert.serial_number)
        self._audit("pair-csr-signed", short)
        return cert, short

    def revoke(self, serial):
        number = _as_serial(serial)
        if number is None:
            raise PairingError("serial")
        if number in self._revoked:
            return
        self._revoked.add(number)
        self._audit("unpair", _serial8(number))

    def is_revoked(self, serial):
        number = _as_serial(serial)
        return number is not None and number in self._revoked

    def revocation_json(self):
        serials = [format(n, "x") for n in sorted(self._revoked)]
        return json.dumps({"serials": serials}, separators=(",", ":"))

    def load_revocation_json(self, text):
        doc = json.loads(text)
        serials = doc["serials"] if isinstance(doc, dict) else doc
        if not isinstance(serials, list):
            raise PairingError("revocation")
        cleaned = set()
        for item in serials:
            number = _as_serial(item)
            if number is None:
                raise PairingError("revocation")
            cleaned.add(number)
        self._revoked = cleaned

    def _signed_by_us(self, cert):
        if cert.issuer != self.cert.subject:
            return False
        pub = self.cert.public_key()
        algo = cert.signature_hash_algorithm
        if algo is None:
            return False
        try:
            pub.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(algo))
        except (InvalidSignature, TypeError, ValueError):
            return False
        return True

    def _times_ok(self, cert):
        now = self._now()
        return cert.not_valid_before_utc.timestamp() <= now <= cert.not_valid_after_utc.timestamp()

    def verify_client_cert(self, cert):
        """False when revoked, the issuer is wrong, the cert is expired, or EKU is not clientAuth only."""
        try:
            cert = load_cert(cert)
        except (PairingError, ValueError):
            return False
        if not self._signed_by_us(cert) or not self._times_ok(cert) or not self._times_ok(self.cert):
            return False
        try:
            bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        except x509.ExtensionNotFound:
            bc = None
        if bc is not None and bc.ca:
            return False
        try:
            eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        except x509.ExtensionNotFound:
            return False
        if set(eku) != {ExtendedKeyUsageOID.CLIENT_AUTH}:
            return False
        if self.is_revoked(cert.serial_number):
            return False
        return True
