#!/usr/bin/env python3
"""Helpers for the relay installer. No network until a subcommand asks for it.

`fetch` downloads the manifest with release.py (verified HTTPS, allow-listed
redirects) and refuses an expired manifest. `pair` reads the station address
and the pairing code from stdin, not from the process arguments. The xAI key
is not handled here.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import release  # noqa: E402
import release_fetch  # noqa: E402
from pair_client import run_pairing  # noqa: E402
from pair_transport import StationTransport  # noqa: E402


def write_secret(path, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, bytes):
        raise TypeError("secret")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def cmd_fetch(dest):
    """Write a validated manifest. Print image refs (name@digest) on stdout. No URL."""
    body = release.fetch_manifest(release_fetch.https_get)
    doc = release.parse_manifest(body)
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    text = json.dumps(doc, sort_keys=True, indent=2) + "\n"
    with open(dest, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(dest, 0o644)
    images = doc["components"]["relay"]["images"]
    for name in sorted(images):
        print(name + "@" + images[name])
    return 0


def _prompt(tty):
    def prompt(message):
        tty.write(message + "\n")
        tty.flush()
        return tty.readline().strip()
    return prompt


def cmd_pair(cfg):
    """Address and code are the first two stdin lines. Fingerprints go to the terminal."""
    address = sys.stdin.readline().rstrip("\n")
    code = sys.stdin.readline().rstrip("\n")
    try:
        tty = open("/dev/tty", "r+", encoding="utf-8", errors="replace")
    except OSError:
        print("pair-failed")
        return 1
    transport = StationTransport(address)
    try:
        result = run_pairing(transport, _prompt(tty), address, code)
    except Exception:
        print("pair-failed")
        return 1
    if not result.get("ok"):
        print(result.get("reason") or "pair-failed")
        return 1
    from cryptography.hazmat.primitives import serialization
    pem = result["key"].private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    write_secret(os.path.join(cfg, "client.key"), pem)
    write_secret(os.path.join(cfg, "station.url"), address.encode("utf-8") + b"\n")
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            raw = transport.fetch_result(result["pairing_id"])
            doc = json.loads(raw.decode("utf-8"))
        except Exception:
            doc = None
        if isinstance(doc, dict) and doc.get("ok") is True:
            cert = doc.get("cert_pem")
            ca = doc.get("ca_pem")
            if isinstance(cert, str) and isinstance(ca, str) and "BEGIN CERTIFICATE" in cert:
                write_secret(os.path.join(cfg, "client.crt"), cert if cert.endswith("\n") else cert + "\n")
                write_secret(os.path.join(cfg, "ca.crt"), ca if ca.endswith("\n") else ca + "\n")
                print("paired")
                return 0
        time.sleep(2)
    print("pending")
    return 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "fetch":
        try:
            return cmd_fetch(argv[1])
        except release.ManifestError as err:
            print(err.reason, file=sys.stderr)
            return 1
    if len(argv) == 2 and argv[0] == "pair":
        return cmd_pair(argv[1])
    print("usage", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
