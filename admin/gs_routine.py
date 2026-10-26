"""Render and check a bot's instructions (0.11.8). Stdlib only.

The station uses this module to show and hash the exact text a bot will adopt. scripts/gs-update.sh carries a
byte-identical copy (a test keeps them equal) and runs it on the bot's computer, so both sides render the same
bytes from the same pinned template. check() refuses any text that is not the fixed allowed-sections template:
unknown paragraphs, links outside the allow list, commands outside the allow list, or risky words.
"""
import hashlib
import re
import sys

REPO = "https://raw.githubusercontent.com/bwilliam79/grok-station/"
RELEASE = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SHA = re.compile(r"^[0-9a-f]{64}$")
SLUG = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
STATION = re.compile(r"^https://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?$")
FINGERPRINT = re.compile(r"^(?:[0-9A-F]{2}:){31}[0-9A-F]{2}$")
PUB = re.compile(r"^[A-Za-z0-9+/]{43}=$")
HEADER = re.compile(r"^Grok Station instructions, release ([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})\.$")

# Each paragraph (text between blank lines) must start with one of these.
SECTIONS = (
    "Grok Station instructions, release ",
    "Answer as the bot named in the request's persona field",
    "If kind is bot_list, do not answer a question",
    "Then run exactly one command (two when you also send a card, below), and no others",
    "Then run exactly two commands (four when you also send a card, below), and no others",
    "printf '%s' \"$ANSWER\" | gs-seal-reply reply --persona ",
    "Set REQUEST_ID from the webhook's request_id",
    "printf '%s' \"$NAMES\" | gs-seal-reply reply --persona ",
    "The key is already on disk for this persona.",
    "1. On the cloud computer, seal.",
    "2. On the home machine, post that line unchanged",
    "Only the sealed JSON goes to the home machine.",
    "Display cards (optional).",
    "Profile picture.",
    "Station update.",
)
# Lines that are commands must match one of these exactly (after the placeholders are filled).
_SL, _FP, _PUB, _ST = r"[a-z][a-z0-9-]{1,23}", r"(?:[0-9A-F]{2}:){31}[0-9A-F]{2}", r"[A-Za-z0-9+/]{43}=", \
    r"https://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?"
COMMANDS = tuple(re.compile(p) for p in (
    r"^printf '%s' \"\$(ANSWER|NAMES|STATUS)\" \| gs-seal-reply reply --persona " + _SL
    + r" --request-id \"\$REQUEST_ID\"( --kind bot_list)?$",
    r"^gs-seal-reply avatar --persona " + _SL + r" < PICTURE_FILE$",
    r"^printf '%s' \"\$CARD_JSON\" \| gs-seal-reply show --persona " + _SL + r"$",
    r"^printf '%s' \"\$(ANSWER|NAMES|STATUS|CARD_JSON)\" \| grok-station-reply seal(-list|-show)? --bot " + _SL
    + r" --station-pub '" + _PUB + r"'( --request-id \"\$REQUEST_ID\")?$",
    r"^grok-station-reply seal-avatar --bot " + _SL + r" --station-pub '" + _PUB + r"' < PICTURE_FILE$",
    r"^printf '%s' '(SEALED_JSON|SEALED_CARD|SEALED_PICTURE|SEALED_STATUS)' \| python3 ~/bin/grok-station-reply post"
    r"( --show| --avatar)? --station " + _ST + r" --insecure --pin-sha256 '" + _FP + r"'$",
    r"^d=\$\(mktemp -d\) && \( cd \"\$d\" && curl --proto '=https' --tlsv1\.2 -fsS -o gs-update\.sh '"
    + re.escape(REPO) + r"[0-9a-f]{40}/scripts/gs-update\.sh' && \{ command -v sha256sum >/dev/null && "
    r"s=\$\(sha256sum gs-update\.sh\) \|\| s=\$\(shasum -a 256 gs-update\.sh\); \} && \[ \"\$\{s%% \*\}\" = "
    r"'[0-9a-f]{64}' \] && sh gs-update\.sh --release \"\$RELEASE\" --commit \"\$COMMIT\" --mode (home|cloud) "
    r"--persona " + _SL + r"( --station '" + _ST + r"' --pin-sha256 '" + _FP + r"' --station-pub '" + _PUB
    + r"')? \); r=\$\?; rm -rf \"\$d\"; \[ \"\$r\" = 0 \]$",
))
_CMD_START = re.compile(r"^(printf |gs-seal-reply|grok-station-reply|python3|d=\$\(|sh |curl |bash |\./|sudo |/)")
URLS = (
    re.compile(r"^https://a\.espncdn\.com/i/teamlogos/<league>/500/<abbr>\.png$"),
    re.compile(r"^" + re.escape(REPO) + r"[0-9a-f]{40}/scripts/gs-update\.sh$"),
)
_URL = re.compile(r"https?://[^\s'\")]+")
FORBIDDEN = ("sudo", "ssh ", "scp ", "eval ", "chmod", "chown", "crontab", "launchctl", "systemctl", "osascript",
             "base64 -d", "| sh", "| bash", "bash -c", "sh -c", "python3 -c", "~/.ssh", ".bashrc", ".zshrc",
             "keychain", "export ", "permission", "always allowed", "ignore previous", "ignore the above",
             "from now on", "you may now", "you can now", "allowed to", "any file", "any command", "any link",
             "override", "disregard", "new rule", "instead of the above", "without asking", "api key",
             "token=", "credential", "private key", "~/.config", "/etc/", ".env")
MAX_PARAS, MAX_PARA = 20, 2200


class Bad(Exception):
    pass


def update_command(mode, slug, pin_commit, pin_sha, station="", fingerprint="", pub=""):
    """The one command a bot runs for kind update_bot. Built from pins and checked values only."""
    if mode not in ("home", "cloud") or not SLUG.match(slug or "") or slug == "grok":
        raise Bad("slug")
    if not COMMIT.match(pin_commit or "") or not SHA.match(pin_sha or ""):
        raise Bad("pin")
    tail = ""
    if mode == "cloud":
        if not (STATION.match(station or "") and FINGERPRINT.match(fingerprint or "") and PUB.match(pub or "")):
            raise Bad("station")
        tail = " --station '%s' --pin-sha256 '%s' --station-pub '%s'" % (station, fingerprint, pub)
    return ("d=$(mktemp -d) && ( cd \"$d\" && curl --proto '=https' --tlsv1.2 -fsS -o gs-update.sh '"
            + REPO + pin_commit + "/scripts/gs-update.sh' && { command -v sha256sum >/dev/null && "
            "s=$(sha256sum gs-update.sh) || s=$(shasum -a 256 gs-update.sh); } && [ \"${s%% *}\" = '"
            + pin_sha + "' ] && sh gs-update.sh --release \"$RELEASE\" --commit \"$COMMIT\" --mode " + mode
            + " --persona " + slug + tail + " ); r=$?; rm -rf \"$d\"; [ \"$r\" = 0 ]")


def render(template, mode, slug, pin_commit, pin_sha, station="", fingerprint="", pub=""):
    """Fill one template for one bot. The same inputs give the same bytes on the station and on the bot."""
    if not isinstance(template, str):
        raise Bad("template")
    cmd = update_command(mode, slug, pin_commit, pin_sha, station, fingerprint, pub)
    text = template.replace("<update command>", cmd).replace("<slug>", slug)
    if mode == "cloud":
        text = text.replace("<station public key>", pub).replace("<station address>", station) \
            .replace("<fingerprint>", fingerprint)
    check(text, mode)
    return text


def check(text, mode):
    """Refuse anything but the fixed allowed-sections template. Returns the release in the header."""
    if not isinstance(text, str) or not text or len(text) > 12000 or "\r" in text or "\x00" in text:
        raise Bad("shape")
    if any(ord(c) < 0x20 and c != "\n" for c in text):
        raise Bad("control")
    low = text.lower()
    for word in FORBIDDEN:
        if word in low:
            raise Bad("forbidden: " + word.strip())
    paras = [p for p in text.split("\n\n") if p.strip()]
    if len(paras) > MAX_PARAS or any(len(p) > MAX_PARA for p in paras):
        raise Bad("size")
    starts = [next(s for s in SECTIONS if p.startswith(s)) for p in paras if p.startswith(SECTIONS)]
    if len(starts) != len(set(starts)):
        raise Bad("section repeated")
    m = HEADER.match(paras[0].split("\n", 1)[0]) if paras else None
    if not m:
        raise Bad("header")
    for p in paras:
        if not p.startswith(SECTIONS):
            raise Bad("section")
    for url in _URL.findall(text):
        if not any(u.match(url) for u in URLS):
            if not (mode == "cloud" and STATION.match(url)):
                raise Bad("link")
    for line in text.split("\n"):
        if _CMD_START.match(line) and not any(c.match(line) for c in COMMANDS):
            raise Bad("command")
    if text.count("--mode ") != 1 or ("--mode %s " % mode) not in text:
        raise Bad("mode")
    if re.search(r"<(slug|update command|station public key|station address|fingerprint)>", text):
        raise Bad("placeholder")
    return m.group(1)


def instr_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _main(argv):
    """python3 -I gs_routine.py render TEMPLATE MODE SLUG PIN_COMMIT PIN_SHA [STATION FINGERPRINT PUB]"""
    if len(argv) not in (7, 10) or argv[1] != "render":
        sys.stderr.write("usage\n")
        return 2
    try:
        with open(argv[2], encoding="utf-8") as fh:
            template = fh.read()
        text = render(template, *argv[3:])
    except (Bad, OSError, UnicodeDecodeError, TypeError) as e:
        sys.stderr.write("refused: %s\n" % e)
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
