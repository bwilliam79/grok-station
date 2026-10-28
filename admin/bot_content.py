"""What a bot actually runs, with the release number taken out (0.12.10). Stdlib only. Station side only.

A bot's instructions and its update script name the release in a header line ("Grok Station instructions,
release X.Y.Z."). That line changes every release even when nothing else does, so comparing release strings
flagged every bot as behind after every station update. content_fp() hashes the parts a bot runs (its
template, the pinned gs-update.sh, and its helper file) with only that header line normalized.

tools/gen-update-manifest.py writes admin/bot-releases.txt from git: one line per published manifest,
    <release> <update commit> <gs-update.sh sha256> <home fingerprint> <cloud fingerprint>
The station reads it to decide whether a bot on an older release is already running the same content.
This file never reaches a bot and changes nothing a bot checks: it only decides whether a row says Updated.
"""
import hashlib
import os
import re

# 0.12.11 (Nix): only whole header lines are normalized: a template's own first line, or the line in
# gs-update.sh's TEMPLATES dict that opens a template ('    "home": r''' + header). Header-looking text
# anywhere else is content, so changing it changes the fingerprint.
HEADER_RX = re.compile(rb"""(?m)^((?:    "(?:home|cloud)": r''')?)Grok Station instructions, release [0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.$""")
HEADER_NORM = rb"\1Grok Station instructions, release X.Y.Z."
HELPER = {"home": "scripts/install-home.sh", "cloud": "tools/grok-station-reply"}
_LINE = re.compile(r"^([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}) ([0-9a-f]{40}) ([0-9a-f]{64}) ([0-9a-f]{64}) ([0-9a-f]{64})$")
TABLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot-releases.txt")


def normalize(data):
    """bytes with every instructions header line's release number replaced by X.Y.Z. Nothing else changes."""
    return HEADER_RX.sub(HEADER_NORM, data)


def content_fp(template, update_script, helper):
    """sha256 over the normalized template, the normalized update script and the helper, each length-prefixed."""
    h = hashlib.sha256(b"gs-bot-content-v1\n")
    for part in (normalize(template), normalize(update_script), helper):
        h.update(b"%d\n" % len(part))
        h.update(part)
    return h.hexdigest()


def read_table(path=None):
    """[{release, update_commit, update_sha, fp: {home, cloud}}] from bot-releases.txt. Bad lines are skipped."""
    try:
        text = open(path or TABLE, encoding="ascii").read()
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for ln in text.split("\n"):
        m = _LINE.fullmatch(ln)
        if m:
            out.append({"release": m.group(1), "update_commit": m.group(2), "update_sha": m.group(3),
                        "fp": {"home": m.group(4), "cloud": m.group(5)}})
    return out
