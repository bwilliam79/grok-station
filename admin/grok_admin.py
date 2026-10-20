#!/usr/bin/env python3
"""grok-station admin web page (LAN only), per Noor's ADMIN-UI.md.

No default password: until an admin password is set, the station shows a one-time 6-character setup code (3+3, no
0/O/1/I), new on each boot until used, and voice stays off. Password >= 10 characters, stored as a scrypt hash.
Secrets are write-only (last 4 shown). 5 wrong tries (code or password) lock sign-in for 15 min. Privileged changes go
through grok-station-helper (root, fixed subcommands, validated input) via sudo. A correct admin password also opens a
10-minute step-up window on that session for saving a webhook, adding or renaming a bot, changing a bot's voice,
uploading a portrait, and changing the privacy toggle; removing a bot or webhook always asks again. Speaker names stay on this Pi. Stdlib only.
"""
import base64, hashlib, hmac, http.client, http.cookies, ipaddress, json, os, re, secrets, shutil, ssl, sys
import subprocess, threading, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

E = os.environ.get
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(E("ADMIN_PORT", "443"))  # HTTPS only; the app is never served over plain HTTP
HTTP_PORT = int(E("ADMIN_HTTP_PORT", "80"))  # 301 to https and nothing else (0 = off)
TLS_DIR = E("ADMIN_TLS_DIR", "/var/lib/grok-station/tls")  # made on the station by `grok-station tls-cert`
STATE_DIR = E("ADMIN_STATE_DIR", "/var/lib/grok-station/admin")
RUN_DIR = E("ADMIN_RUN_DIR", "/run/grok-station-admin")  # setup.json lives here (dir 0750, group grok)
WEB = E("ADMIN_WEB_DIR", os.path.join(HERE, "web"))
AVATAR_DIR = E("ADMIN_AVATAR_DIR", os.path.join(os.path.dirname(HERE), "assets", "avatars"))
AVATAR_NAME = re.compile(r"default-(0[1-9]|1[0-8])\.svg")
HELPER = E("ADMIN_HELPER", "sudo -n /usr/local/lib/grok-station/grok-station-helper").split()
_RELAY_SRC = os.path.join(os.path.dirname(HERE), "relay")
if os.path.isdir(_RELAY_SRC) and _RELAY_SRC not in sys.path:
    sys.path.insert(0, _RELAY_SRC)
import pairing  # noqa: E402
import pair_listener  # noqa: E402
import release  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
WAKE_CTL = E("ADMIN_WAKE_CTL", "http://127.0.0.1:8789")
WAKE_CONFIG = E("WAKE_CONFIG", "/opt/grok-station/wake/wakewords.json")
ENROLLED = E("WAKE_ENROLLED_DIR", "/opt/grok-station/wake/models/enrolled") + "/wakewords.enrolled.json"
PERSONAS = E("ADMIN_PERSONAS", "/opt/grok-station/portraits/personas.json")
SNAPSHOT = E("GROK_STATION_SNAPSHOT", "/run/grok-station/last.png")
VERSION_FILE = E("ADMIN_VERSION_FILE", "/opt/grok-station/VERSION")
REPO = "bwilliam79/grok-station"
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O, 1/I
MIN_PASSWORD = 10
MAX_FAILS, FAIL_WINDOW, LOCK_FOR = 5, 900, 900
SESSION_TTL, SETUP_TTL, IDLE_TTL = 12 * 3600, 900, 30 * 60  # idle: 30 min with no request; absolute cap stays 12 h
STEPUP_TTL = 600  # from the password entry; later requests do not move it
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
JARVIS_LOCKED = "Jarvis is the station's default wake word and can't be turned off"
UNITS = ("grok-station", "grok-station-wake", "grok-station-admin")
LOG_SINCE = {"15m": "-15min", "1h": "-1h", "6h": "-6h", "24h": "-24h", "7d": "-7d"}
LOCK = threading.RLock()


def now():
    return time.time()


def path(*p):
    return os.path.join(STATE_DIR, *p)


def read_json(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(p, obj, mode=0o600):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, p)


# ------------------------------------------------------------------ auth state
def hash_password(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, maxmem=64 * 1024 * 1024, **SCRYPT)
    return {"alg": "scrypt", **SCRYPT, "salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(h).decode()}


def check_password(pw, rec):
    try:
        salt = base64.b64decode(rec["salt"])
        h = hashlib.scrypt(pw.encode(), salt=salt, n=rec["n"], r=rec["r"], p=rec["p"], dklen=rec["dklen"],
                           maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(h, base64.b64decode(rec["hash"]))
    except (KeyError, ValueError, TypeError):
        return False


def password_problem(pw):
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(pw) > 256:
        return "That's too long."
    if len(set(pw)) < 4:
        return "Use a mix of characters."
    return None


def new_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def norm_code(s):
    s = re.sub(r"[\s-]", "", str(s or "")).upper()
    return s


class Auth:
    """Password record, setup code, sessions, global lockout."""

    def __init__(self):
        self.sessions, self.setup_tickets, self.fails, self.locked_until = {}, {}, [], 0.0
        self.code = None

    @property
    def rec(self):
        return read_json(path("auth.json"), {}).get("password")

    def setup_required(self):
        return not self.rec

    def mode(self):
        if not self.setup_required():
            return None
        if os.path.exists(os.path.join(RUN_DIR, "hotspot.json")):
            return "hotspot"
        return "reset" if read_json(path("auth.json"), {}).get("reset_at") else "setup"

    def publish(self):
        """Keep /run/grok-station/setup.json in step: the display shows it, the wake listener stays closed."""
        f = os.path.join(RUN_DIR, "setup.json")
        if not self.setup_required():
            self.code = None
            try:
                os.unlink(f)
            except OSError:
                pass
            return
        old = read_json(f, {})
        if not self.code:  # same boot (tmpfs): keep the code already on screen
            c = str(old.get("code", ""))
            self.code = c if len(c) == 6 and all(ch in CODE_ALPHABET for ch in c) else new_code()
        doc = {"mode": self.mode(), "code": self.code, "host": hostname() + ".local", "url": admin_url(),
               "fingerprint": tls_info()["fingerprint"]}
        doc.update({k: v for k, v in read_json(os.path.join(RUN_DIR, "hotspot.json"), {}).items()
                    if k in ("ssid", "password", "url", "qr_join", "qr_open")})
        if doc != old:
            write_json(f, doc, 0o640)

    def note_fail(self):
        t = now()
        self.fails = [x for x in self.fails if x > t - FAIL_WINDOW] + [t]
        if len(self.fails) >= MAX_FAILS:
            self.locked_until, self.fails = t + LOCK_FOR, []

    def locked(self):
        return max(0, int(self.locked_until - now()))

    def try_code(self, code):
        with LOCK:
            if self.locked() or not self.setup_required() or not self.code:
                return None
            if hmac.compare_digest(norm_code(code).encode(), self.code.encode()):
                t = secrets.token_urlsafe(32)
                self.setup_tickets[t] = now() + SETUP_TTL
                return t
            self.note_fail()
            return None

    def set_first_password(self, ticket, pw):
        with LOCK:
            exp = self.setup_tickets.get(ticket or "")
            if not exp or exp < now() or not self.setup_required():
                return "Your setup code expired. Enter it again."
            why = password_problem(pw)
            if why:
                return why
            doc = read_json(path("auth.json"), {})
            doc.pop("reset_at", None)
            doc.update(password=hash_password(pw), set_at=int(now()))
            write_json(path("auth.json"), doc)
            self.setup_tickets.clear()
            self.code = None
            self.publish()
            return None

    def login(self, user, pw):
        with LOCK:
            if self.locked() or self.setup_required():
                return None
            rec = self.rec
        ok = user == "admin" and check_password(str(pw or ""), rec)
        with LOCK:
            if not ok:
                self.note_fail()
                return None
            self.fails = []
            return self.new_session()

    def new_session(self):
        t = secrets.token_urlsafe(32)
        t0 = now()
        self.sessions[hashlib.sha256(t.encode()).hexdigest()] = {"exp": t0 + SESSION_TTL, "seen": t0}
        return t

    def _key(self, token):
        return hashlib.sha256(token.encode()).hexdigest() if token else None

    def _fresh(self, rec, t):
        return isinstance(rec, dict) and rec.get("exp", 0) >= t and t - rec.get("seen", 0) <= IDLE_TTL

    def valid(self, token):
        if not token or self.setup_required():
            return False
        k = self._key(token)
        rec = self.sessions.get(k)
        t = now()
        if not self._fresh(rec, t):
            self.sessions.pop(k, None)
            return False
        rec["seen"] = t  # idle window slides on each authenticated request; step-up does not
        return True

    def stepup_until(self, token):
        """Epoch when this session's step-up window closes, or None. Does not slide or refresh it."""
        if not token or self.setup_required():
            return None
        rec = self.sessions.get(self._key(token))
        t = now()
        if not self._fresh(rec, t):
            return None
        until = rec.get("stepup_until")
        if isinstance(until, bool) or not isinstance(until, (int, float)) or until <= t:
            return None
        return until

    def stepup_open(self, token):
        return self.stepup_until(token) is not None

    def open_stepup(self, token):
        """Start a fresh STEPUP_TTL window on this session. Caller already checked the password."""
        if not token or self.setup_required():
            return None
        rec = self.sessions.get(self._key(token))
        t = now()
        if not self._fresh(rec, t):
            return None
        rec["stepup_until"] = t + STEPUP_TTL
        audit_stepup()
        return rec["stepup_until"]

    def reauth(self, pw):
        """Admin password again for a bot change. Wrong guesses share the login lockout.

        Returns None, "wrong", or "locked". A wrong password is "wrong" even on the try that
        trips the lock; the next try is "locked".
        """
        with LOCK:
            if self.locked():
                return "locked"
            rec = self.rec
            if not rec or not check_password(str(pw or ""), rec):
                self.note_fail()
                return "wrong"
            self.fails.clear()
            return None

    def logout(self, token):
        if token:
            self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    def change_password(self, old, new):
        with LOCK:
            if self.locked():
                return "Too many tries. Wait 15 minutes."
            if not check_password(str(old or ""), self.rec):
                self.note_fail()
                return "Current password is wrong."
            why = password_problem(new)
            if why:
                return why
            doc = read_json(path("auth.json"), {})
            doc.update(password=hash_password(new), set_at=int(now()))
            write_json(path("auth.json"), doc)
            self.sessions.clear()
            return None


AUTH = Auth()


# ------------------------------------------------------------------ helpers
def helper(*args, stdin=None, timeout=30):
    """Run grok-station-helper. Returns (ok, parsed JSON or {})."""
    try:
        p = subprocess.run(HELPER + list(args), input=(json.dumps(stdin) if stdin is not None else "").encode(),
                           capture_output=True, timeout=timeout)
        try:
            out = json.loads(p.stdout.decode() or "{}")
        except ValueError:
            out = {}
        return p.returncode == 0, out
    except (OSError, subprocess.TimeoutExpired):
        return False, {"error": "helper unavailable"}


def hostname():
    try:
        return open("/etc/hostname").read().strip() or "grok-station"
    except OSError:
        return "grok-station"


def tls_info():
    """The station cert's SHA-256 fingerprint (AB:CD:...) and the hostname it was made for."""
    def rd(n):
        try:
            return open(os.path.join(TLS_DIR, n)).read().strip()
        except OSError:
            return ""
    fp = rd("fingerprint").upper()
    return {"fingerprint": fp if re.fullmatch(r"(?:[0-9A-F]{2}:){31}[0-9A-F]{2}", fp) else "", "host": rd("host")}


def admin_url(host=None):
    return f"https://{host or hostname()}.local"


def wake(method, p, body=None, timeout=3):
    try:
        req = urllib.request.Request(WAKE_CTL + p, json.dumps(body).encode() if body is not None else None,
                                     {"Content-Type": "application/json"}, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read(65536) or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(4096))
        except ValueError:
            return e.code, {}
    except Exception:
        return 503, {"error": "wake listener unreachable"}


def busy():
    """A conversation or enrollment is active (don't restart voice / reboot under it)."""
    code, st = wake("GET", "/status")
    return code == 200 and (st.get("session") or st.get("enrolling"))


def version():
    try:
        return open(VERSION_FILE).read().strip()[:64]
    except OSError:
        return "dev"


def env_file(name):
    """Non-secret settings, read through the helper (the env files are root-only)."""
    ok, out = helper("env-get", name)
    return out.get("values", {}) if ok else {}


def secrets_meta():
    return read_json(path("secrets.json"), {})


def note_secret(name, value=None, test=None):
    m = secrets_meta()
    e = m.get(name, {})
    if value is not None:
        e.update(set=True, last4=value[-4:], saved_at=int(now()))
    if test is not None:
        e["last_test"] = {"ok": bool(test), "at": int(now())}
    m[name] = e
    write_json(path("secrets.json"), m)


# ------------------------------------------------------------------ wake words / speakers
def configured_phrase_names(cfg):
    """Phrase ids already in wakewords.json (words, enroll, fallback). Not overrides."""
    names = set()
    if not isinstance(cfg, dict):
        return names
    for w in (cfg.get("words") or []) + (cfg.get("enroll") or []):
        if isinstance(w, dict) and w.get("name"):
            names.add(w["name"])
    fb = cfg.get("fallback")
    if isinstance(fb, dict) and fb.get("name"):
        names.add(fb["name"])
    return names


def ensure_wake_entries(cfg, ov, bots):
    """Add missing wake entries. Never change enabled on one that already exists.

    hey_grok is created on (fresh install) when it is not in the config or the overrides.
    Every other new entry, including a bot added later, starts off. hey_jarvis is the fallback
    and is not written here. Returns (overrides, changed).
    """
    ov = dict(ov) if isinstance(ov, dict) else {}
    changed = False
    have = configured_phrase_names(cfg)

    def present(name):
        return name in have or isinstance(ov.get(name), dict)

    if not present("hey_grok"):
        ov["hey_grok"] = {"enabled": True, "persona": "grok"}
        changed = True
    for b in bots or []:
        if not isinstance(b, dict) or not slug_ok(b.get("slug")):
            continue
        name = "hey_" + b["slug"]
        if present(name):
            continue
        ov[name] = {"enabled": False, "persona": b["slug"]}
        changed = True
    return ov, changed


def remember_bot_wakeword(slug):
    """A bot just added on this station gets hey_<slug>, off, unless that entry already exists."""
    if not slug_ok(slug):
        return
    name = "hey_" + slug
    if name in configured_phrase_names(read_json(WAKE_CONFIG, {})):
        return
    ov = read_json(path("wake-overrides.json"), {})
    if not isinstance(ov, dict):
        ov = {}
    if isinstance(ov.get(name), dict):
        return
    ov[name] = {"enabled": False, "persona": slug}
    write_json(path("wake-overrides.json"), ov, 0o640)


def known_bots():
    """Bots the wake-word list should mirror: the relay list, or the station's cached personas."""
    got = relay_call("GET", "/admin/bots")
    if got and got[0] == 200 and isinstance(got[1], dict):
        out = []
        for b in got[1].get("bots") or []:
            safe = safe_bot(b)
            if safe:
                out.append({"slug": safe["slug"], "name": safe["name"]})
        return out
    return [{"slug": b["slug"], "name": b["name"]} for b in local_bots()]


def wakewords():
    drop_stale_guest()
    cfg = read_json(WAKE_CONFIG, {})
    ov = read_json(path("wake-overrides.json"), {})
    if not isinstance(ov, dict):
        ov = {}
    bots = known_bots()
    new_ov, changed = ensure_wake_entries(cfg, ov, bots)
    if changed:
        write_json(path("wake-overrides.json"), new_ov, 0o640)
        ov = new_ov
    names = {b["slug"]: b["name"] for b in bots}
    enrolled = {w.get("name"): w for w in read_json(ENROLLED, {}).get("words", []) if isinstance(w, dict)}
    models = read_json(path("models.json"), {})  # Pi-only: phrase -> {speakers: [ids], trained_at, clips}
    rows = [w for w in cfg.get("words", []) + cfg.get("enroll", []) if isinstance(w, dict)]
    have = {w.get("name") for w in rows}
    fb = cfg.get("fallback") if isinstance(cfg.get("fallback"), dict) else None
    for name, o in ov.items():
        # a phrase recorded on this station (phrase id -> persona), not one of the built-in words
        if name in have or not isinstance(o, dict) or (fb and name == fb.get("name")):
            continue
        if wake_name_ok(name) and persona_ok(str(o.get("persona") or "")):
            rows.append({"name": name, "persona": o["persona"]})
    out = []
    for w in rows:
        n = w.get("name")
        if not n or any(x["name"] == n for x in out):
            continue
        o = ov.get(n) if isinstance(ov.get(n), dict) else {}
        e = enrolled.get(n, {})
        has_model = n in enrolled or os.path.isfile(os.path.join(cfg.get("models_dir", ""), n + ".onnx"))
        persona = str(o.get("persona")) if persona_ok(str(o.get("persona") or "")) else w.get("persona", "grok")
        row = {"name": n, "persona": persona, "enabled": o.get("enabled", True) and has_model,
               "has_model": has_model, "sensitivity": round(1 - float(o.get("threshold", e.get("threshold", w.get("threshold", 0.5)))), 2),
               "provisional": bool(w.get("provisional")), "enrolled": n in enrolled, "metrics": e.get("metrics"),
               "trained_on": models.get(n)}
        slug = n[4:] if isinstance(n, str) and n.startswith("hey_") else ""
        if slug in names:
            row["label"] = "Hey " + names[slug]
        out.append(row)
    if fb and fb.get("name"):
        out.append({"name": fb["name"], "persona": fb.get("persona", "grok"), "enabled": True, "backup": True, "has_model": True,
                    "sensitivity": round(1 - float(fb.get("threshold", 0.5)), 2)})
    return out


def speakers():
    return read_json(path("speakers.json"), {})  # id -> {"name", "added_at", "guest"}


# ------------------------------------------------------------------ request handling
PRIVATE_NETS = None


def lan_ok(ip):
    try:
        a = ipaddress.ip_address(ip.removeprefix("::ffff:"))
        return a.is_private or a.is_loopback or a.is_link_local
    except ValueError:
        return False


HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
# 2–4 words, letters only, 32 characters. Still matches every hey_<name> the station already ships.
PHRASE_RE = re.compile(r"^[a-z]+(?:_[a-z]+){1,3}$")
PHRASE_MAX = 32
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")  # persona slug; "grok" is reserved and rejected separately
COMMON_WORDS = frozenset({"hey", "hi", "ok", "okay", "yo", "ah", "oh", "um", "uh"})
TOO_SHORT = "Too short. Use two or three words."
TOO_LONG = "Too long. Use two, three, or four words."
TOO_LONG_ID = "Too long. Keep it to 32 characters."
ACTIVE_ENROLL = ("recording", "uploading", "training")
WEBHOOK_HOST = "api2.cursor.sh"
RELAY_ENV = E("ADMIN_RELAY_ENV", "/etc/grok-station/admin-relay.env")
RELAY_UNCONFIGURED = "The station can't reach the relay's admin API yet"
PAIR_HOST = E("GROK_STATION_PAIR_HOST", "0.0.0.0")
PAIR_PORT = int(E("GROK_STATION_PAIR_PORT", "8443"))
RELAY_FETCHER = None  # tests: (method, path, payload) -> None or (status, dict). No network.
MANIFEST_FETCHER = None  # tests: () -> manifest dict or bytes. No network.
STATION_POST = None  # tests: (payload) -> ignored. Shows or clears the pairing fingerprint.
PAIR_SOCKET_FACTORY = None
PAIR_CA = PAIR_WINDOW = PAIR_LISTENER = PAIR_ISSUANCE = None
_VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
RELAY_DOWN = "Can't reach the voice service right now."
RELAY_ACTOR = "admin"
BOT_PATH = re.compile(r"^/api/bots(?:/([a-z][a-z0-9-]{1,23}))?(?:/(webhook|test)(?:/([A-Za-z0-9_-]{1,80}))?)?$")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/admin.css": ("admin.css", "text/css; charset=utf-8"), "/favicon.ico": ("assets/favicon.ico", "image/x-icon")}
ASSET_TYPE = {"png": "image/png", "svg": "image/svg+xml", "ttf": "font/ttf", "ico": "image/x-icon"}


def slug_ok(slug):
    return isinstance(slug, str) and SLUG_RE.fullmatch(slug) is not None and slug != "grok"


# The voice service's bot id is stricter than a 2–32 free slug: a letter first, then up to 23 more, "grok" reserved.
BOT_ID_ERROR = ("Use 2–24 characters: a letter first, then lowercase letters, digits, or hyphens. "
                "The voice service doesn't allow a longer id.")
BOT_ID_RESERVED = '"grok" is reserved.'


def bot_id_from_name(name):
    """Lowercase, hyphens for spaces, strip anything else. Does not invent a prefix or rewrite "grok"."""
    s = str(name or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if len(s) > 24:
        s = s[:24].rstrip("-")
    return s


def bot_id_problem(slug):
    """None when this can be a new bot id. Same rule the add-bot request enforces."""
    if slug == "grok":
        return BOT_ID_RESERVED
    if not isinstance(slug, str) or SLUG_RE.fullmatch(slug) is None:
        return BOT_ID_ERROR
    return None


def phrase_ok(name):
    return isinstance(name, str) and len(name) <= PHRASE_MAX and PHRASE_RE.fullmatch(name) is not None


def wake_name_ok(name):
    """A built-in phrase id, or hey_<bot id> (digits and hyphens allowed). phrase_ok itself stays strict."""
    if phrase_ok(name):
        return True
    return isinstance(name, str) and name.startswith("hey_") and slug_ok(name[4:])


def persona_ok(slug):
    """Grok, or a bot slug from the Bots list (letters, digits, dashes)."""
    return slug == "grok" or slug_ok(slug)


def phrase_label(name):
    return " ".join(p.capitalize() for p in str(name).split("_") if p)


def too_close_msg(label):
    return f"Too close to “{label}”. Pick something that sounds different."


def duplicate_msg(label):
    return f"“{label}” is already a wake word."


def common_msg(label):
    return f"“{label}” is only common words like “hey” or “okay”. Add a word that's yours."


def normalize_phrase(text):
    """Lowercase, keep letters, spaces and apostrophes, collapse spaces.

    Underscores are word breaks, so an id the page already has (hey_hank) stays that id.
    Apostrophes are removed. The model id is the words joined with "_" (hey_kitchen).
    Returns (model_id, words). An empty phrase is ("", []).
    """
    s = str(text or "").lower().replace("\u2019", "'").replace("\u2018", "'").replace("_", " ")
    s = re.sub(r"[^a-z' ]+", "", s)
    words = []
    for w in " ".join(s.split()).split(" "):
        w = w.strip("'").replace("'", "")
        if w:
            words.append(w)
    return "_".join(words), words


def levenshtein(a, b):
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def lev_ratio(a, b):
    """1 at a match, 0 when nothing is shared. Compared at >= 0.75."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return 1.0 - levenshtein(a, b) / max(len(a), len(b))


def syllables(word):
    w = re.sub(r"[^a-z]", "", str(word).lower())
    if not w:
        return 0
    count, prev = 0, False
    for ch in w:
        vowel = ch in "aeiouy"
        if vowel and not prev:
            count += 1
        prev = vowel
    if w.endswith("e") and count > 1 and not w.endswith(("le", "ye")):
        count -= 1
    return max(1, count)


def phrase_syllables(words):
    return sum(syllables(w) for w in words)


def known_phrase_ids():
    return [w["name"] for w in wakewords() if w.get("name")]


def phrase_personas():
    """phrase id -> persona, including the backup word and phrases recorded on this station."""
    return {w["name"]: w.get("persona") or "grok" for w in wakewords() if w.get("name")}


def phrase_problem(text, known=None):
    """None when the phrase can be a new wake word, otherwise the sentence to show."""
    mid, words = normalize_phrase(text)
    letters = sum(len(w) for w in words)
    if words and all(w in COMMON_WORDS for w in words):
        return common_msg(phrase_label(mid))
    if len(words) < 2 or letters < 6:
        return TOO_SHORT
    if len(words) > 4:
        return TOO_LONG
    if not phrase_ok(mid):
        return TOO_LONG_ID
    known = list(known_phrase_ids() if known is None else known)
    if mid in known:
        return duplicate_msg(phrase_label(mid))
    mine_syl = phrase_syllables(words)
    for other in known:
        if not isinstance(other, str) or not other:
            continue
        ow = [p for p in other.split("_") if p]
        if not ow:
            continue
        if lev_ratio(mid, other) >= 0.75 or (words[-1] == ow[-1] and mine_syl == phrase_syllables(ow)):
            return too_close_msg(phrase_label(other))
    return None


def persona_allowed(slug):
    """Grok, or a slug on the Bots list. When that list can't be loaded, a well-formed slug is enough."""
    if not persona_ok(slug):
        return False
    if slug == "grok":
        return True
    got = relay_call("GET", "/admin/bots")
    if not got or got[0] != 200:
        return True
    data = got[1] if isinstance(got[1], dict) else {}
    return any(isinstance(b, dict) and b.get("slug") == slug for b in (data.get("bots") or []))


def save_phrase_persona(phrase, persona):
    """Remember phrase id -> persona in the overrides the wake listener already reads."""
    ov = read_json(path("wake-overrides.json"), {})
    if not isinstance(ov, dict):
        ov = {}
    o = ov.get(phrase) if isinstance(ov.get(phrase), dict) else {}
    o["persona"] = persona
    ov[phrase] = o
    write_json(path("wake-overrides.json"), ov, 0o640)


def drop_phrase_persona(phrase):
    ov = read_json(path("wake-overrides.json"), {})
    if isinstance(ov, dict) and phrase in ov:
        ov.pop(phrase, None)
        write_json(path("wake-overrides.json"), ov, 0o640)


def read_enroll_status():
    doc = read_json(path("enroll-status.json"), {})
    return doc if isinstance(doc, dict) else {}


def write_enroll_status(doc):
    write_json(path("enroll-status.json"), doc, 0o640)


def enrolled_phrase_ids():
    return {w.get("name") for w in read_json(ENROLLED, {}).get("words", []) if isinstance(w, dict) and w.get("name")}


def set_enroll_state(phrase, state, message, persona=None, session=None, had_model=None):
    doc = read_enroll_status()
    old = doc.get(phrase) if isinstance(doc.get(phrase), dict) else {}
    row = {"state": state, "message": message, "updated_at": int(now())}
    who = persona or old.get("persona")
    if who:
        row["persona"] = who
    ses = session or old.get("session")
    if ses:
        row["session"] = ses
    if had_model is None:
        had_model = old.get("had_model")
    if had_model is not None:
        row["had_model"] = bool(had_model)
    doc[phrase] = row
    write_enroll_status(doc)
    return row


def _status_row(phrase, state, message, now_ts, prev, persona=None, session=None, had_model=None):
    old = prev.get(phrase) if isinstance(prev.get(phrase), dict) else {}
    row = {"state": state, "message": message, "updated_at": now_ts}
    if old.get("state") == state and (old.get("message") or "") == (message or ""):
        row["updated_at"] = old.get("updated_at", now_ts)
    who = persona or old.get("persona")
    if who:
        row["persona"] = who
    ses = session or old.get("session")
    if ses:
        row["session"] = ses
    if had_model is None:
        had_model = old.get("had_model")
    if had_model is not None:
        row["had_model"] = bool(had_model)
    return row


def quality_message(scores=None):
    """The sentence the Wake words page and the per-bot button show for a gate miss."""
    scores = scores if isinstance(scores, dict) else {}
    recall = scores.get("recall_real", scores.get("recall"))
    fa = scores.get("fa_h", scores.get("false_accepts", scores.get("fa_per_hour")))
    if isinstance(recall, bool) or not isinstance(recall, (int, float)):
        recall = None
    if isinstance(fa, bool) or not isinstance(fa, (int, float)):
        fa = None
    if recall is not None and fa is not None:
        fa_s = f"{float(fa):.3f}".rstrip("0").rstrip(".")
        return (f"Not good enough yet (recall {float(recall):.2f} vs 0.80, false accepts {fa_s}/h vs 1.0/h). "
                "Record more clips, 20-30, somewhere quiet.")
    return "Not good enough yet (below the gate). Record more clips, 20-30, somewhere quiet."


def map_train_state(state, log="", scores=None):
    """Host training status → the state the admin page stores.

    "done" is never a success. "JOB DONE below the gate, not published" is failed_quality.
    recording/uploading/training/ready/failed/idle pass through (admin "ready" means the model is ready).
    """
    log = log or ""
    low = log.lower()
    st = "" if state is None else str(state)
    if st == "failed_quality" or "below the gate" in low:
        return "failed_quality"
    if st == "published":
        return "published"
    if st == "processing":
        return "processing"
    if st == "done":
        if "not published" in low or "below the gate" in low:
            return "failed_quality"
        if "published" in low:
            return "published"
        return "failed_quality"
    if st in ("recording", "uploading", "training", "ready", "failed", "idle"):
        return st
    return st


def derive_enroll_status(prev, wake_status, enrolled_names, now_ts):
    """Fold the listener's enroll stage and any published model into the persisted per-phrase status.

    wake_status None means the listener didn't answer: keep what was saved, and still notice a model
    that landed in the enrolled list. had_model is whether a model already existed when this attempt
    started, so a top-up doesn't look finished just because the previous model is still there.
    """
    prev = prev if isinstance(prev, dict) else {}
    out = {}
    kept = ("idle", *ACTIVE_ENROLL, "ready", "failed", "processing", "published", "failed_quality")
    for k, v in prev.items():
        if not (isinstance(k, str) and isinstance(v, dict)):
            continue
        row = dict(v)
        scores = row.get("scores") if isinstance(row.get("scores"), dict) else None
        st = map_train_state(row.get("state"), str(row.get("message") or ""), scores)
        if st not in kept:
            continue
        if st != row.get("state"):
            row["state"] = st
        if st == "failed_quality" and not str(row.get("message") or "").startswith("Not good enough"):
            row["message"] = quality_message(scores)
        out[k] = row
    enrolled = {n for n in (enrolled_names or []) if n}
    last, busy = {}, False
    if isinstance(wake_status, dict):
        busy = bool(wake_status.get("enrolling"))
        last = wake_status.get("last_enroll") if isinstance(wake_status.get("last_enroll"), dict) else {}
    phrase = str(last.get("phrase") or "")
    stage = str(last.get("stage") or "")
    msg = str(last.get("message") or "")[:240]
    persona = str(last.get("persona")) if persona_ok(str(last.get("persona") or "")) else None
    session = last.get("session") if isinstance(last.get("session"), str) else None
    if wake_name_ok(phrase):
        entry = out.get(phrase) or {}
        state, new_msg = None, msg
        if busy:
            state = stage if stage in ACTIVE_ENROLL else "recording"
            new_msg = msg or {"recording": "Say it on the station.", "uploading": "Sending the clips.",
                              "training": "Training."}[state]
        elif stage == "failed":
            state, new_msg = "failed", msg or "Training didn't pass."
        elif stage == "failed_quality":
            state = "failed_quality"
            new_msg = msg if msg.startswith("Not good enough") else quality_message(None)
        elif stage == "published":
            state, new_msg = "published", msg or "Ready."
        elif stage == "processing":
            state, new_msg = "processing", msg or "Processing…"
        elif stage == "done":
            state = map_train_state("done", msg)
            new_msg = quality_message(None) if state == "failed_quality" else (msg or "Ready.")
        elif stage == "ready":
            state, new_msg = "ready", msg or "Ready."
        elif stage in ACTIVE_ENROLL or entry.get("state") in (*ACTIVE_ENROLL, "processing"):
            state, new_msg = "training", msg or "Training."
        if state:
            had = entry.get("had_model")
            if had is None and state == "recording":
                had = phrase in enrolled
            out[phrase] = _status_row(phrase, state, new_msg, now_ts, out, persona, session, had)
    if not busy:
        for name, row in list(out.items()):
            if name in enrolled and row.get("state") in ("uploading", "training") and not row.get("had_model"):
                out[name] = _status_row(name, "ready", "Ready.", now_ts, out)
    return out


def refresh_enroll_status():
    """Poll the wake listener and the enrolled-model list, then write enroll-status.json."""
    code, st = wake("GET", "/status")
    wake_status = st if code == 200 and isinstance(st, dict) else None
    prev = read_enroll_status()
    doc = derive_enroll_status(prev, wake_status, enrolled_phrase_ids(), int(now()))
    if doc != prev:
        write_enroll_status(doc)
    return doc


def record_button(rows):
    """What the Bots page shows for one bot, from that bot's phrase statuses.

    "Processing…" while any phrase is recording, uploading, training, or processing.
    "Ready" once a model is ready or published. A gate miss uses the quality sentence.
    A hard failure keeps "Record voice" and carries the short reason.
    """
    rows = [r for r in rows if isinstance(r, dict)]

    def ts(r):
        return r.get("updated_at") or 0

    active = [r for r in rows if r.get("state") in (*ACTIVE_ENROLL, "processing")]
    if active:
        top = max(active, key=ts)
        return {"label": "Processing…", "processing": True, "message": top.get("message") or "Processing…",
                "phrase": top.get("phrase") or "", "state": top.get("state")}
    if not rows:
        return {"label": "Record voice", "processing": False, "message": "", "phrase": "", "state": "idle"}
    top = max(rows, key=ts)
    if top.get("state") == "failed":
        return {"label": "Record voice", "processing": False, "message": top.get("message") or "Something went wrong.",
                "phrase": top.get("phrase") or "", "state": "failed"}
    if top.get("state") == "failed_quality":
        msg = top.get("message") or ""
        if not str(msg).startswith("Not good enough"):
            msg = quality_message(top.get("scores") if isinstance(top.get("scores"), dict) else None)
        return {"label": msg, "processing": False, "message": msg, "phrase": top.get("phrase") or "",
                "state": "failed_quality"}
    if top.get("state") in ("ready", "published"):
        return {"label": "Ready", "processing": False, "message": "", "phrase": top.get("phrase") or "",
                "state": top.get("state")}
    return {"label": "Record voice", "processing": False, "message": "", "phrase": top.get("phrase") or "",
            "state": top.get("state") or "idle"}


def buttons_for(doc):
    groups = {}
    for name, row in (doc or {}).items():
        if not isinstance(row, dict):
            continue
        who = str(row.get("persona") or "")
        if not persona_ok(who):
            who = "grok"
        groups.setdefault(who, []).append({**row, "phrase": name})
    return {who: record_button(rows) for who, rows in groups.items()}


def webhook_url_ok(url):
    """https only, host exactly api2.cursor.sh, no userinfo, port 443 or omitted, path non-empty, <= 512."""
    if not isinstance(url, str) or not 12 <= len(url) <= 512:
        return False
    if any(c in url for c in " \t\r\n\\"):
        return False
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.username or u.password or u.fragment:
        return False
    hostport = u.netloc
    if not hostport or "@" in hostport or hostport.startswith("["):
        return False
    if hostport.count(":") > 1:
        return False
    host, port = hostport, None
    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
        if port != "443":
            return False
    if host.lower().rstrip(".") != WEBHOOK_HOST or u.hostname != WEBHOOK_HOST:
        return False
    return bool(u.path)


def webhook_key_ok(key):
    return isinstance(key, str) and re.fullmatch(r"[\x21-\x7e]{8,400}", key) is not None


def voice_token_ok(tok):
    return isinstance(tok, str) and re.fullmatch(r"vt_[\x21-\x7e]{4,180}", tok) is not None


def clean_name(v):
    if not isinstance(v, str):
        return None
    name = " ".join(v.split())
    if not name or len(name) > 40 or any(ord(c) < 32 for c in name):
        return None
    return name


def voice_ok(v):
    return isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", v) is not None


def _epoch(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    n = int(v)
    if n < 0 or n > 4_000_000_000:
        return None
    return n


def _num(v, limit=3600):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    x = float(v)
    if x < 0 or x > limit:
        return None
    return round(x, 3)


def public_error(msg, secrets=()):
    """A short relay error safe to show. Anything with a URL, a key, or odd characters becomes generic."""
    generic = "The voice service refused that."
    if not isinstance(msg, str):
        return generic
    msg = " ".join(msg.split())
    if not msg or len(msg) > 200 or not re.fullmatch(r"[\x20-\x7e]+", msg):
        return generic
    low = msg.lower()
    if "http:" in low or "https:" in low or "bearer " in low:
        return generic
    for s in secrets:
        if isinstance(s, str) and len(s) >= 8 and s.lower() in low:
            return generic
    return msg


def scrub_note(msg):
    """last_error shown on the bot row: short, no URL."""
    if not isinstance(msg, str):
        return None
    msg = " ".join(msg.split())[:180]
    if not msg or not re.fullmatch(r"[\x20-\x7e]+", msg):
        return None
    low = msg.lower()
    if "http:" in low or "https:" in low or "bearer " in low:
        return None
    return msg


def audit(persona, action, last4=None):
    """One journal line per change. last4 only; the key itself is never written."""
    tail = last4 if isinstance(last4, str) and re.fullmatch(r"[A-Za-z0-9]{4}", last4) else "-"
    print(f"audit persona={persona} action={action} last4={tail} time={int(now())}", flush=True)


def audit_stepup():
    """One journal line when a step-up window opens. The password is not written."""
    print(f"audit persona=- action=stepup open last4=- time={int(now())}", flush=True)


def drop_stale_guest():
    """Remove a leftover guest-mode flag from t1.json. Missing file or key is a no-op.

    Speaker records keep their own guest flag. Only the station-wide switch is dropped.
    """
    p = path("t1.json")
    try:
        with open(p) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(doc, dict) or "guest" not in doc:
        return
    doc.pop("guest", None)
    try:
        write_json(p, doc, 0o640)
    except OSError:
        pass


def schedule_persona_sync(delay=30):
    """Ask the wake listener to re-sync portraits after the relay has had time to draw one."""
    def run():
        time.sleep(delay)
        try:
            wake("POST", "/personas/sync", {})
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


PII_TEXT = ("Always blocked, in every mode: passwords, keys, tokens, SSNs, and card numbers. "
            "When this is on, replies also block personal info: household names, emails, phones, "
            "addresses, IPs, MACs, and host names.")
AVATAR_MAX = 2_000_000
AVATAR_ERROR = "Use a png, jpeg, or webp up to 2 MB."
AVATAR_POST = re.compile(r"^/api/bots/([a-z][a-z0-9-]{1,23})/avatar$")


def sniff_image(ctype, data):
    """Content type to send the relay, or None when the bytes don't match the declared type."""
    ctype = str(ctype or "").split(";")[0].strip().lower()
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    if ctype == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if ctype in ("image/jpeg", "image/jpg") and data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if ctype == "image/webp" and len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _private_file(p):
    """True when the file exists and is not group- or world-readable."""
    try:
        st = os.stat(p)
    except OSError:
        return False
    return not (st.st_mode & 0o077)


def _env_file(p):
    vals = {}
    try:
        with open(p) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


LOCAL_RELAY_TOKEN = E("ADMIN_LOCAL_RELAY_TOKEN_FILE", "/etc/grok-station/secrets/admin_token")
LOCAL_RELAY_ORIGIN = "http://127.0.0.1:" + E("RELAY_PORT", "8790")


def local_relay_settings():
    """Single device: the relay runs on this Pi on loopback. Token file is readable by this user only."""
    try:
        token = open(LOCAL_RELAY_TOKEN).read().strip()
    except OSError:
        return None
    if not token or len(token) > 400 or "\n" in token:
        return None
    return {"origin": LOCAL_RELAY_ORIGIN, "token": token, "local": True}


def relay_settings():
    """Admin relay settings from the env file, or None when the station isn't set up to call it.

    Refuses a non-https URL, a world/group-readable env file, key, or token, and a pin that isn't
    64 hex chars. The token stays in the returned dict for this call only and is never logged.
    """
    if not _private_file(RELAY_ENV):
        return None
    vals = _env_file(RELAY_ENV)
    if not vals:
        return None
    url = vals.get("ADMIN_RELAY_URL", "")
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        return None
    ca, cert, key, tokf = (vals.get(k, "") for k in
                           ("ADMIN_RELAY_CA", "ADMIN_RELAY_CERT", "ADMIN_RELAY_KEY", "ADMIN_RELAY_TOKEN_FILE"))
    if not ca or not cert or not key or not tokf:
        return None
    if not os.path.isfile(ca) or not os.path.isfile(cert) or not _private_file(key) or not _private_file(tokf):
        return None
    try:
        token = open(tokf).read().splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    if not token or len(token) > 400:
        return None
    pin = vals.get("ADMIN_RELAY_PIN", "").strip().lower()
    if pin and not re.fullmatch(r"[0-9a-f]{64}", pin):
        return None
    origin = f"https://{u.hostname}:{u.port or 443}"
    return {"origin": origin, "ca": ca, "cert": cert, "key": key, "token": token, "pin": pin}


def relay_ssl_context(cfg):
    """Verify the relay with only the pinned CA file (not the system store) and present the client cert."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=cfg["ca"])  # fresh context: this CA only, no load_default_certs
    ctx.load_cert_chain(certfile=cfg["cert"], keyfile=cfg["key"])
    ctx.check_hostname = False  # the optional SHA-256 pin below is the name check
    return ctx


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _pinned_connection(pin):
    class Conn(http.client.HTTPSConnection):
        def connect(self):
            super().connect()
            if not pin:
                return
            der = self.sock.getpeercert(binary_form=True) or b""
            got = hashlib.sha256(der).hexdigest()
            if not hmac.compare_digest(got, pin):
                try:
                    self.sock.close()
                finally:
                    raise ssl.SSLCertVerificationError("pin mismatch")
    return Conn


def relay_call(method, path, payload=None, timeout=10, raw=None, content_type=None):
    """Call the relay admin API. None means the station has no relay admin config.

    A failed call is (503, {error}). The exception text is not logged (it can contain the URL).
    raw is a request body sent as content_type (a portrait). It is not logged.
    """
    cfg = local_relay_settings() or relay_settings()
    if not cfg:
        return None
    if cfg.get("local"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    else:
        ctx = relay_ssl_context(cfg)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),  # never send the admin token via a proxy
            _RelayHTTPS(ctx, _pinned_connection(cfg["pin"])),
            _NoRedirect(),
        )
    headers = {"Authorization": "Bearer " + cfg["token"], "Accept": "application/json"}
    if raw is not None:
        data = raw if isinstance(raw, bytes) else bytes(raw)
        headers["Content-Type"] = content_type or "application/octet-stream"
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    else:
        data = None
    req = urllib.request.Request(cfg["origin"] + path, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read(1 << 20)
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read(1 << 20)
        except Exception:
            raw = b""
    except Exception as e:
        print(f"relay admin failed: {type(e).__name__}", flush=True)
        return 503, {"error": RELAY_DOWN}
    try:
        parsed = json.loads(raw.decode() or "{}")
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return status, parsed


class _RelayHTTPS(urllib.request.HTTPSHandler):
    def __init__(self, context, conn_cls):
        super().__init__(context=context)
        self._conn_cls = conn_cls

    def https_open(self, req):
        return self.do_open(self._conn_cls, req, context=self._context)


def safe_bot(b):
    """The fields the browser may see. Anything else from the relay (tokens, urls, keys) is dropped."""
    if not isinstance(b, dict) or not slug_ok(b.get("slug")):
        return None
    wh = b.get("webhook") if isinstance(b.get("webhook"), dict) else {}
    st = b.get("status") if isinstance(b.get("status"), dict) else {}
    last4 = wh.get("last4")
    if not (isinstance(last4, str) and re.fullmatch(r"[A-Za-z0-9]{4}", last4)):
        last4 = None
    state = st.get("state")
    if state not in ("connected", "error", "not_configured"):
        state = "not_configured"
    saved = _epoch(wh.get("saved_at"))
    return {
        "slug": b["slug"],
        "name": clean_name(b.get("name")) or b["slug"],
        "voice": b.get("voice") if voice_ok(b.get("voice")) else None,
        "source": b.get("source") if b.get("source") in ("registry", "added") else "registry",
        "webhook": {"configured": bool(wh.get("configured")), "last4": last4, "saved_at": saved},
        "status": {
            "state": state,
            "last_answered_at": _epoch(st.get("last_answered_at")),
            "last_error": scrub_note(st.get("last_error")),
            "last_error_at": _epoch(st.get("last_error_at")),
        },
    }


def safe_voices(vs):
    out = []
    if isinstance(vs, list):
        for v in vs:
            if voice_ok(v) and v not in out:
                out.append(v)
    return out[:50]


def local_bots():
    """Personas cached on the station, each shown as not configured (used when the relay admin API isn't set)."""
    doc = read_json(PERSONAS, {})
    items = doc.get("personas", doc) if isinstance(doc, dict) else doc
    out = []
    for x in items if isinstance(items, list) else []:
        if isinstance(x, dict) and slug_ok(x.get("slug")):
            out.append({
                "slug": x["slug"],
                "name": clean_name(x.get("name")) or x["slug"],
                "voice": x.get("voice") if voice_ok(x.get("voice")) else None,
                "source": "registry",
                "webhook": {"configured": False, "last4": None, "saved_at": None},
                "status": {"state": "not_configured", "last_answered_at": None, "last_error": None, "last_error_at": None},
            })
    return out


# Geocode hits stay here (id -> coords). The browser only ever sees id + label.
GEOCODE_TTL = 15 * 60
GEOCODE, GEOCODE_LOCK = {}, threading.Lock()
_TZ_RE = re.compile(r"[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}")


def coord_key(k):
    """True when a key or message would show a latitude or longitude to the browser."""
    return "lat" in re.sub(r"[^a-z]", "", str(k).lower()) or "lon" in re.sub(r"[^a-z]", "", str(k).lower())


def coord_str(n, limit):
    """Helper's env format, or None. Never logged by the caller."""
    try:
        x = round(float(n), 4)
    except (TypeError, ValueError):
        return None
    if x != x or abs(x) > limit:  # NaN, or outside the range
        return None
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    if s == "-0":
        s = "0"
    if not re.fullmatch(r"-?\d{1,3}(\.\d{1,4})?", s):
        return None
    return s


def _place_part(v):
    if not isinstance(v, str):
        return ""
    s = " ".join(v.split())
    if not s or len(s) > 60 or any(ord(c) < 32 for c in s):
        return ""
    return s


def place_label(row):
    """'City, Region', or the town name when the geocoder has no region."""
    name, region = _place_part(row.get("name")), _place_part(row.get("admin1"))
    if not name:
        return None
    return f"{name}, {region}" if region else name


def remember_places(rows):
    """Store coords privately and return [{id, label}] for the browser."""
    t = now()
    out = []
    with GEOCODE_LOCK:
        for k, v in list(GEOCODE.items()):
            if v.get("exp", 0) <= t:
                GEOCODE.pop(k, None)
        while len(GEOCODE) > 40:
            GEOCODE.pop(next(iter(GEOCODE)))
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = place_label(row)
            lat, lon = coord_str(row.get("latitude"), 90), coord_str(row.get("longitude"), 180)
            if not label or lat is None or lon is None:
                continue
            gid = secrets.token_urlsafe(16)
            while gid in GEOCODE:
                gid = secrets.token_urlsafe(16)
            tz = row.get("timezone") if isinstance(row.get("timezone"), str) else ""
            if not _TZ_RE.fullmatch(tz or ""):
                tz = ""
            GEOCODE[gid] = {"lat": lat, "lon": lon, "label": label, "timezone": tz, "exp": t + GEOCODE_TTL}
            out.append({"id": gid, "label": label})
            if len(out) >= 5:
                break
    return out


def lookup_place(gid):
    if not isinstance(gid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", gid):
        return None
    t = now()
    with GEOCODE_LOCK:
        hit = GEOCODE.get(gid)
        if not hit or hit.get("exp", 0) <= t:
            GEOCODE.pop(gid, None)
            return None
        return dict(hit)


def location_label():
    d = read_json(path("location.json"), {})
    if not isinstance(d, dict) or not isinstance(d.get("label"), str):
        return None
    lab = " ".join(d["label"].split())
    if not lab or len(lab) > 80 or any(ord(c) < 32 for c in lab):
        return None
    return lab


def save_location_label(label):
    write_json(path("location.json"), {"label": label}, 0o640)


def public_time(out):
    """Settings the browser may see: the place label, never coordinates."""
    src = out.get("values") if isinstance(out, dict) else None
    vals, had = {}, False
    if isinstance(src, dict):
        for k, v in src.items():
            if coord_key(k):
                if v not in (None, ""):
                    had = True
                continue
            vals[k] = v
    label = location_label()
    if label:
        vals["location"] = label
    elif had:
        vals["location"] = "Saved location"
    return {"ok": True, "values": vals}


def time_body(j):
    """Browser JSON -> helper stdin. Coordinates come only from a remembered geocode id."""
    if not isinstance(j, dict):
        return None, "bad request", None, None
    body = {}
    if isinstance(j.get("timezone"), str):
        body["timezone"] = j["timezone"]
    if "GROK_STATION_UNITS" in j:
        body["GROK_STATION_UNITS"] = j["GROK_STATION_UNITS"]
    label = tz = None
    if "place" in j:
        hit = lookup_place(j.get("place"))
        if not hit:
            return None, "That place expired. Look it up again.", None, None
        body["GROK_STATION_LAT"] = hit["lat"]
        body["GROK_STATION_LON"] = hit["lon"]
        label = hit["label"]
        if hit.get("timezone"):
            body["timezone"] = hit["timezone"]
        tz = body.get("timezone")
    else:
        tz = body.get("timezone")
    return body, None, label, tz


# ------------------------------------------------------------------ relay pairing + updates
_PAIR_STOP = None
_PAIR_THREAD = None
_ISO_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_XAI_CODE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_UPDATE_REASONS = {"bad-args", "not-in-manifest", "downgrade", "relay-failed", "station-failed"}


def ca_dir():
    return os.path.join(STATE_DIR, "pairing-ca")


def paired_dir():
    return os.path.join(STATE_DIR, "paired-relay")


def _pair_clock():
    return now()


def append_audit(line):
    """Append one already-formatted audit line. Drops anything with a secret marker."""
    if not isinstance(line, str) or not line or "\n" in line or "\r" in line:
        return
    low = line.lower()
    if "-----begin" in low or "://" in low or "bearer " in low:
        return
    p = path("relay-audit.log")
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        return


def _audit_event(line):
    parts = line.split(" ")
    return parts[3] if len(parts) >= 4 else ""


def recent_audit(limit=20, event_prefix=None):
    try:
        lines = open(path("relay-audit.log"), encoding="utf-8").read().splitlines()
    except OSError:
        lines = []
    if event_prefix:
        lines = [ln for ln in lines if _audit_event(ln).startswith(event_prefix)]
    return lines[-limit:]


def _audit_update(event, detail="-"):
    if not isinstance(detail, str) or not detail:
        detail = "-"
    ts = datetime.fromtimestamp(int(now()), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        line = release.format_audit(ts, "admin", "station", event, detail)
    except ValueError:
        return
    append_audit(line)


def _vt(value):
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
        return None
    return tuple(int(part) for part in value[1:].split("."))


def _station_ver():
    """Release version, or None when VERSION is not vX.Y.Z (a dev tree)."""
    return version() if _vt(version()) else None


def station_cert_bytes():
    try:
        with open(os.path.join(TLS_DIR, "cert.pem"), "rb") as handle:
            data = handle.read(64 * 1024)
    except OSError:
        return None
    if not data or b"PRIVATE" in data:
        return None
    return data


def station_fingerprint_hex():
    raw = station_cert_bytes()
    if not raw:
        return None
    try:
        return pairing.fingerprint_bytes(raw).hex()
    except pairing.PairingError:
        return None


def _show_station_fp():
    fp = station_fingerprint_hex()
    if not fp or len(fp) != 64:
        return
    payload = {"fingerprint": fp}
    post = STATION_POST
    if post is not None:
        try:
            post(payload)
        except Exception:
            pass
        return
    helper("display-pairing", stdin=payload)


def _clear_station_fp():
    payload = {"clear": True}
    post = STATION_POST
    if post is not None:
        try:
            post(payload)
        except Exception:
            pass
        return
    helper("display-pairing", stdin=payload)


def _on_pair_bind():
    ok, _ = helper("pair-fw-open")
    if not ok:
        return False
    _show_station_fp()
    return True


def _on_pair_unbind():
    helper("pair-fw-close")
    _clear_station_fp()


def _start_pair_thread():
    global _PAIR_STOP, _PAIR_THREAD
    if _PAIR_THREAD is not None and _PAIR_THREAD.is_alive():
        return
    _PAIR_STOP = threading.Event()
    listener = PAIR_LISTENER
    _PAIR_THREAD = threading.Thread(target=listener.serve, args=(_PAIR_STOP,), name="pair-port", daemon=True)
    _PAIR_THREAD.start()


def load_ca():
    global PAIR_CA
    if PAIR_CA is None:
        PAIR_CA = pairing.StationCA.load_or_create(ca_dir(), clock=_pair_clock, audit=append_audit)
    return PAIR_CA


def ensure_pair():
    """Load the CA and the pairing window. The listener stays unbound until the window is open."""
    global PAIR_CA, PAIR_WINDOW, PAIR_LISTENER, PAIR_ISSUANCE
    load_ca()
    cert = station_cert_bytes()
    if cert is None or not os.path.isfile(os.path.join(TLS_DIR, "key.pem")):
        raise pairing.PairingError("station-cert")
    if PAIR_WINDOW is None:
        PAIR_WINDOW = pairing.PairingWindow(cert, ca=PAIR_CA, clock=_pair_clock, audit=append_audit)
    if PAIR_ISSUANCE is None:
        PAIR_ISSUANCE = pair_listener.Issuance()
    if PAIR_LISTENER is None:
        PAIR_LISTENER = pair_listener.PairListener(
            PAIR_WINDOW, PAIR_ISSUANCE,
            os.path.join(TLS_DIR, "cert.pem"), os.path.join(TLS_DIR, "key.pem"),
            host=PAIR_HOST, port=PAIR_PORT, socket_factory=PAIR_SOCKET_FACTORY,
            on_bind=_on_pair_bind, on_unbind=_on_pair_unbind,
        )
        _start_pair_thread()
    return PAIR_LISTENER


def sync_pair_port():
    listener = PAIR_LISTENER
    if listener is None:
        return False
    try:
        listener.poll()
    except Exception:
        return False
    return bool(listener.bound)


def reset_pair_runtime():
    """Stop the pairing thread and drop in-memory window state. Disk CA stays."""
    global PAIR_CA, PAIR_WINDOW, PAIR_LISTENER, PAIR_ISSUANCE, _PAIR_STOP, _PAIR_THREAD
    global RELAY_FETCHER, MANIFEST_FETCHER, STATION_POST
    stop, thread, listener = _PAIR_STOP, _PAIR_THREAD, PAIR_LISTENER
    if stop is not None:
        stop.set()
    if listener is not None:
        try:
            listener.unbind()
        except Exception:
            pass
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2)
    PAIR_CA = PAIR_WINDOW = PAIR_LISTENER = PAIR_ISSUANCE = None
    _PAIR_STOP = _PAIR_THREAD = None
    RELAY_FETCHER = None
    MANIFEST_FETCHER = None
    STATION_POST = None


def _write_bytes(dest, data, mode):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = f"{dest}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, dest)


def _paired_record():
    doc = read_json(os.path.join(paired_dir(), "relay.json"), None)
    return doc if isinstance(doc, dict) else None


def _key_file_ok(p):
    if os.path.islink(p) or not _private_file(p):
        return False
    return True


def save_paired(serial, cert_expiry, relay_fp, key, cert, ca_cert):
    """Store the station's client key (mode 0600) and the relay serial. No admin-relay.env changes."""
    directory = paired_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    _write_bytes(os.path.join(directory, "client-key.pem"), key_pem, 0o600)
    _write_bytes(os.path.join(directory, "client-cert.pem"), cert.public_bytes(serialization.Encoding.PEM), 0o644)
    _write_bytes(os.path.join(directory, "ca.pem"), ca_cert.public_bytes(serialization.Encoding.PEM), 0o644)
    write_json(os.path.join(directory, "relay.json"), {
        "serial": serial,
        "cert_expiry": cert_expiry,
        "relay_fingerprint": relay_fp,
    })


def drop_paired_files():
    """Remove the paired client files. Does not touch admin-relay.env."""
    directory = paired_dir()
    for name in ("client-key.pem", "client-cert.pem", "ca.pem", "relay.json"):
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:
            pass


def relay_mtls_config():
    """Prefer the paired client cert. Fall back to admin-relay.env. Never deletes that file.

    A paired key with no relay URL still needs admin-relay.env for the origin. Until the
    one-time re-pair, the env file's own client cert is used.
    """
    env = relay_settings()
    directory = paired_dir()
    key_p = os.path.join(directory, "client-key.pem")
    cert_p = os.path.join(directory, "client-cert.pem")
    ca_p = os.path.join(directory, "ca.pem")
    paired = _key_file_ok(key_p) and os.path.isfile(cert_p) and os.path.isfile(ca_p)
    if paired and env:
        return {
            "origin": env["origin"], "ca": env["ca"], "cert": cert_p, "key": key_p,
            "token": env.get("token"), "pin": env.get("pin") or "", "source": "paired",
        }
    if env:
        return {
            "origin": env["origin"], "ca": env["ca"], "cert": env["cert"], "key": env["key"],
            "token": env.get("token"), "pin": env.get("pin") or "", "source": "env",
        }
    return None


def _relay_configured():
    return RELAY_FETCHER is not None or relay_mtls_config() is not None


def relay_admin_fetch(method, path, payload=None):
    """Call the relay admin API over mTLS. RELAY_FETCHER skips the network in tests.

    POST /admin/update sends only {"version": "..."}.
    """
    if path == "/admin/update":
        if not isinstance(payload, dict) or set(payload) != {"version"} or not _vt(payload.get("version")):
            return 400, {"error": "bad request"}
    if RELAY_FETCHER is not None:
        return RELAY_FETCHER(method, path, payload)
    cfg = relay_mtls_config()
    if not cfg:
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = False
    try:
        ctx.load_verify_locations(cafile=cfg["ca"])
        ctx.load_cert_chain(certfile=cfg["cert"], keyfile=cfg["key"])
    except (OSError, ssl.SSLError):
        return 503, {"error": RELAY_DOWN}
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RelayHTTPS(ctx, _pinned_connection(cfg.get("pin") or "")),
        _NoRedirect(),
    )
    headers = {"Accept": "application/json"}
    token = cfg.get("token")
    if isinstance(token, str) and token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(cfg["origin"] + path, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=10) as resp:
            status, raw = resp.status, resp.read(1 << 20)
    except urllib.error.HTTPError as err:
        status = err.code
        try:
            raw = err.read(1 << 20)
        except Exception:
            raw = b""
    except Exception as err:
        print(f"relay admin failed: {type(err).__name__}", flush=True)
        return 503, {"error": RELAY_DOWN}
    try:
        parsed = json.loads(raw.decode() or "{}")
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return status, parsed


def _safe_time(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and 0 <= float(value) < 4_000_000_000:
        return int(value)
    if isinstance(value, str) and _ISO_TIME.fullmatch(value):
        return value
    return None


def _safe_rate(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value < 0 or value > 1_000_000:
            return None
        return int(value) if isinstance(value, int) or float(value).is_integer() else value
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._/-]{1,32}", value):
        return value
    return None


def public_relay_status(data, latency_ms):
    """Fields the browser may see. Question text, URLs, tokens and keys are dropped."""
    data = data if isinstance(data, dict) else {}
    xai = data.get("xai")
    if xai == "ok":
        xai_out = "ok"
    elif isinstance(xai, str) and _XAI_CODE.fullmatch(xai):
        xai_out = xai
    else:
        xai_out = "error"
    last4 = data.get("key_last4")
    if not (isinstance(last4, str) and re.fullmatch(r"[A-Za-z0-9]{4}", last4)):
        last4 = None
    pii = data.get("pii_mode")
    if isinstance(pii, bool):
        pii = "on" if pii else "off"
    elif pii not in ("on", "off"):
        pii = None
    ver = data.get("version") if _vt(data.get("version")) else None
    if isinstance(latency_ms, bool) or not isinstance(latency_ms, (int, float)) or not 0 <= latency_ms <= 120_000:
        latency = None
    else:
        latency = int(latency_ms)
    expiry = _safe_time(data.get("cert_expiry"))
    if expiry is None:
        rec = _paired_record() or {}
        expiry = rec.get("cert_expiry") if isinstance(rec.get("cert_expiry"), str) and _ISO_TIME.fullmatch(rec["cert_expiry"]) else None
    return {
        "version": ver,
        "last_seen": _safe_time(data.get("last_seen")),
        "latency_ms": latency,
        "xai": xai_out,
        "key_last4": last4,
        "cert_expiry": expiry,
        "pii_mode": pii,
        "rate": _safe_rate(data.get("rate")),
    }


def fetch_relay_status():
    if not _relay_configured() and not _paired_record():
        return None
    started = time.perf_counter()
    got = relay_admin_fetch("GET", "/admin/status", None)
    latency = int((time.perf_counter() - started) * 1000)
    if not got or got[0] != 200:
        return None
    pub = public_relay_status(got[1], latency)
    if pub.get("version"):
        cache = read_json(path("update-check.json"), {})
        if not isinstance(cache, dict):
            cache = {}
        cache["relay_version"] = pub["version"]
        write_json(path("update-check.json"), cache)
    return pub


def _outdated(relay_version):
    cache = read_json(path("update-check.json"), {})
    available = cache.get("available") if isinstance(cache, dict) else None
    left, right = _vt(relay_version), _vt(available)
    return bool(left and right and right > left)


def relay_state_name(status):
    phase = PAIR_WINDOW.phase if PAIR_WINDOW is not None else "idle"
    if phase in ("open", "pending"):
        return "pairing"
    if not _relay_configured() and not _paired_record():
        return "not-set-up"
    if not status:
        return "error"
    if _outdated(status.get("version")):
        return "outdated"
    return "connected"


def pairing_public(code=None):
    """Window fields for the browser. `code` is set only by the start and new-code responses."""
    if PAIR_WINDOW is None:
        info = {"expires_in": None, "tries_left": pairing.MAX_TRIES}
    else:
        snap = PAIR_WINDOW.snapshot()
        info = {"expires_in": snap.get("expires_in"), "tries_left": snap.get("tries_left")}
        fp = snap.get("relay_fingerprint")
        if isinstance(fp, str) and fp:
            info["relay_fingerprint"] = fp
    if code:
        info["code"] = code
    return info


def _settings_path():
    return path("update-settings.json")


def _load_manifest():
    if MANIFEST_FETCHER is not None:
        raw = MANIFEST_FETCHER()
    else:
        def fetcher(url, context):
            req = urllib.request.Request(
                url, headers={"Accept": "application/json", "User-Agent": "station-admin"})
            with urllib.request.urlopen(req, timeout=10, context=context) as resp:
                return resp.read(release.MAX_MANIFEST_BYTES + 1)
        raw = release.fetch_manifest(fetcher)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if isinstance(raw, (bytes, bytearray)):
        doc = json.loads(bytes(raw))
        if not isinstance(doc, dict):
            raise release.ManifestError("bad-format", "manifest")
        return doc
    raise release.ManifestError("bad-format", "manifest")


def _cant_check(reason):
    if reason == "expired":
        return {"title": release.EXPIRED_TITLE, "message": release.EXPIRED_MESSAGE}
    return {"title": release.EXPIRED_TITLE, "message": "Updates can't be checked right now."}


def _store_check(available, cant, plan, station_only, relay_version):
    prev = read_json(path("update-check.json"), {})
    if not isinstance(prev, dict):
        prev = {}
    if relay_version is None:
        relay_version = prev.get("relay_version") if _vt(prev.get("relay_version")) else None
    write_json(path("update-check.json"), {
        "available": available,
        "cant_check": cant,
        "plan": plan,
        "station_only_allowed": bool(station_only),
        "relay_version": relay_version,
        "checked_at": int(now()),
    })


def run_update_check():
    """Fetch the manifest (injected in tests) and cache the plan. No socket unless the fetcher opens one."""
    try:
        manifest = _load_manifest()
        station = _station_ver() or "v0.0.0"
        result = release.check_update(station, manifest, now=datetime.fromtimestamp(now(), timezone.utc))
    except release.ManifestError as err:
        reason = err.reason if err.reason in ("expired", "future", "bad-format", "fetch-failed") else "fetch-failed"
        _store_check(None, _cant_check(reason), None, False, None)
        _audit_update("update-check-expired" if reason == "expired" else "update-check-failed", reason if reason != "expired" else "-")
        return
    status = fetch_relay_status()
    relay_v = status.get("version") if status else None
    if result.reason == "expired":
        _store_check(None, _cant_check("expired"), None, False, relay_v)
        _audit_update("update-check-expired", "-")
        return
    available = result.version if result.update and _vt(result.version) else None
    plan, station_only = None, False
    if relay_v and _vt(station):
        try:
            planned = release.plan_update(station, relay_v, manifest)
            plan = list(planned)
            station_only = bool(planned.station_only_allowed)
        except release.ManifestError:
            plan = None
    _store_check(available, None, plan, station_only, relay_v)
    detail = result.version if _vt(result.version) else (result.reason or "-")
    if detail is None:
        detail = "-"
    _audit_update("update-check", detail if isinstance(detail, str) else "-")


def updates_view():
    cache = read_json(path("update-check.json"), {})
    if not isinstance(cache, dict):
        cache = {}
    try:
        settings = release.load_settings(_settings_path())
    except (OSError, ValueError):
        settings = {"autocheck": False}
    relay_v = cache.get("relay_version") if _vt(cache.get("relay_version")) else None
    cant = cache.get("cant_check")
    if not isinstance(cant, dict):
        cant = None
    plan = cache.get("plan") if isinstance(cache.get("plan"), list) else None
    return {
        "installed": {"station": version(), "relay": relay_v},
        "available": cache.get("available") if _vt(cache.get("available")) else None,
        "cant_check": cant,
        "plan": plan,
        "station_only_allowed": bool(cache.get("station_only_allowed")),
        "autocheck": settings.get("autocheck") is True,
        "history": recent_audit(20, "update"),
    }


def run_apply(version):
    """Relay first (only {"version"}), then the station helper. A relay failure skips the station."""
    manifest = _load_manifest()
    station = _station_ver()
    if station is None:
        _audit_update("update-rejected", "bad-args")
        return 400, {"error": "This station's version isn't a release."}
    try:
        checked = release.check_update(station, manifest, now=datetime.fromtimestamp(now(), timezone.utc))
        station_decision = release.validate_update_request({"version": version}, station, manifest)
    except release.ManifestError:
        _audit_update("update-rejected", "bad-args")
        return 400, {"error": "The update request isn't valid."}
    if checked.reason == "expired":
        _audit_update("update-check-expired", "-")
        return 409, {"error": release.EXPIRED_MESSAGE}
    if station_decision.reason == "not-in-manifest":
        _audit_update("update-rejected", "not-in-manifest")
        return 400, {"error": "That version isn't the release."}
    if station_decision.reason == "bad-args":
        _audit_update("update-rejected", "bad-args")
        return 400, {"error": "The update request isn't valid."}
    status = fetch_relay_status()
    relay_v = status.get("version") if status else None
    if not relay_v:
        cache = read_json(path("update-check.json"), {})
        relay_v = cache.get("relay_version") if isinstance(cache, dict) else None
    relay_needs = True
    if relay_v and _vt(relay_v):
        relay_decision = release.validate_update_request({"version": version}, relay_v, manifest)
        if relay_decision.reason == "not-in-manifest":
            _audit_update("update-rejected", "not-in-manifest")
            return 400, {"error": "That version isn't the release."}
        relay_needs = bool(relay_decision.ok)
    station_needs = bool(station_decision.ok)
    if not relay_needs and not station_needs:
        _audit_update("update-rejected", "downgrade")
        return 400, {"error": "That version isn't newer."}
    if relay_needs:
        got = relay_admin_fetch("POST", "/admin/update", {"version": version})
        if not got or got[0] != 200:
            _audit_update("update-rejected", "relay-failed")
            return 502, {"error": "The relay did not update. The station was not changed."}
    if station_needs:
        ok, out = helper("station-update", version, timeout=120)
        if not ok:
            detail = "bad-args" if isinstance(out, dict) and out.get("error") == "bad version" else "station-failed"
            if detail not in _UPDATE_REASONS:
                detail = "station-failed"
            _audit_update("update-rejected", detail)
            return 500, {"error": "The station update did not start."}
    _audit_update("update-apply", version)
    return 202, {"ok": True, "version": version}


class Handler(BaseHTTPRequestHandler):
    server_version = "grok-station-admin"
    protocol_version = "HTTP/1.1"

    # -- plumbing
    def send(self, code, obj=None, ctype="application/json", body=None, headers=()):
        if body is None:
            body = (json.dumps(obj if obj is not None else {}) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Strict-Transport-Security", "max-age=31536000")  # browsers apply it only once the cert is trusted
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                                    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def cookie(self, name):
        c = http.cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return None
        return c[name].value if name in c else None

    def set_cookie(self, name, val, max_age):
        return ("Set-Cookie", f"{name}={val}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={max_age}")

    def body(self, limit=16384):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too large")
        raw = self.rfile.read(n) if n else b""
        j = json.loads(raw or b"{}")
        if not isinstance(j, dict):
            raise ValueError("object expected")
        return j

    def authed(self):
        return AUTH.valid(self.cookie("gs_session"))

    def log_message(self, fmt, *a):  # method, path (no query) and status only; never bodies
        if a and isinstance(a[0], str):
            print(f"{self.command} {self.path.split('?')[0]} {a[1] if len(a) > 1 else ''}", flush=True)

    def avatar_slug(self):
        if self.command != "POST":
            return None
        m = AVATAR_POST.fullmatch(urllib.parse.urlsplit(self.path).path)
        return m.group(1) if m else None

    def guard(self, write):
        if not lan_ok(self.client_address[0]):
            self.send(403, {"error": "LAN only"})
            return False
        if write:  # CSRF: JSON only, same-origin header (forms can't set it), SameSite=Strict cookies
            if self.headers.get("X-GS-Admin") != "1":
                self.send(403, {"error": "bad request origin"})
                return False
            if self.avatar_slug():
                ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if ctype not in ("image/png", "image/jpeg", "image/jpg", "image/webp"):
                    self.send(400, {"error": AVATAR_ERROR})
                    return False
                return True
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self.send(403, {"error": "bad request origin"})
                return False
        return True

    # -- verbs
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")

    def do_PATCH(self):
        self.route("PATCH")

    def route(self, method):
        u = urllib.parse.urlsplit(self.path)
        p, q = u.path, dict(urllib.parse.parse_qsl(u.query))
        if not self.guard(method != "GET"):
            return
        try:
            if method == "GET" and (p in STATIC or p.startswith("/assets/") or p.startswith("/avatars/")):
                return self.static(p)
            if not p.startswith("/api/"):
                return self.send(404, {"error": "not found"})
            avatar = self.avatar_slug()
            if avatar:
                if not self.authed():
                    return self.send(401, {"error": "sign in", "setup_required": AUTH.setup_required()})
                return self.post_avatar(avatar)
            j = self.body() if method in ("POST", "PUT", "PATCH", "DELETE") else {}
        except ValueError:
            return self.send(400, {"error": "bad request"})
        open_routes = {("GET", "/api/state"), ("POST", "/api/setup/code"), ("POST", "/api/setup/password"),
                       ("POST", "/api/login"), ("POST", "/api/logout")}
        if (method, p) not in open_routes and not self.authed():
            return self.send(401, {"error": "sign in", "setup_required": AUTH.setup_required()})
        fn = getattr(self, "api_" + method.lower() + "_" + re.sub(r"[^a-z0-9]+", "_", p[5:].split("/")[0] or "root"), None)
        if not fn:
            return self.send(404, {"error": "not found"})
        try:
            return fn(p, q, j)
        except Exception as e:  # never leak details
            print(f"error {method} {p}: {type(e).__name__}", flush=True)
            return self.send(500, {"error": "Something went wrong on the station."})

    def static(self, p):
        if p.startswith("/avatars/"):
            name = p[len("/avatars/"):]
            if not AVATAR_NAME.fullmatch(name):
                return self.send(404, {"error": "not found"})
            root = os.path.realpath(AVATAR_DIR)
            f = os.path.realpath(os.path.join(root, name))
            if not (f.startswith(root + os.sep) and os.path.isfile(f)):
                return self.send(404, {"error": "not found"})
            ctype = "image/svg+xml"
        elif p.startswith("/assets/"):
            name = p[8:]
            if not re.fullmatch(r"[a-z0-9_-]{1,40}\.(png|svg|ttf|ico)", name):
                return self.send(404, {"error": "not found"})
            f = os.path.join(WEB, "assets", name)
            ctype = ASSET_TYPE[name.rsplit(".", 1)[1]]
        else:
            name, ctype = STATIC[p]
            f = os.path.join(WEB, name)
        try:
            with open(f, "rb") as fh:
                return self.send(200, body=fh.read(), ctype=ctype)
        except OSError:
            return self.send(404, {"error": "not found"})

    # -- open routes
    def api_get_state(self, p, q, j):
        AUTH.publish()
        signed = self.authed()
        return self.send(200, {"setup_required": AUTH.setup_required(), "mode": AUTH.mode(), "signed_in": signed,
                               "locked_s": AUTH.locked(), "host": hostname() + ".local", "version": version(),
                               "stepup_until": AUTH.stepup_until(self.cookie("gs_session")) if signed else None,
                               "wizard": read_json(path("wizard.json"), {"step": 2, "done": False}) if signed else None})

    def api_post_setup(self, p, q, j):
        if AUTH.locked():
            return self.send(429, {"error": "Too many tries. Wait 15 minutes.", "locked_s": AUTH.locked()})
        if p == "/api/setup/code":
            t = AUTH.try_code(j.get("code"))
            if not t:
                return self.send(429 if AUTH.locked() else 403,
                                 {"error": "That code didn't match. Check the station screen.", "locked_s": AUTH.locked()})
            return self.send(200, {"ok": True}, headers=[self.set_cookie("gs_setup", t, SETUP_TTL)])
        if p == "/api/setup/password":
            was_reset = AUTH.mode() == "reset"
            why = AUTH.set_first_password(self.cookie("gs_setup"), j.get("password"))
            if why:
                return self.send(400, {"error": why})
            wz = read_json(path("wizard.json"), {})
            if not (was_reset and wz.get("done")):  # a password reset keeps every other setting, including a finished wizard
                write_json(path("wizard.json"), {"step": 2, "done": False})
            s = AUTH.new_session()
            return self.send(200, {"ok": True}, headers=[self.set_cookie("gs_session", s, SESSION_TTL),
                                                          self.set_cookie("gs_setup", "", 0)])
        return self.send(404, {"error": "not found"})

    def api_post_login(self, p, q, j):
        if AUTH.locked():
            return self.send(429, {"error": "Too many tries. Wait 15 minutes.", "locked_s": AUTH.locked()})
        s = AUTH.login(str(j.get("username", "")), j.get("password"))
        if not s:
            return self.send(429 if AUTH.locked() else 401, {"error": "Wrong username or password.", "locked_s": AUTH.locked()})
        return self.send(200, {"ok": True}, headers=[self.set_cookie("gs_session", s, SESSION_TTL)])

    def api_post_logout(self, p, q, j):
        AUTH.logout(self.cookie("gs_session"))
        return self.send(200, {"ok": True}, headers=[self.set_cookie("gs_session", "", 0)])

    # -- signed in
    def api_post_password(self, p, q, j):
        why = AUTH.change_password(j.get("old"), j.get("new"))
        if why:
            return self.send(400, {"error": why})
        s = AUTH.new_session()
        return self.send(200, {"ok": True}, headers=[self.set_cookie("gs_session", s, SESSION_TTL)])

    def api_post_wizard(self, p, q, j):
        step = j.get("step")
        if not isinstance(step, int) or not 2 <= step <= 8:
            return self.send(400, {"error": "bad step"})
        write_json(path("wizard.json"), {"step": step, "done": bool(j.get("done"))})
        return self.send(200, {"ok": True})

    def api_get_home(self, p, q, j):
        code, st = wake("GET", "/status")
        ok_relay, relay = helper("relay-test", timeout=15) if q.get("relay") == "1" else (None, {})
        last = None
        try:
            out = subprocess.run(["journalctl", "-u", "grok-station-wake", "--since", "-7d", "-g", "^WAKE ", "-n", "1",
                                  "-o", "short-unix", "--no-pager"], capture_output=True, timeout=5).stdout.decode()
            m = re.search(r"^(\d+)\.\d+ .*WAKE \S+ score=\S+ persona=([a-z]+)", out, re.M)
            if m:
                last = {"at": int(m.group(1)), "who": m.group(2)}
        except (OSError, subprocess.TimeoutExpired):
            pass
        return self.send(200, {"wake": st if code == 200 else None, "relay": relay if ok_relay is not None else None,
                               "last_heard": last, "services": services(), "network": net_summary(),
                               "words": [w["name"] for w in wakewords() if w.get("enabled")], "as_of": int(now())})

    def api_get_preview(self, p, q, j):
        try:
            with open(SNAPSHOT, "rb") as f:
                return self.send(200, body=f.read(), ctype="image/png")
        except OSError:
            return self.send(404, {"error": "no snapshot yet"})

    # settings: display / time / audio (non-secret env keys)
    def api_get_settings(self, p, q, j):
        sec = p.split("/")[3] if p.count("/") >= 3 else ""
        if sec not in ("display", "time", "audio"):
            return self.send(404, {"error": "not found"})
        ok, out = helper("settings-get", sec)
        if not ok:
            return self.send(503, {"error": "Can't read settings."})
        if sec == "time":
            out = public_time(out)
        return self.send(200, out)

    def api_put_settings(self, p, q, j):
        sec = p.split("/")[3] if p.count("/") >= 3 else ""
        if sec not in ("display", "time", "audio"):
            return self.send(404, {"error": "not found"})
        label = tz = None
        if sec == "time":
            j, err, label, tz = time_body(j)
            if err:
                return self.send(400, {"error": err})
        ok, out = helper("settings-set", sec, stdin=j)
        if not ok:
            msg = out.get("error", "Couldn't save.") if isinstance(out, dict) else "Couldn't save."
            if not isinstance(msg, str) or coord_key(msg):
                msg = "Couldn't save."
            return self.send(400, {"error": msg})
        if label:
            save_location_label(label)
        if sec == "audio" or out.get("restart_wake"):
            if not busy():
                helper("restart", "wake")
            else:
                out["pending"] = "Applies after the current conversation or enrollment."
        if sec != "time":
            return self.send(200, {"ok": True, **out})
        body = {"ok": True}
        if out.get("restart_wake"):
            body["restart_wake"] = True
        if out.get("pending"):
            body["pending"] = out["pending"]
        if label:
            body["location"] = label
        if tz:
            body["timezone"] = tz
        return self.send(200, body)

    def api_get_geocode(self, p, q, j):
        name = str(q.get("q", ""))[:60]
        if len(name) < 2:
            return self.send(400, {"error": "Type a town or ZIP."})
        url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({"name": name, "count": 5, "format": "json"})
        try:
            with urllib.request.urlopen(url, timeout=6) as r:
                res = json.loads(r.read(200000)).get("results") or []
        except Exception:
            return self.send(503, {"error": "Can't look that up right now."})
        if not isinstance(res, list):
            res = []
        return self.send(200, {"results": remember_places(res)})

    def api_get_timezones(self, p, q, j):
        try:
            zones = subprocess.run(["timedatectl", "list-timezones"], capture_output=True, timeout=5).stdout.decode().split()
        except (OSError, subprocess.TimeoutExpired):
            zones = []
        return self.send(200, {"zones": zones})

    def api_get_audio(self, p, q, j):
        def cards(tool):
            try:
                out = subprocess.run([tool, "-l"], capture_output=True, timeout=5).stdout.decode()
            except (OSError, subprocess.TimeoutExpired):
                return []
            return sorted({m.group(1) for m in re.finditer(r"^card \d+: \S+ \[([^\]]+)\]", out, re.M)})
        code, st = wake("GET", "/status")
        return self.send(200, {"inputs": cards("arecord"), "outputs": cards("aplay"),
                               "level_dbfs": st.get("level_dbfs") if code == 200 else None,
                               "level_at": st.get("level_at") if code == 200 else None})

    def api_post_audio(self, p, q, j):
        if p == "/api/audio/test":
            code, out = wake("POST", "/test-sound", {})
            return self.send(200 if code == 202 else 409, {"ok": code == 202, "error": None if code == 202 else "The station is busy. Try again in a moment."})
        return self.send(404, {"error": "not found"})

    # secrets: relay token, relay URL and the xAI key are not stored from this page
    def api_get_secrets(self, p, q, j):
        if p != "/api/secrets":
            return self.send(404, {"error": "not found"})
        return self.send(200, {})

    def api_post_secrets(self, p, q, j):
        return self.send(404, {"error": "not found"})

    # network
    def api_get_network(self, p, q, j):
        if p == "/api/network/scan":
            ok, out = helper("wifi-scan", timeout=30)
            return self.send(200 if ok else 503, out if ok else {"error": "Can't scan right now."})
        ok, out = helper("net-status")
        return self.send(200 if ok else 503, {**(out if ok else {}), "hostname": hostname()})

    def api_post_network(self, p, q, j):
        if p == "/api/network/hostname":
            h = str(j.get("hostname", "")).lower()
            if not HOST_RE.fullmatch(h):
                return self.send(400, {"error": "Use letters, numbers and dashes (up to 32)."})
            ok, out = helper("hostname", h)
            if ok:
                AUTH.publish()
                restart_soon()  # new hostname: the service restarts and `tls-cert` makes a cert for it
            return self.send(200 if ok else 500, {"ok": ok, "url": admin_url(h), "restarting": True} if ok else {"error": "Couldn't change it."})
        if p == "/api/network/join":
            ssid, pw = str(j.get("ssid", "")), j.get("password")
            if not 1 <= len(ssid.encode()) <= 32 or (pw is not None and not 8 <= len(str(pw)) <= 63):
                return self.send(400, {"error": "Check the network name and password (8 to 63 characters)."})
            ok, out = helper("wifi-join", stdin={"ssid": ssid, "password": pw, "hidden": bool(j.get("hidden"))}, timeout=60)
            return self.send(200 if ok else 400, {"ok": ok} if ok else {"error": out.get("error", "Couldn't join. The old network stays on.")})
        if p == "/api/network/forget":
            ok, out = helper("wifi-forget", stdin={"ssid": str(j.get("ssid", ""))[:32]})
            return self.send(200 if ok else 400, {"ok": ok} if ok else {"error": out.get("error", "Couldn't forget it.")})
        return self.send(404, {"error": "not found"})

    # wake words + enrollment + speakers
    def api_get_wakewords(self, p, q, j):
        return self.send(200, {"words": wakewords(), "speakers": [{"id": k, **v} for k, v in speakers().items()]})

    def api_put_wakewords(self, p, q, j):
        name = p.split("/")[3] if p.count("/") >= 3 else ""
        known = {w["name"]: w for w in wakewords()}
        if name not in known:
            return self.send(404, {"error": "No such wake word."})
        if known[name].get("backup"):
            if j.get("enabled") is False:
                return self.send(409, {"error": JARVIS_LOCKED})
            return self.send(400, {"error": "The default wake word has no settings to change."})
        ov = read_json(path("wake-overrides.json"), {})
        o = ov.get(name, {})
        if "enabled" in j:
            if not isinstance(j["enabled"], bool):
                return self.send(400, {"error": "bad value"})
            if j["enabled"] and not known[name]["has_model"]:
                return self.send(409, {"error": "Record and train this word first."})
            o["enabled"] = j["enabled"]
        if "sensitivity" in j:
            s = j["sensitivity"]
            if not isinstance(s, (int, float)) or not 0.05 <= s <= 0.95:
                return self.send(400, {"error": "Sensitivity is 5 to 95%."})
            o["threshold"] = round(1 - float(s), 3)
        ov[name] = o
        write_json(path("wake-overrides.json"), ov, 0o640)
        wake("POST", "/reload", {})
        return self.send(200, {"ok": True})

    def api_post_enroll(self, p, q, j):
        if p == "/api/enroll/check":
            err = phrase_problem(j.get("phrase", ""))
            if err:
                return self.send(400, {"error": err})
            mid, _words = normalize_phrase(j.get("phrase", ""))
            return self.send(200, {"ok": True, "id": mid, "label": phrase_label(mid)})
        raw, count = j.get("phrase", ""), j.get("count", 25)
        spk = str(j.get("speaker") or "")
        direct = str(raw or "").strip().lower()
        # hey_<bot id> can contain a hyphen or a digit; normalize_phrase would strip those.
        mid = direct if wake_name_ok(direct) else normalize_phrase(raw)[0]
        if isinstance(count, bool) or not isinstance(count, int) or not 20 <= count <= 30:
            return self.send(400, {"error": "Pick a wake word and 20 to 30 clips."})
        if not wake_name_ok(mid):
            return self.send(400, {"error": phrase_problem(raw) or "Pick a wake word and 20 to 30 clips."})
        if spk and spk not in speakers():
            return self.send(400, {"error": "Pick a speaker."})
        personas = phrase_personas()
        persona = j.get("persona")
        creating = mid not in personas
        if creating:
            err = phrase_problem(raw)
            if err:
                return self.send(400, {"error": err})
            if not isinstance(persona, str) or not persona_allowed(persona):
                return self.send(400, {"error": "Pick a bot."})
            save_phrase_persona(mid, persona)
            who = persona
        else:
            who = personas.get(mid) or "grok"
            if isinstance(persona, str) and persona and persona != who:
                return self.send(400, {"error": duplicate_msg(phrase_label(mid))})
        code, out = wake("POST", "/enroll", {"phrase": mid, "count": count, "topup": bool(j.get("topup")),
                                              "persona": who})
        if not isinstance(out, dict):
            out = {}
        if code != 202 and creating:
            drop_phrase_persona(mid)
        elif code == 202:
            set_enroll_state(mid, "recording", "Say it on the station.", who,
                             had_model=mid in enrolled_phrase_ids())
            if spk:  # speaker name -> opaque id stays on the Pi; uploads carry no name
                m = read_json(path("models.json"), {})
                e = m.setdefault(mid, {"speakers": []})
                if spk not in e["speakers"]:
                    e["speakers"].append(spk)
                e["last_session_at"] = int(now())
                write_json(path("models.json"), m, 0o600)
        elif not creating:
            set_enroll_state(mid, "failed", out.get("error") or "Couldn't start recording.", who)
        out = {**out, "id": mid}
        return self.send(code, out)

    def api_get_enroll(self, p, q, j):
        if p == "/api/enroll/status":
            doc = refresh_enroll_status()
            return self.send(200, {"words": doc, "buttons": buttons_for(doc)})
        code, st = wake("GET", "/status")
        return self.send(200, {"enrolling": st.get("enrolling") if code == 200 else None,
                               "last": st.get("last_enroll") if code == 200 else None})

    def api_post_speakers(self, p, q, j):
        name = " ".join(str(j.get("name", "")).split())[:32]
        if not name:
            return self.send(400, {"error": "Type a name."})
        sp = speakers()
        sid = "spk-" + secrets.token_hex(2)
        while sid in sp:
            sid = "spk-" + secrets.token_hex(2)
        sp[sid] = {"name": name, "guest": bool(j.get("guest")), "added_at": int(now())}
        write_json(path("speakers.json"), sp)
        return self.send(200, {"id": sid})

    def api_delete_speakers(self, p, q, j):
        sid = p.split("/")[3] if p.count("/") >= 3 else ""
        sp = speakers()
        if sid not in sp:
            return self.send(404, {"error": "not found"})
        sp.pop(sid)
        write_json(path("speakers.json"), sp)
        return self.send(200, {"ok": True})

    def api_get_personas(self, p, q, j):
        doc = read_json(PERSONAS, {})
        items = doc.get("personas", doc) if isinstance(doc, dict) else doc
        words = {w["persona"]: w["name"] for w in wakewords() if not w.get("backup")}
        out = []
        for x in (items if isinstance(items, list) else []):
            if isinstance(x, dict) and x.get("slug"):
                out.append({"slug": x["slug"], "name": x.get("name", x["slug"]), "voice": x.get("voice"),
                            "wake_word": words.get(x["slug"])})
        return self.send(200, {"personas": out, "editable": False,
                               "where": "The persona registry lives on the voice service computer; this page shows it read-only."})

    def api_get_privacy(self, p, q, j):
        """pii_mode defaults on. A missing relay config still answers 200 so the switch shows on."""
        drop_stale_guest()
        got = relay_call("GET", "/admin/policy")
        if not got or got[0] != 200 or not isinstance(got[1], dict) or not isinstance(got[1].get("pii_mode"), bool):
            body = {"pii_mode": True, "text": PII_TEXT}
            if got is None:
                body["message"] = RELAY_UNCONFIGURED
            elif not got or got[0] != 200:
                body["message"] = RELAY_DOWN
            return self.send(200, body)
        return self.send(200, {"pii_mode": got[1]["pii_mode"], "text": PII_TEXT})

    def api_put_privacy(self, p, q, j):
        if not isinstance(j.get("pii_mode"), bool):
            return self.send(400, {"error": "bad value"})
        if not self.need_password(j):  # before the relay, so a wrong password never leaves the station
            return
        got = relay_call("PUT", "/admin/policy", {"pii_mode": j["pii_mode"], "actor": RELAY_ACTOR})
        if got is None:
            return self.send(503, {"error": RELAY_UNCONFIGURED})
        status, data = got
        if status != 200 or data.get("ok") is False:
            return self.relay_fail(status, data, (j.get("password"),))
        audit("-", "pii_mode " + ("on" if j["pii_mode"] else "off"))
        return self.send(200, {"ok": True, "pii_mode": j["pii_mode"]})

    def api_get_portrait(self, p, q, j):
        parts = p.split("/")
        if len(parts) != 4 or not slug_ok(parts[3]):
            return self.send(404, {"error": "not found"})
        base = os.path.realpath(os.path.dirname(PERSONAS))
        for ext, ctype in (("png", "image/png"), ("jpg", "image/jpeg"), ("jpeg", "image/jpeg"), ("webp", "image/webp")):
            f = os.path.realpath(os.path.join(base, parts[3] + "." + ext))
            if not (f.startswith(base + os.sep) and os.path.isfile(f)):
                continue
            try:
                if os.path.getsize(f) > 2_000_000:
                    continue
                with open(f, "rb") as fh:
                    return self.send(200, body=fh.read(), ctype=ctype)
            except OSError:
                return self.send(404, {"error": "not found"})
        return self.send(404, {"error": "not found"})

    # -- bots (proxied to the relay; webhook keys are not stored here)
    def need_password(self, j, always=False):
        """Password check for a bot change. always=True never skips (remove bot, remove webhook, unpair).

        Otherwise an open step-up window is enough. A correct password opens or reopens the window.
        """
        token = self.cookie("gs_session")
        pw = j.get("password") if isinstance(j, dict) and "password" in j else None
        supplied = isinstance(pw, str) and pw != ""
        if not always and not supplied and AUTH.stepup_open(token):
            return True
        why = AUTH.reauth(pw if supplied else None)
        if why == "locked":
            self.send(429, {"error": "Too many tries. Wait 15 minutes.", "locked_s": AUTH.locked()})
            return False
        if why:
            self.send(403, {"error": "Password is wrong."})
            return False
        AUTH.open_stepup(token)
        return True

    def relay_fail(self, status, data, secrets=()):
        if not status or status >= 500:
            return self.send(503, {"error": RELAY_DOWN})
        code = status if status in (400, 401, 403, 404, 409, 429) else 502
        return self.send(code, {"error": self.shown_error(data, secrets)})

    def shown_error(self, data, secrets=()):
        """Relay error text safe for the browser: no key, URL, admin token, or voice token."""
        err = data.get("error") if isinstance(data, dict) else None
        extra = list(secrets)
        cfg = relay_settings()
        if cfg:
            extra.append(cfg["token"])
        if isinstance(data, dict):
            for k in ("key", "url", "token", "password", "voice_token"):
                v = data.get(k)
                if isinstance(v, str):
                    extra.append(v)
        return public_error(err, extra)

    def bots_unconfigured(self):
        return self.send(503, {"error": RELAY_UNCONFIGURED})

    def api_get_bots(self, p, q, j):
        m = BOT_PATH.fullmatch(p)
        if not m:
            return self.send(404, {"error": "not found"})
        slug, kind, tid = m.groups()
        if slug is None and kind is None:
            return self.bots_list()
        if slug and kind == "test" and tid and slug_ok(slug):
            return self.bots_test_get(slug, tid)
        return self.send(404, {"error": "not found"})

    def api_post_bots(self, p, q, j):
        if p == "/api/bots/refresh-avatars":
            return self.bots_refresh_avatars()
        am = re.fullmatch(r"/api/bots/([a-z][a-z0-9-]{1,23})/avatar-request", p)
        if am and slug_ok(am.group(1)):
            return self.bots_avatar_request(am.group(1))
        m = BOT_PATH.fullmatch(p)
        if not m:
            return self.send(404, {"error": "not found"})
        slug, kind, tid = m.groups()
        if slug is None and kind is None:
            return self.bots_add(j)
        if slug and kind == "test" and not tid and slug_ok(slug):
            return self.bots_test_start(slug, j)
        return self.send(404, {"error": "not found"})

    def api_patch_bots(self, p, q, j):
        m = BOT_PATH.fullmatch(p)
        if not m:
            return self.send(404, {"error": "not found"})
        slug, kind, tid = m.groups()
        if slug and kind is None and not tid and slug_ok(slug):
            return self.bots_rename(slug, j)
        return self.send(404, {"error": "not found"})

    def api_put_bots(self, p, q, j):
        m = BOT_PATH.fullmatch(p)
        if not m:
            return self.send(404, {"error": "not found"})
        slug, kind, tid = m.groups()
        if slug and kind == "webhook" and not tid and slug_ok(slug):
            return self.bots_webhook_put(slug, j)
        return self.send(404, {"error": "not found"})

    def api_delete_bots(self, p, q, j):
        m = BOT_PATH.fullmatch(p)
        if not m:
            return self.send(404, {"error": "not found"})
        slug, kind, tid = m.groups()
        if not slug or not slug_ok(slug):
            return self.send(404, {"error": "not found"})
        if kind is None and not tid:
            return self.bots_remove(slug, j)
        if kind == "webhook" and not tid:
            return self.bots_webhook_delete(slug, j)
        if kind == "test" and tid:
            return self.bots_test_delete(slug, tid)
        return self.send(404, {"error": "not found"})

    def _stepup(self):
        return AUTH.stepup_until(self.cookie("gs_session"))

    def bots_list(self):
        drop_stale_guest()
        got = relay_call("GET", "/admin/bots")
        if got is None:
            return self.send(200, {"bots": local_bots(), "voices": [], "message": RELAY_UNCONFIGURED,
                                   "stepup_until": self._stepup()})
        status, data = got
        if status != 200:
            return self.relay_fail(status, data)
        bots = [b for b in (safe_bot(x) for x in (data.get("bots") or [])) if b]
        return self.send(200, {"bots": bots, "voices": safe_voices(data.get("voices")), "stepup_until": self._stepup()})

    def bots_refresh_avatars(self):
        """Ask the wake listener to re-sync personas and portraits. No password."""
        code, data = wake("POST", "/personas/sync", {})
        if code == 202 and isinstance(data, dict):
            if data.get("running") is True:
                return self.send(202, {"running": True})
            if data.get("started") is True:
                return self.send(202, {"started": True})
        return self.send(503, {"error": "Can't reach the voice listener right now."})

    def post_avatar(self, slug):
        """Portrait upload: raw image, step-up via X-GS-Password (or the open window). Proxied, not stored here."""
        if not slug_ok(slug):
            return self.send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send(400, {"error": AVATAR_ERROR})
        if n <= 0 or n > AVATAR_MAX:
            return self.send(400, {"error": AVATAR_ERROR})
        data = self.rfile.read(n)
        if len(data) != n:
            return self.send(400, {"error": AVATAR_ERROR})
        send_as = sniff_image(self.headers.get("Content-Type", ""), data)
        if not send_as:
            return self.send(400, {"error": AVATAR_ERROR})
        pw = self.headers.get("X-GS-Password")
        if not self.need_password({"password": pw} if isinstance(pw, str) and pw != "" else {}):
            return
        got = relay_call("POST", "/admin/bots/" + slug + "/avatar", raw=data, content_type=send_as)
        if got is None:
            return self.bots_unconfigured()
        status, body = got
        if status not in (200, 201) or (isinstance(body, dict) and body.get("ok") is False):
            return self.relay_fail(status, body, (pw if isinstance(pw, str) else "",))
        audit(slug, "avatar")
        return self.send(200, {"ok": True})

    def bots_avatar_request(self, slug):
        """Ask the relay to redraw this bot's portrait. No password. Sync the station copy ~30 s later."""
        got = relay_call("POST", "/admin/bots/" + slug + "/avatar-request", {"actor": RELAY_ACTOR})
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status not in (200, 202) or (isinstance(data, dict) and data.get("ok") is False):
            return self.relay_fail(status, data)
        schedule_persona_sync(30)
        return self.send(202, {"ok": True})

    def bots_add(self, j):
        slug, name, voice = j.get("slug"), clean_name(j.get("name")), j.get("voice")
        problem = bot_id_problem(slug)
        if problem:
            return self.send(400, {"error": problem})
        if not name:
            return self.send(400, {"error": "Type a name."})
        if not voice_ok(voice):
            return self.send(400, {"error": "Pick a voice."})
        if not self.need_password(j):
            return
        got = relay_call("POST", "/admin/bots", {"slug": slug, "name": name, "voice": voice, "actor": RELAY_ACTOR})
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        tok = data.get("voice_token")
        if status not in (200, 201) or data.get("ok") is False or not voice_token_ok(tok):
            if status in (200, 201):
                return self.send(502, {"error": "The voice service didn't return a voice token."})
            return self.relay_fail(status, data, (j.get("password"),))
        audit(slug, "add")
        remember_bot_wakeword(slug)
        return self.send(201, {"ok": True, "slug": slug, "voice_token": tok})

    def bots_rename(self, slug, j):
        """Rename, change voice, or both. Voice is optional when a name is sent, and the name is optional when a voice is."""
        if not isinstance(j, dict):
            return self.send(400, {"error": "Type a name."})
        has_name, has_voice = "name" in j, "voice" in j
        if not has_name and not has_voice:
            return self.send(400, {"error": "Type a name."})
        name = clean_name(j.get("name")) if has_name else None
        if has_name and not name:
            return self.send(400, {"error": "Type a name."})
        voice = j.get("voice") if has_voice else None
        if has_voice and not voice_ok(voice):
            return self.send(400, {"error": "Pick a voice."})
        if not self.need_password(j):
            return
        body = {"actor": RELAY_ACTOR}
        if has_name:
            body["name"] = name
        if has_voice:
            body["voice"] = voice
        got = relay_call("PATCH", "/admin/bots/" + slug, body)
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200 or data.get("ok") is False:
            return self.relay_fail(status, data, (j.get("password"),))
        if has_name:
            audit(slug, "rename")
        if has_voice:
            audit(slug, "voice")
        return self.send(200, {"ok": True})

    def bots_remove(self, slug, j):
        if not self.need_password(j, always=True):
            return
        got = relay_call("DELETE", "/admin/bots/" + slug, {"actor": RELAY_ACTOR})
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200 or data.get("ok") is False:
            return self.relay_fail(status, data, (j.get("password"),))
        audit(slug, "remove")
        return self.send(200, {"ok": True})

    def bots_webhook_put(self, slug, j):
        url, key = j.get("url"), j.get("key")
        if not webhook_url_ok(url):
            return self.send(400, {"error": "That webhook address isn't allowed."})
        if not webhook_key_ok(key):
            return self.send(400, {"error": "That webhook key isn't valid."})
        if not self.need_password(j):
            return
        got = relay_call("PUT", "/admin/bots/" + slug + "/webhook", {"url": url, "key": key, "actor": RELAY_ACTOR})
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200 or data.get("ok") is False:
            return self.relay_fail(status, data, (key, url, j.get("password")))
        last4 = key[-4:]
        audit(slug, "webhook-save", last4)
        return self.send(200, {"ok": True, "last4": last4})

    def bots_webhook_delete(self, slug, j):
        if not self.need_password(j, always=True):
            return
        got = relay_call("DELETE", "/admin/bots/" + slug + "/webhook", {"actor": RELAY_ACTOR})
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200 or data.get("ok") is False:
            return self.relay_fail(status, data, (j.get("password"),))
        audit(slug, "webhook-remove")
        return self.send(200, {"ok": True})

    def bots_test_start(self, slug, j):
        body = {}
        url, key = j.get("url"), j.get("key")
        if url not in (None, ""):
            if not webhook_url_ok(url):
                return self.send(400, {"error": "That webhook address isn't allowed."})
            body["url"] = url
        if key not in (None, ""):
            if not webhook_key_ok(key):
                return self.send(400, {"error": "That webhook key isn't valid."})
            body["key"] = key
        got = relay_call("POST", "/admin/bots/" + slug + "/test", body)
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        tid = data.get("test_id")
        if status != 202 or not (isinstance(tid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", tid)):
            if status == 202:
                return self.send(502, {"error": "The voice service didn't start the test."})
            return self.relay_fail(status, data, (key if isinstance(key, str) else "", url if isinstance(url, str) else "",
                                       j.get("password") if isinstance(j.get("password"), str) else ""))
        return self.send(202, {"test_id": tid})

    def bots_test_get(self, slug, tid):
        got = relay_call("GET", "/admin/bots/" + slug + "/test/" + tid)
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200:
            return self.relay_fail(status, data)
        state = data.get("state")
        if state not in ("waiting", "ok", "failed", "cancelled"):
            state = "failed"
        err = data.get("error")
        elapsed = _num(data.get("elapsed_s"))
        return self.send(200, {
            "state": state,
            "elapsed_s": 0.0 if elapsed is None else elapsed,
            "answered_in_s": _num(data.get("answered_in_s")),
            "error": None if err in (None, "") else self.shown_error(data),
        })

    def bots_test_delete(self, slug, tid):
        got = relay_call("DELETE", "/admin/bots/" + slug + "/test/" + tid, {})
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200 or data.get("ok") is False:
            return self.relay_fail(status, data)
        return self.send(200, {"ok": True})

    # system
    def api_get_system(self, p, q, j):
        if p == "/api/system/logs":
            unit = q.get("part", "grok-station-wake")
            since = LOG_SINCE.get(q.get("since", "1h"))
            if unit not in UNITS or not since:
                return self.send(400, {"error": "bad filter"})
            try:
                out = subprocess.run(["journalctl", "-u", unit, "--since", since, "-n", "500", "-o", "short-iso", "--no-pager"],
                                     capture_output=True, timeout=10).stdout.decode(errors="replace")
            except (OSError, subprocess.TimeoutExpired):
                out = ""
            if q.get("download") == "1":
                return self.send(200, body=out.encode(), ctype="text/plain; charset=utf-8",
                                 headers=[("Content-Disposition", f'attachment; filename="{unit}.log"')])
            return self.send(200, {"lines": out.splitlines()[-500:]})
        if p == "/api/system/update":
            return self.send(404, {"error": "not found"})
        if p != "/api/system":
            return self.send(404, {"error": "not found"})
        return self.send(200, {"version": version(), "health": health(), "services": services(), "network": net_summary(),
                               "tls": {**tls_info(), "url": admin_url()}})

    def api_post_system(self, p, q, j):
        if p == "/api/system/restart-voice":
            if busy():
                return self.send(409, {"error": "A conversation or enrollment is running. Try again when it ends."})
            ok, _ = helper("restart", "wake")
            return self.send(200 if ok else 500, {"ok": ok})
        if p == "/api/system/reboot":
            if j.get("confirm") is not True:
                return self.send(400, {"error": "Confirm the reboot."})
            if busy() and not j.get("force"):
                return self.send(409, {"error": "A conversation or enrollment is running.", "can_force": True})
            ok, _ = helper("reboot")
            return self.send(202 if ok else 500, {"ok": ok})
        return self.send(404, {"error": "not found"})

    # -- voice relay pairing
    def api_get_relay(self, p, q, j):
        if p != "/api/relay":
            return self.send(404, {"error": "not found"})
        if PAIR_LISTENER is not None:
            sync_pair_port()
        status = fetch_relay_status()
        return self.send(200, {
            "state": relay_state_name(status),
            "pairing": pairing_public(),
            "relay": status,
            "audit": recent_audit(20),
        })

    def api_post_relay(self, p, q, j):
        if p == "/api/relay/pair/start":
            return self._pair_start(j)
        if p == "/api/relay/pair/new-code":
            return self._pair_new_code(j)
        if p == "/api/relay/pair/cancel":
            return self._pair_cancel(j)
        if p == "/api/relay/pair/confirm":
            return self._pair_confirm(j)
        if p == "/api/relay/unpair":
            return self._unpair(j)
        return self.send(404, {"error": "not found"})

    def _pair_start(self, j):
        if not self.need_password(j, always=True):
            return
        try:
            ensure_pair()
        except pairing.PairingError:
            return self.send(503, {"error": "The station certificate isn't ready."})
        code = PAIR_WINDOW.open(now())
        if not code:
            return self.send(409, {"error": "Pairing is already open."})
        sync_pair_port()
        return self.send(200, {
            "state": "pairing",
            "pairing": pairing_public(code),
            "relay": None,
            "audit": recent_audit(20),
        })

    def _pair_new_code(self, j):
        if not self.need_password(j, always=False):
            return
        if PAIR_WINDOW is None:
            return self.send(409, {"error": "Pairing isn't open."})
        code = PAIR_WINDOW.new_code()
        if not code:
            return self.send(409, {"error": "Pairing isn't open."})
        return self.send(200, {"state": "pairing", "pairing": pairing_public(code), "audit": recent_audit(20)})

    def _pair_cancel(self, j):
        if not self.need_password(j, always=False):
            return
        if PAIR_WINDOW is None or not PAIR_WINDOW.cancel():
            return self.send(409, {"error": "Pairing isn't open."})
        sync_pair_port()
        return self.send(200, {"ok": True})

    def _pair_confirm(self, j):
        if not self.need_password(j, always=False):
            return
        if PAIR_WINDOW is None or PAIR_CA is None or PAIR_LISTENER is None:
            return self.send(409, {"error": "Nothing to confirm."})
        snap = PAIR_WINDOW.snapshot()
        shown = snap.get("relay_fingerprint")
        hexfp = "".join(shown.split()).lower() if isinstance(shown, str) else ""
        if not re.fullmatch(r"[0-9a-f]{64}", hexfp):
            hexfp = ""
        pending_id = PAIR_WINDOW._pending_id
        with PAIR_LISTENER.lock:
            issued = PAIR_WINDOW.confirm_match()
        if not issued:
            return self.send(409, {"error": "Nothing to confirm."})
        cert, _serial8 = issued
        der = cert.public_bytes(serialization.Encoding.DER)
        match = re.search(rb"relay-([0-9a-f]{16})", der)
        pid = pending_id if isinstance(pending_id, str) and re.fullmatch(r"[0-9a-f]{16}", pending_id) else ""
        if not pid and match:
            pid = match.group(1).decode("ascii")
        if pid and PAIR_ISSUANCE is not None:
            cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
            ca_pem = PAIR_CA.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
            PAIR_ISSUANCE.put(pid, cert_pem, ca_pem)
        expiry = cert.not_valid_after_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            key, local = PAIR_CA.issue_local_client()
            save_paired(format(cert.serial_number, "x"), expiry, hexfp, key, local, PAIR_CA.cert)
        except OSError:
            sync_pair_port()
            return self.send(500, {"error": "The certificate was signed but could not be stored."})
        sync_pair_port()
        return self.send(200, {"ok": True})

    def _unpair(self, j):
        if not self.need_password(j, always=True):
            return
        rec = _paired_record()
        if not isinstance(rec, dict) or not rec.get("serial"):
            return self.send(409, {"error": "No relay is paired."})
        try:
            load_ca().revoke(rec["serial"])
        except pairing.PairingError:
            return self.send(409, {"error": "No relay is paired."})
        drop_paired_files()
        if PAIR_WINDOW is not None and PAIR_WINDOW.phase in ("open", "pending"):
            PAIR_WINDOW.cancel()
            sync_pair_port()
        return self.send(200, {"ok": True})

    # -- updates
    def api_get_updates(self, p, q, j):
        if p != "/api/updates":
            return self.send(404, {"error": "not found"})
        return self.send(200, updates_view())

    def api_post_updates(self, p, q, j):
        if p == "/api/updates/check":
            if not self.need_password(j, always=False):
                return
            run_update_check()
            return self.send(200, updates_view())
        if p == "/api/updates/apply":
            if not self.need_password(j, always=False):
                return
            if not isinstance(j, dict) or not set(j) <= {"version", "password"} or "version" not in j:
                _audit_update("update-rejected", "bad-args")
                return self.send(400, {"error": "The update request isn't valid."})
            version_s = j.get("version")
            if not isinstance(version_s, str) or not _VERSION_RE.fullmatch(version_s):
                _audit_update("update-rejected", "bad-args")
                return self.send(400, {"error": "The update request isn't valid."})
            try:
                code, body = run_apply(version_s)
            except release.ManifestError:
                _audit_update("update-check-failed", "fetch-failed")
                return self.send(503, {"error": "Updates can't be checked right now."})
            return self.send(code, body)
        return self.send(404, {"error": "not found"})

    def api_put_updates(self, p, q, j):
        if p != "/api/updates/autocheck":
            return self.send(404, {"error": "not found"})
        if not self.need_password(j, always=False):
            return
        on = j.get("on") if isinstance(j, dict) else None
        if not isinstance(on, bool):
            return self.send(400, {"error": "Say whether checking is on."})
        try:
            release.set_autocheck(_settings_path(), on)
        except (OSError, ValueError):
            return self.send(500, {"error": "Couldn't save that."})
        _audit_update("update-autocheck", "on" if on else "off")
        return self.send(200, {"ok": True, "autocheck": on})


def services():
    out = {}
    for u in UNITS:
        try:
            out[u] = subprocess.run(["systemctl", "is-active", u], capture_output=True, timeout=5).stdout.decode().strip()
        except (OSError, subprocess.TimeoutExpired):
            out[u] = "unknown"
    return out


def health():
    h = {}
    try:
        h["cpu_c"] = round(int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000, 1)
    except (OSError, ValueError):
        h["cpu_c"] = None
    try:
        h["uptime_s"] = int(float(open("/proc/uptime").read().split()[0]))
    except (OSError, ValueError):
        h["uptime_s"] = None
    try:
        mem = dict(l.split(":", 1) for l in open("/proc/meminfo").read().splitlines() if ":" in l)
        tot, av = int(mem["MemTotal"].split()[0]), int(mem["MemAvailable"].split()[0])
        h["mem_used_pct"] = round(100 * (tot - av) / tot)
    except (OSError, KeyError, ValueError):
        h["mem_used_pct"] = None
    try:
        du = shutil.disk_usage("/")
        h["disk_free_gb"] = round(du.free / 1e9, 1)
    except OSError:
        h["disk_free_gb"] = None
    return h


def net_summary():
    ok, out = helper("net-status")
    return out if ok else None


# ------------------------------------------------------------------ hotspot fallback
def hotspot_loop():
    """No network at boot (no wired, no Wi-Fi, no saved network in range): start the station's own hotspot so it can
    be set up. Never started while any connection is up; stops as soon as a saved network is reachable."""
    time.sleep(float(E("ADMIN_HOTSPOT_GRACE", "90")))
    while True:
        try:
            ok, st = helper("net-status")
            hs = os.path.exists(os.path.join(RUN_DIR, "hotspot.json"))
            if ok and not hs and not st.get("connected") and not st.get("saved_in_range"):
                ssid = "grok-station-" + secrets.token_hex(2).upper()
                pw = "hearth-" + "".join(secrets.choice("23456789") for _ in range(4))
                ok2, out = helper("hotspot-start", stdin={"ssid": ssid, "password": pw}, timeout=40)
                if ok2:
                    write_json(os.path.join(RUN_DIR, "hotspot.json"), {"ssid": ssid, "password": pw, "url": "https://10.42.0.1",
                                                                      "qr_join": qr(f"WIFI:T:WPA;S:{ssid};P:{pw};;"),
                                                                      "qr_open": qr("https://10.42.0.1")}, 0o640)
                    print("hotspot started (no network)", flush=True)
            elif ok and hs and (st.get("saved_in_range") or st.get("wired") or st.get("wifi")):
                helper("hotspot-stop", timeout=40)
                os.unlink(os.path.join(RUN_DIR, "hotspot.json"))
                print("hotspot stopped (network available)", flush=True)
            AUTH.publish()
        except Exception as e:
            print(f"hotspot check failed: {type(e).__name__}", flush=True)
        time.sleep(60)


def qr(text):
    """QR matrix (list of 0/1 strings) for the station screen; None if the qrcode module is missing."""
    try:
        import qrcode
    except ImportError:
        return None
    q = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(text)
    q.make(fit=True)
    return ["".join("1" if c else "0" for c in row) for row in q.get_matrix()]


EXIT = os._exit  # tests replace it


def restart_soon(delay=2.0):
    """Exit after the reply is out; systemd restarts the service and ExecStartPre remakes the cert."""
    threading.Timer(delay, lambda: EXIT(3)).start()


def tls_context():
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(os.path.join(TLS_DIR, "cert.pem"), os.path.join(TLS_DIR, "key.pem"))  # no cert: fail, never plain HTTP
    return ctx


class TLSServer(ThreadingHTTPServer):
    """Handshake in the request thread (never in the accept loop), with a timeout, so a slow client blocks nobody."""
    daemon_threads = True

    def finish_request(self, request, client_address):
        try:
            request.settimeout(10)
            request.do_handshake()
            request.settimeout(60)  # idle keep-alive connections close after a minute
        except Exception:
            return
        super().finish_request(request, client_address)


class Redirect(BaseHTTPRequestHandler):
    """Port 80: a 301 to the same path on https, no app content, no cookies."""
    server_version = "grok-station-admin"
    protocol_version = "HTTP/1.1"

    def go(self):
        host = (self.headers.get("Host") or "").rsplit("]", 1)[-1].split(":")[0].strip().lower()
        own = (hostname(), hostname() + ".local")
        if not (host in own or re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host)):  # only our own names or an IP literal
            host = hostname() + ".local"
        path = self.path if self.path.startswith("/") and not self.path.startswith("//") else "/"
        path = re.sub(r"[^A-Za-z0-9._~/?=&%+-]", "", path)[:512] or "/"
        self.send_response(301)
        self.send_header("Location", f"https://{host}{'' if PORT == 443 else f':{PORT}'}{path}")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = go

    def log_message(self, fmt, *a):
        pass


def publish_tls():
    """/run/grok-station-admin/tls.json for the station screen (shows a new cert's fingerprint for a while)."""
    t = tls_info()
    if not t["fingerprint"]:
        return
    try:
        made = int(os.stat(os.path.join(TLS_DIR, "cert.pem")).st_mtime)
    except OSError:
        made = 0
    write_json(os.path.join(RUN_DIR, "tls.json"), {"fingerprint": t["fingerprint"], "url": admin_url(), "made_at": made}, 0o640)


def main():
    os.makedirs(STATE_DIR, mode=0o750, exist_ok=True)
    drop_stale_guest()
    publish_tls()
    AUTH.publish()
    print(f"admin on :{PORT} setup_required={AUTH.setup_required()} mode={AUTH.mode()}", flush=True)
    threading.Thread(target=hotspot_loop, daemon=True).start()

    def republish():
        while True:
            time.sleep(5)
            try:
                with LOCK:
                    AUTH.publish()
            except Exception:
                pass
            if tls_info()["host"] not in ("", hostname()):  # renamed outside the page: restart for a matching cert
                print("hostname changed: restarting for a new cert", flush=True)
                EXIT(3)
    threading.Thread(target=republish, daemon=True).start()
    ThreadingHTTPServer.daemon_threads = True
    if HTTP_PORT:
        threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Redirect).serve_forever, daemon=True).start()
    ctx = tls_context()
    srv = TLSServer(("0.0.0.0", PORT), Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    print(f"admin https on :{PORT} (sha256 {tls_info()['fingerprint']}), http :{HTTP_PORT} redirects only", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
