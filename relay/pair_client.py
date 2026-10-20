"""Installer side of relay pairing. The transport is injected; this module opens no sockets.

The station certificate is fetched and shown before anything is sent. The key
stays on this machine. The proof is HMAC-SHA256(key=normalized code,
msg=sha256 of the certificate that was just shown).
"""
import hashlib, hmac, re, secrets

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CONFIRM_PROMPT = "Type yes to continue"
_CODE = re.compile(r"[A-Z2-7]{8}\Z")


class PairingError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def load_cert(obj):
    try:
        if isinstance(obj, x509.Certificate):
            return obj
        if isinstance(obj, str):
            obj = obj.encode()
        if isinstance(obj, (bytes, bytearray, memoryview)):
            data = bytes(obj)
            if b"-----BEGIN" in data:
                return x509.load_pem_x509_certificate(data)
            return x509.load_der_x509_certificate(data)
    except ValueError:
        raise PairingError("cert") from None
    raise PairingError("cert")


def fingerprint_bytes(cert):
    der = load_cert(cert).public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).digest()


def format_fingerprint(cert_or_fp):
    """4 lines x 4 groups of 4 uppercase hex, all 64 digits. Same layout as the station."""
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
    der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).digest()


def normalize_code(code):
    if not isinstance(code, str):
        raise PairingError("code")
    text = "".join(code.split()).replace("-", "").upper()
    if not _CODE.fullmatch(text):
        raise PairingError("code")
    return text


def code_proof(code, fingerprint):
    if not isinstance(fingerprint, (bytes, bytearray, memoryview)) or len(fingerprint) != 32:
        raise PairingError("fingerprint")
    return hmac.new(normalize_code(code).encode("ascii"), bytes(fingerprint), hashlib.sha256).digest()


def build_relay_csr(key, pairing_id):
    if not isinstance(pairing_id, str) or not re.fullmatch(r"[0-9a-f]{16}", pairing_id):
        raise PairingError("pairing-id")
    dns = f"relay-{pairing_id}"
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns)])
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(name)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )


def run_pairing(transport, prompt, station_addr, code, *, rng=None):
    """Show the station fingerprint, and send a CSR only after the operator types yes.

    `station_addr` is not logged and is not dialed here. `transport.fetch_station_cert`
    and `transport.send` are the only I/O. The private key is not sent.
    """
    if not isinstance(station_addr, str):
        return {"ok": False, "reason": "address"}
    cert = transport.fetch_station_cert()
    fp = fingerprint_bytes(cert)
    shown = format_fingerprint(fp)
    print(shown)
    answer = prompt(CONFIRM_PROMPT)
    if answer != "yes":
        return {"ok": False, "reason": "aborted", "station_fingerprint": shown}
    try:
        normalize_code(code)
    except PairingError:
        return {"ok": False, "reason": "code", "station_fingerprint": shown}
    key = ec.generate_private_key(ec.SECP256R1())
    raw_id = (rng or secrets.token_bytes)(8)
    if not isinstance(raw_id, (bytes, bytearray, memoryview)) or len(raw_id) != 8:
        raise PairingError("rng")
    pairing_id = bytes(raw_id).hex()
    csr = build_relay_csr(key, pairing_id)
    csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode("ascii")
    proof = code_proof(code, fp)
    relay_fp = format_fingerprint(spki_fingerprint(key.public_key()))
    # Printed before send: a blocking send waits until the admin has compared this.
    print(relay_fp)
    transport.send(csr_pem, proof)
    return {
        "ok": True,
        "reason": "sent",
        "pairing_id": pairing_id,
        "station_fingerprint": shown,
        "relay_fingerprint": relay_fp,
        "csr_pem": csr_pem,
        "proof": proof,
        "key": key,
    }


def read_xai_key(getpass_fn):
    """Read a key with the hidden prompt only. The key is returned and not logged."""
    if not callable(getpass_fn):
        raise TypeError("hidden prompt required")
    key = getpass_fn("xAI key: ")
    if not isinstance(key, str) or key == "":
        raise ValueError("empty key")
    return key


def echo_last4(key):
    """Last 4 characters for display. Does not print or log the key."""
    if not isinstance(key, str):
        raise TypeError("key")
    if len(key) < 4:
        return ""
    return key[-4:]
