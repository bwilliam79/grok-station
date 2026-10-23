#!/usr/bin/env python3
"""leak-scan: block private terms, personal email domains and real LAN IPs.

Called by tools/pii-guard (pre-commit, commit-msg, pre-push and CI). The repo is
public, so private terms (personal hostnames, family names, the home city) are
stored only as salted sha256 hashes of lowercased tokens; every token in the
scanned text is hashed and compared. Generic rules (gmail/aol addresses, private
IPv4 outside the made-up test ranges) are plain.

Usage: leak-scan.py LABEL [FILE...]   (no FILE: read stdin)
Output names the label/file, line and rule only, never the matching text.
Exit 1 on any hit. A line containing "leak-guard: allow" or an
assertNotIn("...") leak-guard assertion is skipped for the private-term rule.
"""
import hashlib
import ipaddress
import re
import sys

SALT = 'dde6fccb534a407c6747a1724b28b140'
PART_HASHES = {'185b5f4f407037254f1b1a32e12522adaa895d5063be2a0567f18bb36327616f',
    '201e60a85eb98c3ac09cf8228c9df0a3e53c451c51c0795eabcf6585186fb21c',
    '2eceaf3e9f1e929e0655bc435abf69a6a4c1e563c963411d9d03615bccfa3b27',
    '44ce1aad7fcdb1da726fac0c0d6d577a9c7261bef45e1df3bcbb0e78293eca95',
    '51fbd204e2b405bdb2b4d8242b4d6c558ade16f6021fa762b65593b7dd8d6e43',
    '5993f36d440e2d32ed4b0c17b96ad28a5a35e3c1dedbeca159fbd0d8a43e5e6f',
    '7c41815da1c09d444cff32eef2fef7e2631e485de306cae6fa5bf8c1e239d3d0',
    'abecb1d7b589a775157eca2273c571c47a33beef9b006835f146090357344cf4',
    'ba535b42915e8a5549ded61e49011f892e6c1153e3635204c54494f9a61b7139',
    'bf1f645a87c61e1f7a25c7eeb8fd29df2b5055bccb84fe7d0c6b08660928d147',
    'c929d766b8514b60fba8a93d3a624c38d6ace75d4bbbb8c3a0514a39418b32f1',
    'dcaf1dac9ec94128f7858d4f38910013412361690e186d4880920c0dae775a91'}
WORD_HASHES = {'9081dfd5334f329fc3d9d8da731e6204169704964374a98c36e213c49d42db69'}

# Files that exist to test this guard.
ALLOW_FILES = {"tools/leak-scan.py", "tests/test_leak_scan.py"}

# Made-up ranges the code and tests already use (hotspot 10.42.0.0/24, admin
# LAN tests, Docker/VPN examples) plus RFC 5737 documentation ranges.
ALLOW_NETS = [ipaddress.ip_network(n) for n in (
    "10.42.0.0/24", "10.0.0.0/24", "10.10.10.0/24", "10.64.0.0/24",
    "172.16.3.0/24", "172.17.0.0/24", "172.18.0.0/24",
    "192.168.4.0/24", "192.168.77.0/24",
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?:gmail|aol)\.com\b", re.I)
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d])")
ALLOW_LINE = re.compile(r"leak-guard: allow|assertNotIn\(\s*[\'\"][^\'\"]*[\'\"]")


def _h(t):
    return hashlib.sha256((SALT + t).encode()).hexdigest()


def _spans(parts):
    for i in range(len(parts)):
        for j in range(i + 1, len(parts) + 1):
            yield ".".join(parts[i:j])


def term_hit(line, salt_hash=None, parts_set=None, words_set=None):
    """True if any token (or dotted/hyphenated piece of one) hashes to a private term."""
    hf = salt_hash or _h
    ps = PART_HASHES if parts_set is None else parts_set
    ws = WORD_HASHES if words_set is None else words_set
    toks = [t.strip("._-").lower() for t in TOKEN.findall(line)]
    toks = [t for t in toks if t]
    for k, t in enumerate(toks):
        dots = [p for p in t.split(".") if p]
        whole = set(_spans(dots)) | {t}
        if any(hf(c) in ps or hf(c) in ws for c in whole):
            return True
        if any(hf(c) in ps for c in re.split(r"[._-]", t) if c):
            return True
        if k + 1 < len(toks) and hf(t + " " + toks[k + 1]) in ps:
            return True
    return False


def ip_hit(line):
    for m in IPV4.finditer(line):
        try:
            ip = ipaddress.ip_address(m.group(1))
        except ValueError:
            continue
        if any(ip in n for n in PRIVATE) and not any(ip in n for n in ALLOW_NETS):
            return True
    return False


def scan_text(text, label):
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if not ALLOW_LINE.search(line) and term_hit(line):
            hits.append((n, "private term"))
        if EMAIL.search(line):
            hits.append((n, "personal email address"))
        if ip_hit(line):
            hits.append((n, "LAN IP"))
    for n, rule in hits:
        print("leak-scan: %s in %s line %d" % (rule, label, n), file=sys.stderr)
    return not hits


def main(argv):
    if len(argv) < 2:
        print("usage: leak-scan.py LABEL [FILE...]", file=sys.stderr)
        return 2
    ok = True
    if len(argv) == 2:
        ok = scan_text(sys.stdin.read(), argv[1])
    for f in argv[2:]:
        if f in ALLOW_FILES:
            continue
        try:
            with open(f, encoding="utf-8", errors="strict") as fh:
                text = fh.read()
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        ok = scan_text(text, f) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
