"""Station control channel checks. No sockets.

The client on this channel is the station, not a bot and not the relay.
Station-side issuance is not in this repo. Assumption until that lands: the
station's pairing CA (the CA that signs relay-<16 hex> pairing certificates)
also issues the station's own client certificate. That leaf has exactly one
SAN, the DNS name station-<16 lowercase hex>, EKU clientAuth only, and the CA
bit clear. A relay pairing name is the other direction and is rejected here.

Revocation is the station's JSON {"serials": ["hex", ...]}. A missing file
means nothing is revoked yet. A present file that is not that shape fails
closed. The relay does not dial the station to fetch it.
"""

import base64
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

import release

STATION_SAN = re.compile(r"station-[0-9a-f]{16}\Z")
_AUDIT_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z [A-Za-z0-9._:-]+ [A-Za-z0-9._:-]+ [A-Za-z0-9._:-]+ [!-~]{1,64}$"
)
_XAI_CODE = re.compile(r"[A-Za-z0-9._:-]{1,32}\Z")
_VERSION = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+\Z")


def _aware(now):
    if not isinstance(now, datetime):
        return None
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def parse_serial(item):
    if isinstance(item, bool):
        return None
    if isinstance(item, int):
        return item if item > 0 else None
    if isinstance(item, str) and re.fullmatch(r"[0-9a-fA-F]+", item.strip() or ""):
        number = int(item.strip(), 16)
        return number if number > 0 else None
    return None


def load_revocation(text):
    """Return a set of serials, or None when the document is not usable.

    None fails closed. An empty serial list is valid (nothing revoked).
    """
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(text, str):
        return None
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    serials = doc.get("serials") if isinstance(doc, dict) else None
    if not isinstance(serials, list):
        return None
    found = set()
    for item in serials:
        number = parse_serial(item)
        if number is None:
            return None
        found.add(number)
    return found


def load_cert_bytes(data):
    if isinstance(data, x509.Certificate):
        return data
    if isinstance(data, str):
        data = data.encode()
    if not isinstance(data, (bytes, bytearray)):
        return None
    raw = bytes(data)
    if not raw:
        return None
    try:
        if b"-----BEGIN" in raw:
            return x509.load_pem_x509_certificate(raw)
        return x509.load_der_x509_certificate(raw)
    except ValueError:
        return None


def _pem_from_text(text):
    if "-----BEGIN CERTIFICATE-----" not in text or "-----END CERTIFICATE-----" not in text:
        return None
    body = text.replace("-----BEGIN CERTIFICATE-----", " ").replace("-----END CERTIFICATE-----", " ")
    body = "".join(body.split())
    if not body or re.fullmatch(r"[A-Za-z0-9+/=]+", body) is None:
        return None
    lines = ["-----BEGIN CERTIFICATE-----"]
    for index in range(0, len(body), 64):
        lines.append(body[index:index + 64])
    lines.append("-----END CERTIFICATE-----")
    return "\n".join(lines) + "\n"


def load_header_cert(value):
    """Leaf from the Caddy header: base64 DER, or a PEM (newlines may be spaces)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 20000:
        return None
    if "-----BEGIN" in text:
        pem = _pem_from_text(text.replace("\\n", "\n"))
        return load_cert_bytes(pem) if pem else None
    compact = "".join(text.split())
    try:
        der = base64.b64decode(compact, validate=True)
    except (ValueError, TypeError):
        return None
    return load_cert_bytes(der)


def _signed_by(leaf, ca):
    if leaf.issuer != ca.subject:
        return False
    pub = ca.public_key()
    if not isinstance(pub, ec.EllipticCurvePublicKey):
        return False
    algo = leaf.signature_hash_algorithm
    if algo is None:
        return False
    try:
        pub.verify(leaf.signature, leaf.tbs_certificate_bytes, ec.ECDSA(algo))
    except (InvalidSignature, TypeError, ValueError):
        return False
    return True


def _is_ca(cert):
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def _client_auth_only(cert):
    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        return False
    return set(eku) == {ExtendedKeyUsageOID.CLIENT_AUTH}


def _station_san(cert):
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    names = list(san)
    if len(names) != 1 or not isinstance(names[0], x509.DNSName):
        return False
    return STATION_SAN.fullmatch(names[0].value) is not None


def _in_window(cert, now):
    try:
        return cert.not_valid_before_utc <= now <= cert.not_valid_after_utc
    except (AttributeError, TypeError, ValueError):
        return False


def verify_station_client(leaf, ca, revoked, now):
    """True only for a current station client cert from the pinned CA.

    `revoked` is a set of serial integers, or None (the revocation document
    was unusable: fail closed).
    """
    now = _aware(now)
    leaf = load_cert_bytes(leaf)
    ca = load_cert_bytes(ca)
    if leaf is None or ca is None or now is None or revoked is None:
        return False
    if not _is_ca(ca) or _is_ca(leaf):
        return False
    if not _signed_by(leaf, ca):
        return False
    if not _in_window(ca, now) or not _in_window(leaf, now):
        return False
    if not _client_auth_only(leaf) or not _station_san(leaf):
        return False
    if leaf.serial_number in revoked:
        return False
    return True


def fingerprint_matches(leaf, presented):
    """True when `presented` is the sha256 of the leaf DER (Caddy's client fingerprint).

    Colons and whitespace are ignored. A missing or different value fails closed,
    so a caller cannot swap the certificate Caddy already checked.
    """
    leaf = load_cert_bytes(leaf)
    if leaf is None or not isinstance(presented, str):
        return False
    raw = "".join(presented.split()).replace(":", "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", raw) is None:
        return False
    digest = hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest()
    return hmac.compare_digest(raw, digest)


def cert_expiry(cert):
    cert = load_cert_bytes(cert)
    if cert is None:
        return "none"
    try:
        return cert.not_valid_after_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (AttributeError, ValueError):
        return "unknown"


def key_last4(key):
    """Last 4 of a key long enough that those 4 are not the whole value. Never the key."""
    if not isinstance(key, str) or len(key) < 8:
        return None
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
        return None
    return key[-4:]


def sanitize_xai(value):
    if value == "ok":
        return "ok"
    if isinstance(value, str) and _XAI_CODE.fullmatch(value):
        return value
    return "error"


def recent_audit_lines(directory, banned=(), limit=20):
    """Last `limit` relay audit lines. Drops questions, PEM, and anything banned.

    A line is five fields and the detail has no spaces, so a question or a reply
    cannot be returned. `banned` is full secrets (the API key), not a last-4.
    """
    if not directory or not os.path.isdir(directory):
        return []
    kept = []
    try:
        names = sorted(name for name in os.listdir(directory) if name.startswith("audit-") and name.endswith(".log"))
    except OSError:
        return []
    secrets = [item for item in banned if isinstance(item, str) and len(item) >= 8]
    for name in names:
        try:
            with open(os.path.join(directory, name), encoding="utf-8", errors="replace") as handle:
                rows = handle.readlines()
        except OSError:
            continue
        for raw in rows:
            line = raw.strip()
            if not _AUDIT_LINE.fullmatch(line):
                continue
            if "?" in line or "BEGIN" in line or "://" in line or "@" in line:
                continue
            if any(secret in line for secret in secrets):
                continue
            kept.append(line)
    if limit < 1:
        return []
    return kept[-limit:]


def body_bad_args(body, installed):
    """True when the body is not {"version":"vX.Y.Z"}. Does not fetch.

    Uses validate_update_request against a stub so the shape rules stay in one place.
    A well-formed version is not bad-args here, even when it will not match the manifest.
    """
    if not isinstance(installed, str) or _VERSION.fullmatch(installed) is None:
        return True
    stub = {"version": installed, "min_version_floor": "v0.0.0"}
    try:
        decision = release.validate_update_request(body, installed, stub)
    except release.ManifestError:
        return True
    return (not decision.ok) and decision.reason == "bad-args"


def note_seen(store, now):
    now = _aware(now)
    if now is None:
        return
    store["at"] = now


def last_seen_text(store):
    when = store.get("at") if isinstance(store, dict) else None
    when = _aware(when) if isinstance(when, datetime) else None
    if when is None:
        return None
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def read_release_file(text):
    """version, compat, and signed_tag from installed state. signed_tag stays null.

    Signing was dropped. A tag in the file is ignored so status cannot claim one.
    """
    version = "v0.0.0"
    compat = {"station_min_relay": "v0.0.0", "relay_min_station": "v0.0.0"}
    if not text:
        return version, None, compat
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError:
            return version, None, compat
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return version, None, compat
    if not isinstance(doc, dict):
        return version, None, compat
    got = doc.get("version") or doc.get("relay")
    if isinstance(got, str) and _VERSION.fullmatch(got):
        version = got
    raw = doc.get("compat")
    if isinstance(raw, dict):
        station = raw.get("station_min_relay")
        relay_min = raw.get("relay_min_station")
        if isinstance(station, str) and _VERSION.fullmatch(station) and isinstance(relay_min, str) and _VERSION.fullmatch(relay_min):
            compat = {"station_min_relay": station, "relay_min_station": relay_min}
    return version, None, compat


def status_document(version, compat, last_seen, xai, last4, expiry, pii_on, rate_cap, audit_lines):
    """The status object. Callers must not add question text, replies, or secrets."""
    return {
        "version": version,
        "signed_tag": None,
        "compat": compat,
        "last_seen": last_seen,
        "xai": sanitize_xai(xai),
        "key_last4": last4,
        "cert_expiry": expiry,
        "pii_mode": bool(pii_on),
        "rate_cap": rate_cap,
        "audit": list(audit_lines),
    }


def enqueue_body(version):
    """Request file body. Version only. Digests are not accepted from the caller."""
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ValueError("version")
    return (json.dumps({"version": version}, separators=(",", ":")) + "\n").encode("ascii")
