#!/usr/bin/env python3
"""leak-scan: block private terms, personal email domains and real LAN IPs.

Called by tools/pii-guard (pre-commit, commit-msg, pre-push and CI). The repo is
public, so private terms (personal hostnames, family names, the home city) live
only in a private list outside the repo (see TERMS_FILE); every token in the
scanned text is compared against it. Generic rules (gmail/aol addresses, private
IPv4 outside the made-up test ranges) are plain.

Usage: leak-scan.py LABEL [FILE...]   (no FILE: read stdin)
Output names the label/file, line and rule only, never the matching text.
Exit 1 on any hit. A line containing "leak-guard: allow" or an
assertNotIn("...") leak-guard assertion is skipped for the private-term rule.
"""
import ipaddress
import os
import re
import sys

# Private terms are NOT in this repo. One term per line (lowercased on load):
#   $LEAK_TERMS (newline-separated, CI secret), else $LEAK_TERMS_FILE,
#   else ~/.config/grok-station/leak-terms (mode 0600, never committed).
# Missing list: warning locally, failure in CI (fail closed).
TERMS_FILE = os.environ.get("LEAK_TERMS_FILE") or os.path.expanduser("~/.config/grok-station/leak-terms")
# Whole-word / dotted-hostname-piece rule set (currently empty; mechanism kept).
WORDS = set()


def load_terms():
    raw = os.environ.get("LEAK_TERMS")
    if not raw:
        try:
            with open(TERMS_FILE, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            return None
    terms = {t.strip().lower() for t in raw.splitlines()}
    terms = {t for t in terms if t and not t.startswith("#")}
    return terms or None


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


def _spans(parts):
    for i in range(len(parts)):
        for j in range(i + 1, len(parts) + 1):
            yield ".".join(parts[i:j])


def term_hit(line, terms, words=WORDS):
    """True if any token (or a dotted/hyphenated piece of one) is a private term."""
    toks = [t.strip("._-").lower() for t in TOKEN.findall(line)]
    toks = [t for t in toks if t]
    for k, t in enumerate(toks):
        whole = set(_spans([p for p in t.split(".") if p])) | {t}
        if whole & terms or whole & words:
            return True
        if any(c in terms for c in re.split(r"[._-]", t) if c):
            return True
        if k + 1 < len(toks) and (t + " " + toks[k + 1]) in terms:
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


def scan_text(text, label, terms=frozenset()):
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if not ALLOW_LINE.search(line) and term_hit(line, terms):
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
    terms = load_terms()
    if terms is None:
        if os.environ.get("CI"):
            print("leak-scan: no private terms (set the LEAK_TERMS secret); failing closed", file=sys.stderr)
            return 1
        if not os.environ.get("LEAK_SCAN_QUIET"):
            print("leak-scan: warning: no private terms at %s; checking emails, IPs and hostname rule only"
                  % TERMS_FILE, file=sys.stderr)
        terms = set()
    if len(argv) == 2:
        ok = scan_text(sys.stdin.read(), argv[1], terms)
    for f in argv[2:]:
        if f in ALLOW_FILES:
            continue
        try:
            with open(f, encoding="utf-8", errors="strict") as fh:
                text = fh.read()
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        ok = scan_text(text, f, terms) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
