"""TLS transport for installer pairing.

The first handshake does not verify the certificate: it only reads the leaf so
the operator can compare the fingerprint. Nothing is sent on that connection.
After the operator continues, the leaf is pinned and the CSR is sent on a
second connection that requires that certificate. Tests pass `dial`.
"""

import re
import socket
import ssl

from cryptography.hazmat.primitives import serialization

from pair_client import PairingError, load_cert


def split_address(address):
    if not isinstance(address, str) or not address or "://" in address or "@" in address:
        raise PairingError("address")
    if address.startswith("[") and "]" in address:
        host, _, rest = address[1:].partition("]")
        if not rest.startswith(":") or not host:
            raise PairingError("address")
        port_text = rest[1:]
    elif address.count(":") == 1:
        host, port_text = address.split(":", 1)
    else:
        raise PairingError("address")
    if not host or any(ord(ch) < 33 or ord(ch) == 127 for ch in host):
        raise PairingError("address")
    if not port_text.isdigit():
        raise PairingError("address")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise PairingError("address")
    return host, port


def _pem(der):
    cert = load_cert(der)
    return cert.public_bytes(serialization.Encoding.PEM)


def real_dial(address, verify, pin=None, payload=None):
    """One connection. `verify` False reads the leaf and writes nothing."""
    host, port = split_address(address)
    if not verify:
        if payload is not None:
            raise PairingError("payload")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((host, port), timeout=10)
        try:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
        except Exception:
            raise PairingError("cert") from None
        if not der:
            raise PairingError("cert")
        return _pem(der)
    if not isinstance(pin, (bytes, bytearray)) or b"-----BEGIN CERTIFICATE-----" not in bytes(pin):
        raise PairingError("pin")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # The pin is the certificate itself, so the name is not a second check.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=bytes(pin))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if not isinstance(payload, tuple) or len(payload) != 3:
        raise PairingError("payload")
    method, path, body = payload
    wire = _http_request(method, path, host, body)
    raw = socket.create_connection((host, port), timeout=10)
    try:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            tls.sendall(wire)
            return _http_body(_read_response(tls))
    except PairingError:
        raise
    except Exception:
        raise PairingError("send") from None


def _payload(csr_pem, proof):
    """JSON body for POST /pair/submit. The station reads csr_pem and a hex proof."""
    import json
    if isinstance(proof, (bytes, bytearray)):
        proof = bytes(proof).hex()
    if not isinstance(csr_pem, str) or not isinstance(proof, str):
        raise PairingError("payload")
    if "-----BEGIN" not in csr_pem or "PRIVATE" in csr_pem:
        raise PairingError("payload")
    return (json.dumps({"csr_pem": csr_pem, "proof": proof}, separators=(",", ":")) + "\n").encode("ascii")


def _http_request(method, path, host, body):
    """One HTTP/1.1 request. `host` is the header value only; it is not logged."""
    if method not in ("GET", "POST") or not isinstance(path, str) or not path.startswith("/"):
        raise PairingError("payload")
    if any(ch in path for ch in (" ", "\r", "\n")):
        raise PairingError("payload")
    lines = [method + " " + path + " HTTP/1.1", "Host: " + host, "Accept: application/json", "Connection: close"]
    payload = b""
    if body is not None:
        if isinstance(body, str):
            body = body.encode("ascii")
        if not isinstance(body, (bytes, bytearray)):
            raise PairingError("payload")
        payload = bytes(body)
        lines.append("Content-Type: application/json")
        lines.append("Content-Length: " + str(len(payload)))
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload


def _http_body(raw):
    if not isinstance(raw, (bytes, bytearray)):
        raise PairingError("send")
    raw = bytes(raw)
    sep = raw.find(b"\r\n\r\n")
    if sep < 0:
        return raw
    status = raw.split(b"\r\n", 1)[0]
    parts = status.split()
    if len(parts) < 2 or not parts[1].isdigit():
        raise PairingError("send")
    if int(parts[1]) not in (200, 202):
        raise PairingError("send")
    return raw[sep + 4:]


def _read_response(tls):
    chunks = []
    tls.settimeout(10)
    while sum(len(part) for part in chunks) < 20000:
        try:
            part = tls.recv(4096)
        except socket.timeout:
            break
        if not part:
            break
        chunks.append(part)
    return b"".join(chunks)


class StationTransport:
    def __init__(self, address, dial=None):
        self.address = address
        self.dial = dial or real_dial
        self.pinned = None

    def fetch_station_cert(self):
        pem = self.dial(self.address, False, None, None)
        if isinstance(pem, str):
            pem = pem.encode()
        cert = load_cert(pem)
        self.pinned = cert.public_bytes(serialization.Encoding.PEM)
        return cert

    def send(self, csr_pem, proof):
        """POST /pair/submit on the pinned connection. The private key is not in the body."""
        if self.pinned is None:
            raise PairingError("pin")
        body = _payload(csr_pem, proof)
        return self.dial(self.address, True, self.pinned, ("POST", "/pair/submit", body))

    def fetch_result(self, pairing_id):
        """GET /pair/result after the operator confirms. Still pinned. No code on the wire."""
        if self.pinned is None:
            raise PairingError("pin")
        if not isinstance(pairing_id, str) or not re.fullmatch(r"[0-9a-f]{16}", pairing_id):
            raise PairingError("pairing-id")
        path = "/pair/result?id=" + pairing_id
        return self.dial(self.address, True, self.pinned, ("GET", path, None))
