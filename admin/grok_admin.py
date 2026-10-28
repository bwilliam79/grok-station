#!/usr/bin/env python3
"""grok-station admin web page (LAN only), per Noor's ADMIN-UI.md.

No default password: until an admin password is set, the station shows a one-time 6-character setup code (3+3, no
0/O/1/I), new on each boot until used, and voice stays off. Password >= 10 characters, stored as a scrypt hash.
Secrets are write-only (last 4 shown). 5 wrong tries (code or password) lock sign-in for 15 min. Privileged changes go
through grok-station-helper (root, fixed subcommands, validated input) via sudo. A correct admin password also opens a grace window on that session (password_grace_min, default 10 minutes;
0 always asks) for saving a webhook, adding, renaming, or removing a bot, changing a bot's voice, uploading a
portrait, revoking a key, and changing the privacy toggle. Changing the grace period always asks again. Logging
out ends the window. Stdlib only.
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
_APP_DIR = os.path.join(os.path.dirname(HERE), "app")
if os.path.isdir(_APP_DIR) and _APP_DIR not in sys.path:
    sys.path.append(_APP_DIR)  # repo checkout: app/dimming.py
_PARENT = os.path.dirname(HERE)
if os.path.isfile(os.path.join(_PARENT, "dimming.py")) and _PARENT not in sys.path:
    sys.path.append(_PARENT)  # installed: /opt/grok-station/dimming.py
import dimming
WAKE_CTL = E("ADMIN_WAKE_CTL", "http://127.0.0.1:8789")
WAKE_CONFIG = E("WAKE_CONFIG", "/opt/grok-station/wake/wakewords.json")
ENROLL_REPS = 5  # clips per training session, not configurable
WAKE_DROP_DIR = E("WAKE_DROP_DIR", "/var/lib/grok-station/wakewords")
ENROLLED = E("WAKE_ENROLLED_DIR", "/opt/grok-station/wake/models/enrolled") + "/wakewords.enrolled.json"
ENROLL_INBOX = E("WAKE_ENROLL_DIR", "/var/lib/grok-station/enroll/inbox")
PERSONAS = E("ADMIN_PERSONAS", "/opt/grok-station/portraits/personas.json")
SNAPSHOT = E("GROK_STATION_SNAPSHOT", "/run/grok-station/last.png")
VERSION_FILE = E("ADMIN_VERSION_FILE", "/opt/grok-station/VERSION")
REPO = "bwilliam79/grok-station"
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O, 1/I
MIN_PASSWORD = 10
MAX_FAILS, FAIL_WINDOW, LOCK_FOR = 5, 900, 900
SESSION_TTL, SETUP_TTL, IDLE_TTL = 12 * 3600, 900, 30 * 60  # idle: 30 min with no request; absolute cap stays 12 h
STEPUP_TTL = 600  # default grace (password_grace_min=10); the open window uses the setting, and later requests do not move it
GRACE_MAX = 120
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
BUILTIN_WORD = "hey_grok"
BUILTIN_LOCKED = "Hey Grok is the station's built-in wake word and can't be turned off"
UNITS = ("grok-station", "grok-station-wake", "grok-station-admin")
LOG_SINCE = {"15m": "-15min", "1h": "-1h", "6h": "-6h", "24h": "-24h", "7d": "-7d"}
LOCK = threading.RLock()


# The one message for any password failure (login, re-check). No prefix in the UI.
PASSWORD_BAD = "Password incorrect."

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


_SESSION_KEY = re.compile(r"^[0-9a-f]{64}$")


def _is_num(v):
    """A JSON number, not a bool (bool is a subclass of int)."""
    return not isinstance(v, bool) and isinstance(v, (int, float))


class Auth:
    """Password record, setup code, sessions, global lockout."""

    def __init__(self):
        self.sessions, self.setup_tickets, self.fails, self.locked_until = {}, {}, [], 0.0
        self.code = None
        self._load_sessions()

    def _load_sessions(self):
        """Restore sha256 session keys after a restart (the update restarts this service).

        Raw tokens are never stored. A missing password (setup or reset) drops every session,
        including one left on disk, so an old cookie cannot sign in after the password is set again.
        """
        if self.setup_required():
            self.sessions = {}
            self._load_note = ["setup"]
            try:
                os.unlink(path("sessions.json"))
            except OSError:
                pass
            return
        raw = read_json(path("sessions.json"), {})
        if not isinstance(raw, dict):
            return
        t = now()
        kept = {}
        self._load_note = [type(raw).__name__, len(raw)]
        for key, rec in raw.items():
            if not isinstance(key, str) or _SESSION_KEY.fullmatch(key) is None or not isinstance(rec, dict):
                self._load_note.append("skip-key")
                continue
            exp, seen = rec.get("exp"), rec.get("seen")
            if not _is_num(exp) or not _is_num(seen):
                self._load_note.append(("skip-num", type(exp).__name__, type(seen).__name__, _is_num(exp), _is_num(seen),
                                        isinstance(exp, bool), isinstance(seen, bool)))
                continue
            if exp < t or t - seen > IDLE_TTL:
                self._load_note.append("skip-stale")
                continue
            item = {"exp": float(exp), "seen": float(seen)}
            until = rec.get("stepup_until")
            if _is_num(until):
                item["stepup_until"] = float(until)
            kept[key] = item
        self.sessions = kept
        self._load_note.append(len(kept))

    def _save_sessions(self):
        try:
            write_json(path("sessions.json"), self.sessions)
        except OSError:
            pass

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
            if not xai_key_set():  # password done, key still missing: first-run screen asks for the key, voice off
                doc = {"mode": "key", "host": hostname() + ".local", "url": admin_url(),
                       "ip": lan_ip()}
                if doc != read_json(f, {}):
                    write_json(f, doc, 0o640)
                return
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
               "ip": lan_ip()}
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
            self.sessions.clear()  # a reset must not revive the cookie that was signed in before it
            self._save_sessions()
            self.code = None
            self.publish()
            return None

    def login(self, pw):
        """Password only (no username). An older auth.json with a username keeps working: only the hash is read."""
        with LOCK:
            if self.locked() or self.setup_required():
                return None
            rec = self.rec
        ok = check_password(str(pw or ""), rec)
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
        self._save_sessions()
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
            if k in self.sessions:
                self.sessions.pop(k, None)
                self._save_sessions()  # an expired cookie must not come back when the service restarts
            return False
        rec["seen"] = t  # idle window slides on each authenticated request; step-up does not
        self._save_sessions()
        return True

    def stepup_until(self, token):
        """Epoch when this session's grace window closes, or None. Does not slide or refresh it.

        A grace setting of 0 means there is never a window, even if a timestamp is still stored.
        """
        if password_grace_min() <= 0 or not token or self.setup_required():
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
        """Start a fresh grace window on this session. Caller already checked the password.

        The length is password_grace_min at this moment. Zero does not open a window.
        """
        ttl = password_grace_min() * 60
        if ttl <= 0 or not token or self.setup_required():
            return None
        rec = self.sessions.get(self._key(token))
        t = now()
        if not self._fresh(rec, t):
            return None
        rec["stepup_until"] = t + ttl
        self._save_sessions()
        audit_stepup()
        return rec["stepup_until"]

    def clear_stepups(self):
        """Drop every open grace window. Used when the grace setting is lowered."""
        for rec in self.sessions.values():
            if isinstance(rec, dict):
                rec.pop("stepup_until", None)
        self._save_sessions()

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
        if not token:
            return
        key = hashlib.sha256(token.encode()).hexdigest()
        if key in self.sessions:
            self.sessions.pop(key, None)
            self._save_sessions()

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
            self._save_sessions()
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


def station_sounds():
    """Catalog module. Next to this checkout, else the installed copy. None if it isn't there."""
    import importlib.util
    cached = getattr(station_sounds, "_mod", None)
    if cached:
        return cached
    cands = []
    if os.environ.get("GROK_STATION_SOUNDS_PY"):
        cands.append(os.environ["GROK_STATION_SOUNDS_PY"])
    here = globals().get("__file__")
    if here:
        cands.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(here))), "sounds.py"))
    cands.append("/opt/grok-station/sounds.py")
    for p in cands:
        if not p or not os.path.isfile(p):
            continue
        spec = importlib.util.spec_from_file_location("grok_station_sounds", p)
        if not spec or not spec.loader:
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            continue
        station_sounds._mod = mod
        return mod
    return None


def _env_bool(name, default):
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "off", "no")


def mic_reset_path():
    return os.environ.get("WAKE_MIC_RESET_FILE") or path("mic-reset.json")


def wait_tone_path():
    """Same file the wake listener reads. Default on when it is missing."""
    return os.environ.get("WAIT_TONE_FILE") or path("wait-tone.json")


def wait_tone_on():
    data = read_json(wait_tone_path(), {})
    if isinstance(data, dict) and isinstance(data.get("on"), bool):
        return data["on"]
    return _env_bool("WAIT_TONE", True)


def save_wait_tone(body):
    """Store the switch. None when the body is not a bool `on`. Does not restart the listener."""
    if not isinstance(body, dict) or not isinstance(body.get("on"), bool):
        return None
    write_json(wait_tone_path(), {"on": body["on"]}, 0o640)
    return body["on"]


def mic_reset_public():
    """The three microphone-reset switches. A bool in the file wins over the env. Default on."""
    data = read_json(mic_reset_path(), {})
    if not isinstance(data, dict):
        data = {}
    out = {}
    for key in ("button", "night", "drought"):
        if isinstance(data.get(key), bool):
            out[key] = data[key]
        else:
            out[key] = _env_bool("WAKE_MIC_RESET_" + key.upper(), True)
    return out


def save_mic_reset(updates):
    """Merge bool switches and write them group-readable for the listener. None if the body is bad."""
    if not isinstance(updates, dict) or not updates:
        return None
    cur = mic_reset_public()
    for key, val in updates.items():
        if key not in ("button", "night", "drought") or not isinstance(val, bool):
            return None
        cur[key] = val
    write_json(mic_reset_path(), cur, 0o640)
    return cur


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


# ------------------------------------------------------------------ wake words
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
    Every other new entry, including a bot added later, starts off. hey_grok is the built-in word
    and is always on. Returns (overrides, changed).
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


def _phrase_has_model(name, cfg=None):
    """True when this phrase has a trained model (enrolled list or an onnx file)."""
    if name in enrolled_entries():
        return True
    if not isinstance(cfg, dict):
        cfg = read_json(WAKE_CONFIG, {})
    models = cfg.get("models_dir") if isinstance(cfg, dict) else ""
    if not isinstance(models, str) or not models:
        return False
    return os.path.isfile(os.path.join(models, name + ".onnx"))


def _parked_phrase(row):
    return isinstance(row, dict) and (
        row.get("hidden") or row.get("forgotten") or row.get("unused") in ("deleted", "unlinked"))


def remember_bot_wakeword(slug):
    """A bot just added gets hey_<slug>, off, unless that entry already exists.

    A parked word for this same slug (or named hey_<slug>) is reclaimed: it leaves Unused.
    A model turns it on. No model stays off. An entry that is already active is left alone.
    """
    if not slug_ok(slug):
        return
    name = "hey_" + slug
    cfg = read_json(WAKE_CONFIG, {})
    ov = read_json(path("wake-overrides.json"), {})
    if not isinstance(ov, dict):
        ov = {}
    changed = False
    for n, o in list(ov.items()):
        if n == BUILTIN_WORD or not isinstance(o, dict):
            continue
        persona = str(o.get("persona") or "")
        if persona != slug and n != name:
            continue
        if not _parked_phrase(o):
            continue
        row = dict(o)
        row.pop("unused", None)
        row.pop("hidden", None)
        row.pop("forgotten", None)
        if not persona_ok(str(row.get("persona") or "")):
            row["persona"] = slug
        if _phrase_has_model(n, cfg):
            row["enabled"] = True
        elif "enabled" not in row:
            row["enabled"] = False
        ov[n] = row
        changed = True
    if name not in configured_phrase_names(cfg) and not isinstance(ov.get(name), dict):
        ov[name] = {"enabled": False, "persona": slug}
        changed = True
    if changed:
        write_json(path("wake-overrides.json"), ov, 0o640)


def bot_roster():
    """(bots, authoritative). Authoritative only for a relay 200, or a non-empty local cache.

    An empty local fallback is not a list of deleted bots. bots are {slug, name}.
    """
    got = relay_call("GET", "/admin/bots")
    if got and got[0] == 200 and isinstance(got[1], dict):
        out = []
        for b in got[1].get("bots") or []:
            safe = safe_bot(b)
            if safe:
                out.append({"slug": safe["slug"], "name": safe["name"]})
        return out, True
    local = [{"slug": b["slug"], "name": b["name"]} for b in local_bots()]
    if local:
        return local, True
    return [], False


def known_bots():
    """Bots the wake-word list should mirror: the relay list, or the station's cached personas."""
    bots, _auth = bot_roster()
    return bots


def owner_absent(persona, slugs=None, authoritative=None):
    """True when this persona is a bot and the bot list says it is gone.

    False for Grok, a bad id, and whenever the list is not authoritative.
    """
    if not persona or persona == "grok" or not persona_ok(persona):
        return False
    if slugs is None or authoritative is None:
        bots, authoritative = bot_roster()
        slugs = {b["slug"] for b in bots}
    if not authoritative:
        return False
    return persona not in slugs


def _stored_phrase_persona(name, ov, enrolled, status):
    """Persona saved for this phrase, "" if the record has none, None if nothing is stored.

    A shipped wake word with no override, enrolled entry, or enroll-status row is None.
    """
    sources = []
    o = ov.get(name) if isinstance(ov, dict) else None
    if isinstance(o, dict):
        sources.append(o)
    ent = enrolled.get(name) if isinstance(enrolled, dict) else None
    if isinstance(ent, dict):
        sources.append(ent)
    st = status.get(name) if isinstance(status, dict) else None
    if isinstance(st, dict):
        sources.append(st)
    if not sources:
        return None
    for src in sources:
        who = str(src.get("persona") or "")
        if persona_ok(who):
            return who
    return ""


def _orphan_names(ov, enrolled, status, slugs, auth):
    """Phrases stored for a bot that is gone. Shipped words with no stored persona are not included."""
    if not auth:
        return set()
    names = set()
    if isinstance(ov, dict):
        names.update(n for n, o in ov.items() if isinstance(o, dict))
    if isinstance(enrolled, dict):
        names.update(enrolled)
    if isinstance(status, dict):
        names.update(n for n, row in status.items() if isinstance(row, dict))
    out = set()
    for name in names:
        who = _stored_phrase_persona(name, ov, enrolled, status)
        if who and owner_absent(who, slugs, True):
            out.add(name)
    return out


def wakewords():
    drop_stale_guest()
    cfg = read_json(WAKE_CONFIG, {})
    ov = read_json(path("wake-overrides.json"), {})
    if not isinstance(ov, dict):
        ov = {}
    bots, bot_auth = bot_roster()
    new_ov, changed = ensure_wake_entries(cfg, ov, bots)
    if changed:
        write_json(path("wake-overrides.json"), new_ov, 0o640)
        ov = new_ov
    names = {b["slug"]: b["name"] for b in bots}
    enrolled = {w.get("name"): w for w in read_json(ENROLLED, {}).get("words", []) if isinstance(w, dict)}
    status = read_enroll_status()
    orphans = _orphan_names(ov, enrolled, status, set(names), bot_auth)
    models = read_json(path("models.json"), {})  # phrase -> training note; speaker fields are ignored
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
        if n != BUILTIN_WORD and isinstance(ov.get(n), dict) and (
                ov[n].get("hidden") or ov[n].get("unused") in ("deleted", "unlinked")):
            continue  # deleted on the admin page, or parked under Unused wake words
        if n in orphans:
            continue  # owner bot is gone; listed under Unused, not written back here
        o = ov.get(n) if isinstance(ov.get(n), dict) else {}
        e = enrolled.get(n, {})
        has_model = n in enrolled or os.path.isfile(os.path.join(cfg.get("models_dir", ""), n + ".onnx"))
        persona = str(o.get("persona")) if persona_ok(str(o.get("persona") or "")) else w.get("persona", "grok")
        row = {"name": n, "persona": persona, "enabled": o.get("enabled", True) and has_model,
               "has_model": has_model, "sensitivity": round(1 - float(o.get("threshold", e.get("threshold", w.get("threshold", 0.5)))), 2),
               "provisional": bool(w.get("provisional")), "enrolled": n in enrolled, "metrics": e.get("metrics"),
               "trained_on": model_note(models.get(n) if isinstance(models, dict) else None)}
        slug = n[4:] if isinstance(n, str) and n.startswith("hey_") else ""
        if slug in names:
            row["label"] = "Hey " + names[slug]
        out.append(row)
    for row in out:  # the built-in word is always on (sensitivity can still be changed)
        if row["name"] == BUILTIN_WORD:
            row.update(builtin=True, enabled=row["has_model"])
    return out


_SESSION_DIR = re.compile(r"^\d{8}-\d{6}$")
_CLIP_WAV = re.compile(r"^\d{2}\.wav$")
_FULL_SESSION = ENROLL_REPS  # a finished session has at least this many clips


def _session_fingerprint(directory, names):
    """sha256 of each clip, in name order. None if a clip cannot be read."""
    digest = hashlib.sha256()
    for fn in names:
        digest.update(fn.encode())
        digest.update(b"\0")
        try:
            with open(os.path.join(directory, fn), "rb") as f:
                digest.update(hashlib.sha256(f.read()).digest())
        except OSError:
            return None
    return digest.digest()


def finished_clip_files(phrase_dir):
    """Clip paths from finished unique sessions under one phrase directory.

    A finished session is a YYYYMMDD-HHMMSS directory with a session.json object and at
    least _FULL_SESSION NN.wav files. Empty directories, partial sessions, and directories
    with no completion marker are skipped. Two sessions with the same clip bytes count once;
    the earlier directory is kept. Returns [] when the path is outside the enroll inbox.
    """
    root = os.path.realpath(ENROLL_INBOX)
    base = os.path.realpath(phrase_dir)
    if not (base == root or base.startswith(root + os.sep)) or not os.path.isdir(base):
        return []
    try:
        sessions = sorted(x for x in os.listdir(base) if _SESSION_DIR.fullmatch(x))
    except OSError:
        return []
    out, seen = [], set()
    for ses in sessions:
        real = os.path.realpath(os.path.join(base, ses))
        if not real.startswith(root + os.sep):
            continue
        try:
            with open(os.path.join(real, "session.json")) as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        try:
            names = sorted(fn for fn in os.listdir(real) if _CLIP_WAV.fullmatch(fn))
        except OSError:
            continue
        if len(names) < (5 if doc.get("prior") else _FULL_SESSION):  # Train more keeps 5+; a full session is 5 too
            continue
        fp = _session_fingerprint(real, names)
        if fp is None or fp in seen:
            continue
        seen.add(fp)
        out.extend(os.path.join(real, fn) for fn in names)
    return out


def clip_count(name):
    """Recordings kept for one phrase: finished unique sessions only. 0 when the files are gone."""
    if not wake_name_ok(name):
        return 0
    return len(finished_clip_files(os.path.join(ENROLL_INBOX, name)))


def unused_wakewords():
    """Words the station is not listening for: unlinked, or left behind when a bot was deleted.

    An orphan (the owner bot is gone) is listed even when it is hidden, disabled, unfinished, or has no clips.
    A word Delete had to keep (a shipped id) is marked forgotten and stays off this list.
    """
    ov = read_json(path("wake-overrides.json"), {})
    if not isinstance(ov, dict):
        ov = {}
    enrolled = enrolled_entries()
    status = read_enroll_status()
    roster, auth = bot_roster()
    bots = {b["slug"] for b in roster if isinstance(b, dict) and b.get("slug")}
    orphans = _orphan_names(ov, enrolled, status, bots, auth)
    out = []
    names = set(ov) | set(enrolled) | set(status)
    for name in sorted(names):
        if name == BUILTIN_WORD or not wake_name_ok(name):
            continue
        o = ov.get(name) if isinstance(ov.get(name), dict) else {}
        if o.get("forgotten"):
            continue
        ent = enrolled.get(name) if isinstance(enrolled.get(name), dict) else {}
        st = status.get(name) if isinstance(status.get(name), dict) else {}
        persona = str(o.get("persona") or ent.get("persona") or st.get("persona") or "")
        flag = o.get("unused") if o.get("unused") in ("deleted", "unlinked") else ""
        orphan = name in orphans
        recordings = clip_count(name)
        if not orphan:
            if o.get("hidden"):
                continue
            if not flag and persona in bots:
                continue
            if not flag and not recordings and name not in enrolled:
                continue
        if flag == "unlinked":
            reason = "unlinked"
        elif flag == "deleted" or orphan or (persona and persona not in bots and persona != "grok"):
            reason = "deleted"
        else:
            reason = "unlinked"
        out.append({"name": name, "label": phrase_label(name), "reason": reason, "recordings": recordings,
                    "persona": persona if persona_ok(persona) else ""})
    return out


def delete_wake_word(name, reload=True):
    """Delete one wake word: recordings, trained model, config entry. Returns (status, body). Hey Grok stays."""
    if name == BUILTIN_WORD:
        return (409, {"error": "Hey Grok is built in and can't be deleted."})
    known = {w["name"] for w in wakewords()}
    ov = read_json(path("wake-overrides.json"), {})
    if not isinstance(ov, dict):
        ov = {}
    safe = wake_name_ok(name)
    stored = name in known or name in ov or name in read_enroll_status() or name in enrolled_entries()
    if not safe and not stored:
        return (400, {"error": "bad name"})
    if not stored and not os.path.isdir(os.path.join(ENROLL_INBOX, name)):
        return (200, {"ok": True})  # already gone: delete is idempotent
    code, out = wake("POST", "/forget", {"name": name}) if safe else (200, {})
    if code == 409:
        return (409, {"error": "That word is recording right now. Try again when it finishes."})
    if safe:  # file paths only for a checked name, never ../ or a slash
        for fn in (name + ".onnx", name + ".json"):
            unlink_in(WAKE_DROP_DIR, fn)
        drop_phrase_leftovers(name)
    cfg = read_json(WAKE_CONFIG, {})
    keep_hidden = name in configured_phrase_names(cfg) or (name.startswith("hey_") and name[4:] in {b["slug"] for b in known_bots()})
    if keep_hidden:
        who = (ov.get(name) or {}).get("persona", "grok") if isinstance(ov.get(name), dict) else "grok"
        # forgotten: a shipped id stays hidden, and Unused does not list it again
        ov[name] = {"enabled": False, "hidden": True, "forgotten": True, "persona": who}
    else:
        ov.pop(name, None)
    write_json(path("wake-overrides.json"), ov, 0o640)
    if safe:
        mark_phrase_deleted(name)
    doc = read_enroll_status()
    if doc.pop(name, None) is not None:
        write_enroll_status(doc)
    models = read_json(path("models.json"), {})
    if isinstance(models, dict) and models.pop(name, None) is not None:
        write_json(path("models.json"), models, 0o640)
    if reload:
        wake("POST", "/reload", {})
    return (200, {"ok": True})


def bot_phrase_names(slug):
    """Wake word ids that belong to this bot: overrides or enrolled entries for it, or hey_<slug>. Never Hey Grok."""
    if not slug_ok(slug):
        return []
    ov = read_json(path("wake-overrides.json"), {})
    if not isinstance(ov, dict):
        ov = {}
    try:
        enrolled = enrolled_entries()
    except Exception:
        enrolled = {}
    try:
        status = read_enroll_status()
    except Exception:
        status = {}
    out = []
    for name in sorted(set(ov) | set(enrolled) | set(status if isinstance(status, dict) else {})):
        if name == BUILTIN_WORD or not isinstance(name, str):
            continue
        rows = [x for x in (ov.get(name), enrolled.get(name), (status or {}).get(name)) if isinstance(x, dict)]
        if name == "hey_" + slug or any(str(r.get("persona") or "") == slug for r in rows):
            out.append(name)
    return out


def delete_bot_phrases(slug):
    """A deleted bot's wake words are deleted too: recordings, models, rows.
    Returns (gone, failed): the ids deleted, and [{"name", "reason"}] for any that could not be."""
    gone, failed = [], []
    for name in bot_phrase_names(slug):
        try:
            code, out = delete_wake_word(name, reload=False)
        except Exception as e:  # one stuck word must not hide the rest
            code, out = 500, {"error": "Couldn't delete it (%s)." % type(e).__name__}
        if code == 200:
            gone.append(name)
        else:
            reason = (out or {}).get("error") if isinstance(out, dict) else None
            failed.append({"name": name, "reason": str(reason or "Couldn't delete it.")[:120]})
    if gone:
        wake("POST", "/reload", {})
    return gone, failed


_SPEAKER_META = frozenset({"speaker", "speakers", "guest"})

def model_note(raw):
    """Training note shown for one phrase. Old speaker fields are dropped; None when nothing else is stored."""
    if not isinstance(raw, dict):
        return None
    out = {k: v for k, v in raw.items() if k not in _SPEAKER_META}
    return out or None


# ------------------------------------------------------------------ request handling
def _ipv4_hex(text):
    return ipaddress.IPv4Address(int.from_bytes(bytes.fromhex(text), "little"))


# Container, VPN, and overlay interfaces are not the home network. Loopback is allowed separately.
VIRTUAL_IFACE_RE = re.compile(r"^(docker\d*|br-.*|veth.*|virbr.*|cni.*|flannel.*|cali.*|tailscale\d*|wg.*|tun.*|tap.*|zt.*)$")


def _connected_v4(text):
    """Directly connected IPv4 prefixes from /proc/net/route. Default routes are skipped."""
    nets = []
    for line in (text or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 8:
            continue
        if VIRTUAL_IFACE_RE.match(parts[0]):
            continue
        dest, gateway, mask = parts[1], parts[2], parts[7]
        if len(dest) != 8 or len(gateway) != 8 or len(mask) != 8:
            continue
        try:
            network, gw, m = _ipv4_hex(dest), _ipv4_hex(gateway), _ipv4_hex(mask)
        except ValueError:
            continue
        if int(m) == 0 or int(gw) != 0:
            continue
        try:
            nets.append(ipaddress.ip_network((network, bin(int(m)).count("1")), strict=False))
        except ValueError:
            continue
    return nets


def _v6_prefixes(text):
    """IPv6 prefixes from /proc/net/if_inet6 (address, index, prefix length, scope, flags, name)."""
    nets = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or len(parts[0]) != 32:
            continue
        if len(parts) >= 6 and VIRTUAL_IFACE_RE.match(parts[5]):
            continue
        try:
            ip = ipaddress.IPv6Address(bytes.fromhex(parts[0]))
            nets.append(ipaddress.ip_network((ip, int(parts[2], 16)), strict=False))
        except ValueError:
            continue
    return nets


def home_subnets(v4=None, v6=None):
    """Subnets on this station's interfaces, plus loopback. Not every private range."""
    nets = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
    if v4 is None:
        try:
            with open("/proc/net/route") as f:
                v4 = f.read()
        except OSError:
            v4 = ""
    if v6 is None:
        try:
            with open("/proc/net/if_inet6") as f:
                v6 = f.read()
        except OSError:
            v6 = ""
    for net in _connected_v4(v4) + _v6_prefixes(v6):
        if net not in nets:
            nets.append(net)
    return nets


def lan_ok(ip, nets=None):
    """True for loopback or an address on one of this station's interface subnets."""
    try:
        a = ipaddress.ip_address(str(ip).removeprefix("::ffff:"))
    except ValueError:
        return False
    if a.is_loopback:
        return True
    if nets is None:
        nets = home_subnets()
    return any(a in net for net in nets)


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
REMOTE_MAIN = None  # tests: () -> {"version": "0.1.1", "sha": "<40 hex>"} or raises OSError. No network.
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
REBOOT_FLAG = E("ADMIN_REBOOT_FLAG", "/run/grok-station-reboot-required")  # install.sh: boot options changed
RELAY_DOWN = "Can't reach the voice service right now."
RELAY_ACTOR = "admin"
BOT_PATH = re.compile(r"^/api/bots(?:/([a-z][a-z0-9-]{1,23}))?(?:/(webhook|test|key)(?:/([A-Za-z0-9_-]{1,80}))?)?$")
_IMPORTED_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .'-]{0,31}$")
_IMPORTED_KEY = re.compile(r"^[a-z0-9][a-z0-9 .'-]{0,31}$")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/deploy-pins.js": ("deploy-pins.js", "text/javascript; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/admin.css": ("admin.css", "text/css; charset=utf-8"), "/favicon.ico": ("assets/favicon.ico", "image/x-icon")}
ASSET_TYPE = {"png": "image/png", "svg": "image/svg+xml", "ttf": "font/ttf", "ico": "image/x-icon"}


def slug_ok(slug):
    return isinstance(slug, str) and SLUG_RE.fullmatch(slug) is not None and slug != "grok"


# The voice service's bot id is stricter than a 2–32 free slug: a letter first, then up to 23 more, "grok" reserved.
BOT_ID_ERROR = ("Use 2–24 characters: a letter first, then lowercase letters, digits, or hyphens. "
                "The voice service doesn't allow a longer id.")
BOT_ID_RESERVED = '"grok" is reserved.'


def bot_id_from_name(name):
    """Strict slug for Set up and Add bot. ASCII letters and digits only; anything else is a dash.

    Does not unicode-lowercase or transliterate. The result matches SLUG_RE or is "". "grok" can
    still come back; bot_id_problem rejects that id. The name and the id stay editable after this.
    """
    chars = []
    for ch in str(name or ""):
        if "A" <= ch <= "Z":
            chars.append(ch.lower())
        elif ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            chars.append(ch)
        else:
            chars.append("-")
    s = re.sub(r"-{2,}", "-", "".join(chars)).strip("-")
    if len(s) > 24:
        s = s[:24].rstrip("-")
    if SLUG_RE.fullmatch(s) is None:
        return ""
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


def phrase_problem(text, known=None, for_persona=None):
    """None when the phrase can be a new wake word, otherwise the sentence to show.

    for_persona reclaims that same slug's word, and a word whose owner bot is gone.
    Without it, an active word is still a duplicate. An orphan is not in the active list.
    """
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
    reclaim = False
    if mid in known and persona_ok(for_persona):
        owner = phrase_personas().get(mid) or "grok"
        if owner == for_persona or owner_absent(owner):
            reclaim = True
    if mid in known and not reclaim:
        return duplicate_msg(phrase_label(mid))
    mine_syl = phrase_syllables(words)
    for other in known:
        if not isinstance(other, str) or not other:
            continue
        if reclaim and other == mid:
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
    o.pop("hidden", None)   # recording a word you deleted earlier brings it back
    o.pop("unused", None)   # setting it again from Configure takes it out of Unused wake words
    o.pop("forgotten", None)  # a reclaimed shipped id can be removed again later
    if o.get("enabled") is False:  # and on: a bot's hey_<slug> entry starts off (0.9.2), which kept a word
        o.pop("enabled")           # recorded in 0.10.0 from ever waking ("turned off on the admin page")
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


def read_deleted_phrases():
    """Phrases deleted since they were last recorded. The listener's last_enroll is ignored for these."""
    doc = read_json(path("deleted-phrases.json"), {})
    return {k: v for k, v in doc.items() if isinstance(k, str)} if isinstance(doc, dict) else {}


def mark_phrase_deleted(name):
    doc = read_deleted_phrases()
    doc[name] = int(now())
    write_json(path("deleted-phrases.json"), doc, 0o640)


def clear_phrase_deleted(name):
    doc = read_deleted_phrases()
    if doc.pop(name, None) is not None:
        write_json(path("deleted-phrases.json"), doc, 0o640)


def enrolled_phrase_ids():
    return {w.get("name") for w in read_json(ENROLLED, {}).get("words", []) if isinstance(w, dict) and w.get("name")}


def unlink_in(folder, fn):
    """Remove folder/fn: a file, or a symlink itself (never its target). fn must be a plain name. Missing is fine."""
    if not isinstance(fn, str) or not fn or fn in (".", "..") or os.path.basename(fn) != fn:
        return False
    p = os.path.join(folder, fn)
    try:
        st = os.lstat(p)
    except OSError:
        return False
    import stat as _stat
    if not (_stat.S_ISLNK(st.st_mode) or _stat.S_ISREG(st.st_mode)):
        return False
    try:
        os.unlink(p)
        return True
    except OSError:
        return False


def drop_phrase_leftovers(name):
    """Remove what the wake listener may have left: every enroll session, the recorded model, the list entry.

    Runs after /forget, and covers a listener that was down or refused. Missing files are fine.
    name must already pass wake_name_ok; the session folder is checked to sit inside the inbox.
    """
    import shutil
    if not wake_name_ok(name) or name == BUILTIN_WORD:
        return
    d = os.path.join(ENROLL_INBOX, name)
    if os.path.islink(d):
        unlink_in(ENROLL_INBOX, name)  # the link only, never what it points at
    elif os.path.isdir(d) and os.path.dirname(os.path.realpath(d)) == os.path.realpath(ENROLL_INBOX):
        shutil.rmtree(d, ignore_errors=True)  # rmtree does not follow symlinks inside the tree
    enrolled_dir = os.path.dirname(ENROLLED)
    for fn in (name + ".onnx", name + ".json", name + ".fewshot.npz"):
        unlink_in(enrolled_dir, fn)
    try:
        doc = read_json(ENROLLED, {})
        words = doc.get("words", []) if isinstance(doc, dict) else []
        keep = [w for w in words if not (isinstance(w, dict) and w.get("name") == name)]
        if len(keep) != len(words):
            tmp = ENROLLED + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"words": keep}, f, indent=1)
            os.replace(tmp, ENROLLED)
    except (OSError, ValueError, AttributeError):
        pass


def enrolled_entries():
    return {w["name"]: w for w in read_json(ENROLLED, {}).get("words", []) if isinstance(w, dict) and w.get("name")}


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


def _copy_enroll_progress(row, last):
    """Clip index, kept count, and the newest rejections, when the listener reported them."""
    if not isinstance(row, dict) or not isinstance(last, dict):
        return
    for key in ("index", "kept", "count"):
        val = last.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or not 0 <= val <= 100:
            continue
        row[key] = val
    phase = last.get("phase")
    if isinstance(phase, str) and phase:
        row["phase"] = phase[:24]
    if isinstance(last.get("topup"), bool):
        row["topup"] = last["topup"]
    recent = last.get("recent")
    if not isinstance(recent, list):
        return
    clean = []
    for item in recent[:6]:
        if not isinstance(item, dict):
            continue
        n, ok = item.get("n"), item.get("ok")
        if isinstance(n, bool) or not isinstance(n, int) or not isinstance(ok, bool):
            continue
        clean.append({"n": n, "ok": ok, "why": str(item.get("why") or "")[:24]})
    if clean:
        row["recent"] = clean


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


ENROLL_STALE_S = 180
ENROLL_START_GRACE_S = 20  # a just-started recording row before the listener reports busy
ENROLL_MIC_STOPPED = "The microphone stopped while recording. Press Record voice to try again."
HELPER_V = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")
ENROLL_STALLED = "The station stopped before this wake word was finished. Press Record voice to try again, or delete it."


def derive_enroll_status(prev, wake_status, enrolled_names, now_ts, deleted=None):
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
    built = enrolled_names if isinstance(enrolled_names, dict) else {}
    enrolled = {n for n in (enrolled_names or []) if n}
    last, busy, queued = {}, False, False
    if isinstance(wake_status, dict):
        busy = bool(wake_status.get("enrolling"))
        queued = bool(wake_status.get("enroll_queued"))
        last = wake_status.get("last_enroll") if isinstance(wake_status.get("last_enroll"), dict) else {}
    if isinstance(deleted, dict) and last.get("phrase") in deleted and not busy:
        last = {}  # deleted since: an older listener still reports it; don't bring the row back
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
        elif stage == "recording":
            stuck = entry.get("state") == "recording" and \
                now_ts - int(entry.get("updated_at") or 0) >= ENROLL_START_GRACE_S
            if isinstance(wake_status, dict) and not queued and stuck:
                # The listener answers and isn't recording: the capture died mid-recording (mic unplugged).
                state, new_msg = "failed", ENROLL_MIC_STOPPED
            else:
                state, new_msg = "recording", msg or "Say it on the station."
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
        elif stage == "cancelled":
            state, new_msg = "idle", msg or "Stopped."
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
    if not busy:
        # A word built on the station since this attempt last moved: it's done, whatever the listener reports now
        # (0.10.0 could leave the dialog waiting after a successful build).
        for name, row in list(out.items()):
            b = built.get(name) if isinstance(built.get(name), dict) else {}
            at = b.get("built_at")
            fresh = (isinstance(at, (int, float)) and at >= int(row.get("updated_at") or 0) - 5) or \
                (at is None and b.get("kind") == "fewshot" and not row.get("had_model"))  # built by 0.10.0 (no time)
            if row.get("state") in ACTIVE_ENROLL and fresh:
                m = b.get("metrics") if isinstance(b.get("metrics"), dict) else {}
                msg = f"Ready. It heard {m['heard']} of {m['count']} recordings." if "heard" in m and "count" in m else "Ready."
                out[name] = _status_row(name, "published", msg, now_ts, out)
    if isinstance(wake_status, dict) and not busy and not queued:
        # A saved row still says recording, but the listener answers and isn't recording (it restarted, or its
        # capture died). Only recording: uploading and training legitimately run while not busy.
        for name, row in list(out.items()):
            if row.get("state") == "recording" and now_ts - int(row.get("updated_at") or 0) >= ENROLL_START_GRACE_S:
                print(f"enroll {name}: listener not recording; marked failed", flush=True)
                out[name] = _status_row(name, "failed", ENROLL_MIC_STOPPED, now_ts, out)
    if isinstance(wake_status, dict) and not busy:
        # The listener answers and isn't recording or building, yet a word still says it is: the listener restarted
        # (or an older listener never reports an end). Fail it with a plain message instead of waiting forever.
        for name, row in list(out.items()):
            if row.get("state") in ACTIVE_ENROLL and now_ts - int(row.get("updated_at") or 0) >= ENROLL_STALE_S:
                print(f"enroll {name}: no progress from the wake listener for {ENROLL_STALE_S}s; marked failed", flush=True)
                out[name] = _status_row(name, "failed", ENROLL_STALLED, now_ts, out)
    if wake_name_ok(phrase) and isinstance(out.get(phrase), dict):
        _copy_enroll_progress(out[phrase], last)
    return out


def silence_weak(doc):
    """A word that isn't good enough stays off, including when an older model is still on disk."""
    if not isinstance(doc, dict):
        return
    ov = read_json(path("wake-overrides.json"), {})
    if not isinstance(ov, dict):
        ov = {}
    changed = False
    for name, row in doc.items():
        if name == BUILTIN_WORD or not isinstance(row, dict) or row.get("state") != "failed_quality":
            continue
        o = dict(ov.get(name) if isinstance(ov.get(name), dict) else {})
        if o.get("enabled") is False:
            continue
        o["enabled"] = False
        who = row.get("persona")
        if "persona" not in o and persona_ok(str(who or "")):
            o["persona"] = who
        ov[name] = o
        changed = True
    if not changed:
        return
    write_json(path("wake-overrides.json"), ov, 0o640)
    wake("POST", "/reload", {})


sys.path.insert(0, HERE)
import gs_routine  # noqa: E402  (shared with scripts/gs-update.sh; a test keeps them byte-identical)

ROUTINES = {m: os.path.join(HERE, "routine-%s.txt" % m) for m in ("home", "cloud")}
FINISH = "finish-update.json"
FINISH_WAIT_S = 600  # a sent update waits this long for the bot's reply; matches the relay's one ask per bot per 10 min


def deploy_pins():
    """commit, files and update_commit from web/deploy-pins.js. {} when it can't be read."""
    try:
        text = open(os.path.join(WEB, "deploy-pins.js"), encoding="utf-8").read()
    except OSError:
        return {}
    commit = re.search(r'^  commit: "([0-9a-f]{40})",$', text, re.M)
    upd = re.search(r'^  update_commit: "([0-9a-f]{40})",$', text, re.M)
    files = dict(re.findall(r'^    "([a-z0-9_./-]+)": "([0-9a-f]{64})",?$', text, re.M))
    return {"commit": commit.group(1) if commit else "", "update_commit": upd.group(1) if upd else "", "files": files}


def render_bot_routine(bot, station, fingerprint, pub, pins=None):
    """Exactly the instructions this bot will adopt (F: only the allowed template renders). Raises gs_routine.Bad."""
    pins = deploy_pins() if pins is None else pins
    mode = "home" if bot.get("key_mode") == "home" else "cloud"
    with open(ROUTINES[mode], encoding="utf-8") as fh:
        template = fh.read()
    if template != gs_routine.TEMPLATES[mode]:  # the station's file ships with its code; anything else is tampering
        raise gs_routine.Bad("station template file differs from this release")
    fp = str(fingerprint or "").upper()
    text = gs_routine.render(template, mode, bot.get("slug", ""), pins.get("commit", ""),
                             pins.get("files", {}).get("scripts/gs-update.sh", ""),
                             station if mode == "cloud" else "", fp if mode == "cloud" else "",
                             pub if mode == "cloud" else "")
    rel = gs_routine.check(text, mode)
    if rel != version():
        raise gs_routine.Bad("release")
    return text


def portrait_version(slug):
    """mtime_ns of the portrait /api/portrait serves for slug, as a short string; "" when there is none."""
    if not slug_ok(slug):
        return ""
    base = os.path.realpath(os.path.dirname(PERSONAS))
    for ext in ("png", "jpg", "jpeg", "webp"):
        f = os.path.realpath(os.path.join(base, slug + "." + ext))
        if f.startswith(base + os.sep) and os.path.isfile(f):
            try:
                st = os.stat(f)
            except OSError:
                return ""
            return "%x-%x" % (st.st_mtime_ns, st.st_size)
    return ""


def read_finish():
    doc = read_json(path(FINISH), {})
    if not isinstance(doc, dict):
        doc = {}
    doc.setdefault("sent", {}); doc.setdefault("copied", {}); doc.setdefault("updaters", {})
    return doc


def forget_finish(slug):
    """Drop Finish update bookkeeping for a bot that is no longer listed.

    finish_rows only counts the bots it is given. This keeps a deleted slug from looking behind if that
    bot is added again, and from sitting in finish-update.json after it is gone.
    """
    if not isinstance(slug, str) or not slug:
        return
    doc = read_finish()
    changed = False
    for key in ("sent", "copied"):
        bucket = doc.get(key)
        if isinstance(bucket, dict) and slug in bucket:
            bucket.pop(slug, None)
            changed = True
    ups = doc.get("updaters")
    if isinstance(ups, dict):
        for key, who in list(ups.items()):
            if who == slug:
                ups.pop(key, None)
                changed = True
    if changed:
        write_json(path(FINISH), doc)


def finish_rows(bots, station, fingerprint, pub, pins=None, finish=None, now=None):
    """The Finish update dialog: computers, each with its bots, the exact text, and each row's state.

    E: a row is done only when the bot's signed status names this release and the hash of exactly this text.
    """
    pins = deploy_pins() if pins is None else pins
    finish = read_finish() if finish is None else finish
    rel = version()
    now = time.time() if now is None else now
    groups, behind = {}, 0
    for b in bots:
        if not (b.get("paired") and (b.get("webhook") or {}).get("configured")):
            continue
        home = b.get("key_mode") == "home"
        try:
            text = render_bot_routine(b, station, fingerprint, pub, pins)
            want, err = gs_routine.instr_hash(text), ""
        except (gs_routine.Bad, OSError) as e:
            text, want, err = "", "", "Can't build instructions: %s" % e
        u = b.get("update") or {}
        wait_left = 0
        if u.get("release") == rel and want and u.get("instr") == want:
            state = "done"
        elif b.get("update_refused_at"):
            state = "refused"
        elif not u and not finish["copied"].get(b["slug"]) and not finish["sent"].get(b["slug"]):
            state = "copy"  # set up before 0.11.8: its instructions don't know update_bot yet
        elif u.get("release") == rel and u.get("instr") != want:
            state = "different"
        elif finish["sent"].get(b["slug"], {}).get("instr") == want:
            try:
                sent_at = float(finish["sent"][b["slug"]].get("at") or 0)
            except (TypeError, ValueError):
                sent_at = 0.0
            wait_left = int(max(0, sent_at + FINISH_WAIT_S - now))
            state = "waiting" if wait_left > 0 else "noreply"  # stored with its time, so a restart keeps it
        else:
            state = "needs"
        if err:
            state = "error"
        if state != "done":
            behind += 1
        if home:
            key = "c:" + u["computer"] if u.get("computer") else "h:" + (b.get("reply_host") or b["slug"])
            label = b.get("reply_host") or "Home computer"
        else:
            key, label = "bot", "Bot computer"
        g = groups.setdefault(key, {"key": key, "label": label, "home": home, "bots": [], "helper": None})
        if home and (b.get("helper") or {}).get("version"):
            g["helper"] = b["helper"]["version"]
        g["bots"].append({"slug": b["slug"], "name": b.get("name") or b["slug"], "mode": "home" if home else "cloud",
                          "state": state, "message": text, "instr": want, "have": u.get("instr"),
                          "have_release": u.get("release"), "error": err,
                          "wait_left": wait_left if state == "waiting" else 0})
    for g in groups.values():
        g["bots"].sort(key=lambda r: r["name"].lower())
        chosen = finish["updaters"].get(g["key"])
        answered = [r["slug"] for r in g["bots"] if r["have"]]
        names = [r["slug"] for r in g["bots"]]
        g["updater"] = chosen if chosen in names else (answered[0] if answered else (names[0] if names else None))
        if not g["home"]:
            g["updater"] = None  # no shared helper on a bot computer
        g["want_helper"] = HELPER_RELEASE if g["home"] else None
    order = sorted(groups.values(), key=lambda g: (g["key"] != "bot", g["label"].lower()))
    return {"release": rel, "commit": pins.get("update_commit", ""), "behind": behind, "computers": order}


def enroll_live(st):
    """Listener fields the set-wake dialog polls. All null when the listener didn't answer."""
    if not isinstance(st, dict):
        return {"enrolling": None, "last": None, "level_dbfs": None, "session": None, "capture": None}
    return {"enrolling": st.get("enrolling"), "last": st.get("last_enroll"),
            "level_dbfs": st.get("level_dbfs"), "session": st.get("session"), "capture": st.get("capture")}


def refresh_enroll_status():
    """Poll the wake listener and the enrolled-model list, then write enroll-status.json.

    Returns (doc, wake_status). wake_status is None when the listener didn't answer.
    """
    code, st = wake("GET", "/status")
    wake_status = st if code == 200 and isinstance(st, dict) else None
    prev = read_enroll_status()
    doc = derive_enroll_status(prev, wake_status, enrolled_entries(), int(now()), read_deleted_phrases())
    if doc != prev:
        write_enroll_status(doc)
    silence_weak(doc)
    return doc, wake_status


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
    """Remove a leftover guest-mode flag from t1.json. Missing file or key is a no-op."""
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


XAI_KEY_FILE = E("ADMIN_XAI_KEY_FILE", "/etc/grok-station/secrets/xai_api_key")
XAI_KEY_RE = re.compile(r"[A-Za-z0-9._-]{20,200}")


def lan_ip():
    """This station's LAN address for the setup screen (UDP connect sends nothing). "" if offline."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
            so.connect(("192.0.2.1", 9))  # TEST-NET: only picks the outgoing interface
            ip = so.getsockname()[0]
        return "" if ip.startswith("127.") else ip
    except OSError:
        return ""


def xai_key_set():
    """True when the relay's key file exists. The admin page can't read it (0400 for the relay) and never tries."""
    try:
        return os.stat(XAI_KEY_FILE).st_size > 0
    except OSError:
        return False


LOCAL_RELAY_TOKEN = E("ADMIN_LOCAL_RELAY_TOKEN_FILE", "/etc/grok-station/secrets/admin_token")
LOCAL_RELAY_ORIGIN = "http://127.0.0.1:" + E("RELAY_PORT", "8790")


def station_urls():
    """Base URLs a bot uses for the reply contract (LAN only)."""
    import socket
    out = ["https://" + socket.gethostname() + ".local"]
    ip = lan_ip()
    if ip:
        out.append("https://" + ip)
    return out


def station_base():
    """The station address written into bot instructions: ADMIN_STATION_ADDRESS when set, else this station's
    own name (station_urls). Never the request's Host header, which the browser or a proxy controls."""
    for cand in [os.environ.get("ADMIN_STATION_ADDRESS", "").strip()] + station_urls():
        if cand and gs_routine.STATION.match(cand):
            return cand
    return ""


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


def load_cards():
    """app/cards.py in a checkout, or ../cards.py when admin is installed under /opt/grok-station/admin."""
    import importlib.util
    mod = getattr(load_cards, "_mod", None)
    if mod is not None:
        return mod
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in (os.path.join(here, "..", "app", "cards.py"), os.path.join(here, "..", "cards.py")):
        path = os.path.abspath(rel)
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location("gs_cards_admin", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        load_cards._mod = mod
        return mod
    raise ImportError("cards.py")


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


def box_pub_ok(text):
    """Standard base64 of 32 bytes (an X25519 or Ed25519 public key). Whitespace around a paste is ignored."""
    if not isinstance(text, str):
        return False
    s = "".join(text.split())
    if not s or len(s) > 80:
        return False
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception:
        return False
    return len(raw) == 32


def bot_public_key(b):
    """Pinned public key for the browser, or None. An unpaired or unusable value is not forwarded."""
    if not isinstance(b, dict) or b.get("paired") is not True:
        return None
    pub = b.get("public_key")
    if not box_pub_ok(pub):
        return None
    return "".join(pub.split())


def stats_view(raw):
    """avg_rtt_ms of answered asks and how many of the last 10 timed out. Nothing else."""
    if not isinstance(raw, dict):
        raw = {}
    avg = raw.get("avg_rtt_ms")
    if type(avg) is not int or avg < 0 or avg > 600000:
        avg = None
    n = raw.get("timeouts")
    if type(n) is not int or n < 0 or n > 10:
        n = 0
    return {"avg_rtt_ms": avg, "timeouts": n}


_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._'’-]{0,39}")


def host_name_ok(name):
    """Name of the home computer that posts a bot's replies. Shown only; the station never checks it."""
    return isinstance(name, str) and _HOST_RE.fullmatch(name) is not None and name == name.strip()


def clean_host(raw):
    """A pasted computer name, trimmed and checked. None when unusable, "" when empty."""
    if not isinstance(raw, str):
        return None
    t = " ".join(raw.split())
    if t == "":
        return ""
    return t if host_name_ok(t) else None


HOST_BAD = "Type the computer's name: letters, numbers, spaces, dots or dashes, up to 40."


def safe_bot(b):
    """The fields the browser may see. Tokens and webhook secrets are dropped.

    public_key is the pinned bot key (it is public). It is returned only when the bot is paired
    and the value is standard base64 of 32 bytes; otherwise null.
    """
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
        "paired": b.get("paired") is True,
        "public_key": bot_public_key(b),
        "key_mode": b.get("key_mode") if b.get("key_mode") in ("cloud", "home") else "cloud",
        "reply_host": b.get("reply_host") if host_name_ok(b.get("reply_host")) else None,
        "helper": helper_view(b),
        "update": update_view(b.get("update")),
        "update_refused_at": _epoch(b.get("update_refused_at")),
        "stats": stats_view(b.get("stats")),
    }


HELPER_RELEASE = "0.11.8"  # gs_seal_common.HELPER_VERSION of the pinned helper (a test keeps them equal)


def _vtuple(v):
    return tuple(int(x) for x in v.split(".")) if isinstance(v, str) and HELPER_V.fullmatch(v) else None


def helper_view(b):
    """Home bots only: the helper release last seen on a signed post, and whether it is behind ours.

    None seen means a helper older than 0.11.7 (it never sent a version), so that is out of date too.
    """
    if b.get("key_mode") != "home":
        return None
    seen = b.get("helper_version") if HELPER_V.fullmatch(str(b.get("helper_version") or "")) else None
    cur = _vtuple(seen)
    return {"version": seen, "release": HELPER_RELEASE, "out_of_date": cur is None or cur < _vtuple(HELPER_RELEASE)}


_UPD = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")


def update_view(u):
    """A bot's last signed update status, or None. Only checked shapes reach the page."""
    if not isinstance(u, dict) or not _UPD.fullmatch(str(u.get("release") or "")) \
            or not re.fullmatch(r"[0-9a-f]{16}", str(u.get("instr") or "")):
        return None
    helper = u.get("helper") if _UPD.fullmatch(str(u.get("helper") or "")) else None
    comp = u.get("computer") if re.fullmatch(r"[0-9a-f]{16}", str(u.get("computer") or "")) else None
    return {"release": u["release"], "instr": u["instr"], "helper": helper, "computer": comp, "at": _epoch(u.get("at"))}


def empty_imported():
    return {"source": "", "updated_at": None, "rows": []}


def safe_imported(raw):
    """Names the browser may render. Anything else from the relay is dropped."""
    if not isinstance(raw, dict):
        return empty_imported()
    rows, seen = [], set()
    for ent in raw.get("rows") or []:
        if not isinstance(ent, dict):
            continue
        name = ent.get("name")
        if not isinstance(name, str) or _IMPORTED_NAME.fullmatch(name) is None:
            continue
        key = name.casefold()
        got = ent.get("key")
        if isinstance(got, str) and got != key:
            continue
        if _IMPORTED_KEY.fullmatch(key) is None or key in seen:
            continue
        state = ent.get("state")
        if state not in ("new", "missing", "configured"):
            continue
        seen.add(key)
        rows.append({"key": key, "name": name, "state": state})
    src = raw.get("source") if slug_ok(raw.get("source")) else ""
    return {"source": src, "updated_at": _epoch(raw.get("updated_at")), "rows": rows}


def safe_routed(raw):
    out, seen = [], set()
    if not isinstance(raw, list):
        return out
    for ent in raw:
        if not isinstance(ent, dict) or not slug_ok(ent.get("slug")) or ent["slug"] in seen:
            continue
        name = ent.get("name")
        if not isinstance(name, str) or _IMPORTED_NAME.fullmatch(name) is None:
            name = ent["slug"]
        seen.add(ent["slug"])
        out.append({"slug": ent["slug"], "name": name})
    return out


def _count(data, key):
    v = data.get(key) if isinstance(data, dict) else None
    return v if type(v) is int and v >= 0 else 0


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
                "paired": False, "public_key": None, "key_mode": "cloud", "reply_host": None, "stats": stats_view(None),
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


def display_public(out):
    """Display settings the browser may see. Dim coordinates are 2 decimals. No device paths."""
    if not isinstance(out, dict):
        out = {}
    vals = out.get("values") if isinstance(out.get("values"), dict) else {}
    body = dict(out)
    body["values"] = dimming.display_view(vals)
    # Sunrise dimming uses the station location. Only whether one is set reaches the browser.
    body["has_location"] = bool(location_label()) or bool(str(vals.get("GROK_STATION_LAT") or "").strip()
                                                        and str(vals.get("GROK_STATION_LON") or "").strip())
    body["values"].pop("GROK_STATION_LAT", None)
    body["values"].pop("GROK_STATION_LON", None)
    root = os.environ.get("GROK_STATION_SYS_ROOT", "/")
    try:
        body["dim"] = dimming.hardware_status(root)
    except OSError:
        body["dim"] = {"sensor": None, "backlight": None}
    return body


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


# ------------------------------------------------------------------ relay status + updates


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
    detail = " ".join(detail.split())[:80] or "-"
    if not re.fullmatch(r"[a-z0-9-]{1,40}", event) or "://" in detail or "@" in detail:
        return
    append_audit(f"{ts} admin station {event} {detail}")


def _settings_path():
    return path("update-settings.json")


def _settings_doc():
    d = read_json(_settings_path(), {})
    return d if isinstance(d, dict) else {}


def password_grace_min():
    """Minutes a correct password covers later changes. 0 always asks. Bad values fall back to 10."""
    n = _settings_doc().get("password_grace_min", 10)
    if isinstance(n, bool) or not isinstance(n, int) or n < 0 or n > GRACE_MAX:
        return 10
    return n


def store_password_grace_min(n):
    """Merge the grace setting into the admin settings file. Leaves autocheck alone."""
    d = _settings_doc()
    d["password_grace_min"] = n
    write_json(_settings_path(), d)


def load_update_settings():
    return {"autocheck": _settings_doc().get("autocheck") is True}


def semver_tuple(value):
    if not isinstance(value, str):
        return None
    match = _SEMVER.fullmatch(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def version_order(installed, available):
    """newer, same, older, or unknown.

    unknown: available is not MAJOR.MINOR.PATCH, so it is never an upgrade.
    newer: available is semver and installed is not (a short commit id left by an older installer, or dev).
    """
    avail = semver_tuple(available)
    if avail is None:
        return "unknown"
    inst = semver_tuple(installed)
    if inst is None:
        return "newer"
    if avail > inst:
        return "newer"
    if avail < inst:
        return "older"
    return "same"


def update_block(installed, available, checked):
    """None when apply may start. Otherwise the error to show. checked is False before a successful check."""
    if not checked:
        return "Check for updates first."
    order = version_order(installed, available)
    if order == "newer":
        return None
    if order == "same":
        return "Already up to date."
    remote = available if isinstance(available, str) and available else "nothing"
    inst = installed if isinstance(installed, str) and installed else "nothing"
    return f"Refusing to install {remote} over {inst}. The main branch is older than this station."


def remote_main():
    """origin/main VERSION and full commit id. The helper fetches the recorded clone. Tests inject REMOTE_MAIN."""
    if REMOTE_MAIN is not None:
        out = REMOTE_MAIN()
        if not isinstance(out, dict):
            raise OSError("remote")
    else:
        ok, out = helper("update-remote", timeout=40)
        if not ok or not isinstance(out, dict):
            raise OSError("remote")
    sha = str(out.get("sha") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise OSError("remote")
    ver = str(out.get("version") or "").strip()
    if semver_tuple(ver) is None:
        ver = ""
    return {"version": ver, "sha": sha}


def run_update_check():
    """Look up origin/main's VERSION and cache it. Never installs anything."""
    try:
        info = remote_main()
    except (OSError, subprocess.TimeoutExpired):
        write_json(path("update-check.json"), {"available": None, "sha": None, "error": True, "checked_at": int(now())}, 0o640)
        _audit_update("update-check-failed", "network")
        return
    prev = _update_cache().get("known")
    known = dict(prev) if isinstance(prev, dict) else {}
    if info["version"]:
        known[info["sha"][:7]] = info["version"]
    known = dict(list(known.items())[-30:])
    write_json(path("update-check.json"), {
        "available": info["version"] or None, "sha": info["sha"], "error": False, "checked_at": int(now()),
        "known": known,
    }, 0o640)
    _audit_update("update-check", info["version"] or info["sha"][:7])


def _short_error(text):
    """One line for the page. The install log itself is not returned."""
    if not isinstance(text, str):
        return "The update failed."
    s = " ".join(text.split())[:160]
    low = s.lower()
    if not s or "bearer " in low or "-----begin" in low or "password=" in low or "://" in s:
        return "The update failed."
    return s


def update_log():
    """Latest update attempt's log, through the root helper (which hides secrets). Admin session only."""
    ok, out = helper("update-log", timeout=10)
    if not ok or not isinstance(out, dict) or not isinstance(out.get("lines"), list):
        return {"lines": [], "at": None, "error": "Couldn't read the update log."}
    return {"lines": [str(x)[:400] for x in out["lines"][-200:]], "at": out.get("at") if isinstance(out.get("at"), int) else None}


def update_job(installed=None):
    """State of the update unit. A failure is one line; the log stays in the journal.

    The unit's Result is sticky (it describes the last run, forever), and install.sh can fail its post-install
    health check after the new version is already in place. So a "failed" unit only counts as a failed update
    when the installed VERSION is not the one that run was installing (update-target.json, written at start).
    """
    ok, out = helper("update-status", timeout=10)
    if not ok or not isinstance(out, dict):
        return {"state": "unknown"}
    state = out.get("state") if out.get("state") in ("running", "succeeded", "failed", "idle") else "unknown"
    if state in ("failed", "succeeded"):
        target = read_json(path("update-target.json"), {})
        target = target.get("to") if isinstance(target, dict) and isinstance(target.get("to"), str) else ""
        inst = version() if installed is None else installed
        if not target:
            return {"state": "idle"}  # no update started from this page since 0.1.1: nothing to report
        if state == "failed" and semver_tuple(target) and version_order(inst, target) in ("same", "older"):
            return {"state": "succeeded", "warning": True}
        if state == "succeeded" and version_order(inst, target) == "newer":
            return {"state": "failed"}
    job = {"state": state}
    if state == "failed":
        line = _short_error(out.get("error")) if out.get("error") else ""
        job["error"] = ("The update didn't finish: " + line) if line and line != "The update failed." \
            else "The update didn't finish. Use View update log to see why."
    return job


_HEX = re.compile(r"[0-9a-f]{7,40}")


def update_history(rows, known=None):
    """Audit lines -> [{at, event, version}] with versions instead of commit ids where they are known.

    known maps a short commit id to its VERSION (kept in update-check.json). An unknown commit id is
    shown as an earlier build, never as a hash.
    """
    known = known if isinstance(known, dict) else {}
    out = []
    for raw in rows or []:
        parts = str(raw or "").split()
        if len(parts) < 4:
            continue
        detail = " ".join(parts[4:]) if len(parts) > 4 else ""
        ver = ""
        for tok in detail.replace("→", " ").split():
            if semver_tuple(tok):
                ver = (ver + " → " + tok) if ver else tok
            elif _HEX.fullmatch(tok.lower()):
                v = known.get(tok.lower()[:7], "")
                tok_out = v if semver_tuple(v) else "earlier build"
                ver = (ver + " → " + tok_out) if ver else tok_out
        if not ver and detail not in ("", "-"):
            ver = detail if not _HEX.search(detail.lower()) else ""
        out.append({"at": parts[0], "event": parts[3], "version": ver})
    return out


def _update_cache():
    cache = read_json(path("update-check.json"), {})
    return cache if isinstance(cache, dict) else {}


def updates_view():
    cache = _update_cache()
    installed = version()
    sha = cache.get("sha") if isinstance(cache.get("sha"), str) and re.fullmatch(r"[0-9a-f]{40}", cache.get("sha") or "") else None
    # Only a MAJOR.MINOR.PATCH counts. A cache from before 0.1.0 holds a full commit id here: treat it as not checked.
    avail = cache.get("available") if isinstance(cache.get("available"), str) and semver_tuple(cache.get("available")) else None
    checked = bool(sha) and not cache.get("error")  # an old cache (commit id in "available") has no "sha": unchecked
    order = version_order(installed, avail) if checked else None
    blocked = update_block(installed, avail, True) if checked and order in ("older", "unknown") else None
    return {
        "installed": installed,
        "available": avail,
        "available_sha": sha[:7] if sha else None,
        "update_available": order == "newer",
        "up_to_date": order == "same",
        "update_blocked": blocked,
        "cant_check": bool(cache.get("error")),
        "checked_at": cache.get("checked_at") if isinstance(cache.get("checked_at"), int) else None,
        "autocheck": load_update_settings()["autocheck"],
        "job": update_job(installed),
        "reboot_required": os.path.exists(REBOOT_FLAG),
        "history": update_history(recent_audit(20, "update"), cache.get("known")),
    }


def start_update():
    """Start the fixed update unit only when origin/main is a newer VERSION.

    The unit runs in its own cgroup, so the admin restart it causes doesn't stop it. The page reads
    status again after this service is back. No URL or ref is taken from the caller.
    """
    cache = _update_cache()
    sha = cache.get("sha") if isinstance(cache.get("sha"), str) else ""
    checked = bool(re.fullmatch(r"[0-9a-f]{40}", sha)) and not cache.get("error")
    avail = cache.get("available") if isinstance(cache.get("available"), str) else ""
    block = update_block(version(), avail, checked)
    if block:
        return 409, {"error": block}
    ok, out = helper("update-start", timeout=15)
    if not ok:
        _audit_update("update-failed", "start")
        return 500, {"error": "The update didn't start."}
    write_json(path("update-target.json"), {"from": version(), "to": avail, "at": int(now())})
    _audit_update("update-start", f"{version()} -> {avail}")
    return 202, {"ok": True}


def update_check_loop():
    """Daily check (only when turned on). Checks for a new version once a day. Never installs anything on its own."""
    while True:
        time.sleep(3600)
        if not load_update_settings()["autocheck"]:
            continue
        last = read_json(path("update-check.json"), {})
        if isinstance(last, dict) and isinstance(last.get("checked_at"), int) and now() - last["checked_at"] < 86400:
            continue
        run_update_check()


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

    def voice_ingress(self, method, p):
        """POST /voice/reply and POST /voice/show on this HTTPS port, forwarded to the loopback relay.

        LAN only. No admin session. The body is a sealed box. There is no reply token. The question rides
        on the webhook, so this port has no request route. This port must never be port-forwarded.
        """
        if not lan_ok(self.client_address[0]):
            return self.send(403, {"error": "LAN only"})
        caps = {"/voice/reply": 8192, "/voice/show": 8192, "/voice/avatar": 4 << 20}  # a picture may be larger
        if method != "POST" or p not in caps:
            return self.send(404, {"error": "not found"})
        if self.headers.get("Transfer-Encoding"):
            return self.send(411, {"error": "length required"})  # no chunked bodies; Content-Length only
        cl = (self.headers.get("Content-Length") or "0").strip()
        if not re.fullmatch(r"[0-9]{1,9}", cl):
            return self.send(400, {"error": "bad request"})  # digits only: int() would take "1_0" and "+5"
        n = int(cl)
        if n > caps[p]:
            return self.send(413, {"error": "too large"})
        data = self.rfile.read(n) if n else b""
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        hv = (self.headers.get("X-GS-Helper") or "").strip()
        if HELPER_V.fullmatch(hv):
            headers["X-GS-Helper"] = hv  # the home helper's release; the relay records it only after the seal checks
        req = urllib.request.Request(LOCAL_RELAY_ORIGIN + p, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(req, timeout=10) as r:
                status, raw = r.status, r.read(65536)
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read(65536)
        except Exception as e:
            print(f"voice ingress failed: {type(e).__name__}", flush=True)
            return self.send(503, {"error": "relay unavailable"})
        try:
            j = json.loads(raw.decode() or "{}")
        except ValueError:
            j = {}
        return self.send(status, j if isinstance(j, dict) else {})

    def route(self, method):
        u = urllib.parse.urlsplit(self.path)
        p, q = u.path, dict(urllib.parse.parse_qsl(u.query))
        if p.startswith("/voice/"):
            return self.voice_ingress(method, p)
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
                               "xai_key_set": xai_key_set(),
                               "wizard": read_json(path("wizard.json"), {"step": 2, "done": False}) if signed else None})

    # -- xAI key: write-only. The value is passed to the root helper on stdin and never stored, logged or echoed here.
    def api_get_xai_key(self, p, q, j):
        st = xai_key_set()
        last4 = str(read_json(path("xai_key_hint.json"), {}).get("last4", "")) if st else ""
        return self.send(200, {"set": st, "last4": last4 if re.fullmatch(r"[A-Za-z0-9]{4}", last4) else ""})

    def api_post_xai_key(self, p, q, j):
        key = str(j.get("key", "")).strip()
        if not XAI_KEY_RE.fullmatch(key):
            return self.send(400, {"error": "That doesn't look like an xAI API key."})
        ok, out = helper("xai-key-set", stdin={"key": key})
        last4 = key[-4:] if ok else ""  # only the last 4 characters are kept, to say which key is set
        key = None
        if not ok:
            return self.send(503, {"error": "Couldn't save the key. Try again."})
        write_json(path("xai_key_hint.json"), {"last4": last4})
        AUTH.publish()
        return self.send(200, {"ok": True, "set": True})

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
                # first run: code, password, then the xAI key (Wi-Fi first only on the setup hotspot)
                write_json(path("wizard.json"), {"step": 2 if AUTH.mode() == "hotspot" else 3, "done": False})
            s = AUTH.new_session()
            return self.send(200, {"ok": True}, headers=[self.set_cookie("gs_session", s, SESSION_TTL),
                                                          self.set_cookie("gs_setup", "", 0)])
        return self.send(404, {"error": "not found"})

    def api_post_login(self, p, q, j):
        if AUTH.locked():
            return self.send(429, {"error": "Too many tries. Wait 15 minutes.", "locked_s": AUTH.locked()})
        s = AUTH.login(j.get("password"))
        if not s:
            return self.send(429 if AUTH.locked() else 401, {"error": PASSWORD_BAD, "locked_s": AUTH.locked()})
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
        return self.send(200, {"wake": st if code == 200 else None, "relay": relay if ok_relay is not None else None, "xai_key": xai_key_set(),
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
        elif sec == "display":
            out = display_public(out)
        return self.send(200, out)

    def api_put_settings(self, p, q, j):
        sec = p.split("/")[3] if p.count("/") >= 3 else ""
        if sec not in ("display", "time", "audio"):
            return self.send(404, {"error": "not found"})
        label = tz = None
        dim_mode = None
        if sec == "time":
            j, err, label, tz = time_body(j)
            if err:
                return self.send(400, {"error": err})
        elif sec == "display":
            j, err = dimming.normalize_form(j if isinstance(j, dict) else {})
            if err:
                return self.send(400, {"error": err})
            if "GROK_STATION_DIM_MODE" in j:
                dim_mode = j["GROK_STATION_DIM_MODE"]
        ok, out = helper("settings-set", sec, stdin=j)
        if not ok:
            msg = out.get("error", "Couldn't save.") if isinstance(out, dict) else "Couldn't save."
            if not isinstance(msg, str) or coord_key(msg):
                msg = "Couldn't save."
            return self.send(400, {"error": msg})
        if dim_mode in dimming.MODES:
            audit("-", "dim-" + dim_mode)
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

    def api_get_cards(self, p, q, j):
        """Logo host allowlist. A relay miss is an empty list so the rest of the display page still loads."""
        got = relay_call("GET", "/admin/cards")
        hosts = []
        if got and got[0] == 200 and isinstance(got[1], dict) and isinstance(got[1].get("hosts"), list):
            hosts = [h for h in got[1]["hosts"] if isinstance(h, str)]
        return self.send(200, {"hosts": hosts})

    def api_put_cards(self, p, q, j):
        """Save hosts on the relay, then the display env. The helper is not the only check."""
        try:
            norm = load_cards().parse_hosts(j.get("hosts"))
        except Exception:
            norm = None
        if norm is None:
            return self.send(400, {"error": "Use public HTTPS host names only, up to 8."})
        got = relay_call("PUT", "/admin/cards", {"hosts": norm, "actor": RELAY_ACTOR})
        if not got or got[0] != 200:
            return self.send(503, {"error": "Couldn't save logo hosts."})
        ok, _out = helper("settings-set", "display", stdin={"GROK_STATION_LOGO_HOSTS": ",".join(norm)})
        if not ok:
            return self.send(503, {"error": "Couldn't apply logo hosts on the display."})
        return self.send(200, {"ok": True, "hosts": norm})

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

        def busy(tool):
            """Card names whose device is open (arecord -l lists them with "Subdevices: 0/1")."""
            try:
                out = subprocess.run([tool, "-l"], capture_output=True, timeout=5).stdout.decode()
            except (OSError, subprocess.TimeoutExpired):
                return []
            names, cur = set(), None
            for line in out.splitlines():
                m = re.match(r"card \d+: \S+ \[([^\]]+)\]", line)
                if m:
                    cur = m.group(1)
                elif cur and re.match(r"\s*Subdevices: 0/\d+", line):
                    names.add(cur)
            return sorted(names)
        code, st = wake("GET", "/status")
        flags = mic_reset_public()
        if code == 200 and isinstance(st.get("mic_reset"), dict):
            for key in ("button", "night", "drought"):
                if isinstance(st["mic_reset"].get(key), bool):
                    flags[key] = st["mic_reset"][key]
        return self.send(200, {"inputs": cards("arecord"), "outputs": cards("aplay"),
                               "in_use": busy("arecord"), "capture": st.get("capture") if code == 200 else None,
                               "level_dbfs": st.get("level_dbfs") if code == 200 else None,
                               "level_at": st.get("level_at") if code == 200 else None,
                               "mic_reset": flags, "wait_tone": wait_tone_on()})

    def api_put_audio(self, p, q, j):
        """Switches only. The listener re-reads the file; this does not stop it."""
        if p == "/api/audio/wait-tone":
            saved = save_wait_tone(j)
            if saved is None:
                return self.send(400, {"error": "Couldn't save that."})
            return self.send(200, {"ok": True, "wait_tone": saved})
        if p != "/api/audio/mic-reset":
            return self.send(404, {"error": "not found"})
        saved = save_mic_reset(j)
        if saved is None:
            return self.send(400, {"error": "Couldn't save that."})
        return self.send(200, {"ok": True, "mic_reset": saved})

    def api_post_audio(self, p, q, j):
        if p == "/api/audio/mic-reset":
            # 409, not 503: a reset failure is not "the station is down" (that banner is 502/503/504).
            code, out = wake("POST", "/mic-reset", {}, timeout=8)
            if code == 202:
                audit("-", "mic-reset " + ("deferred" if out.get("deferred") else "ok"))
            else:
                audit("-", "mic-reset failed")
            if code == 202:
                return self.send(202, {"ok": True, "deferred": bool(out.get("deferred"))})
            if code == 409 and isinstance(out.get("error"), str):
                return self.send(409, {"error": out["error"]})
            return self.send(409, {"error": "Couldn't reset the microphone."})
        if p == "/api/audio/test":
            # Empty body plays today's chime. An id is allowlisted here; the wake listener resolves it again.
            body = {}
            if j.get("id") not in (None, ""):
                sid = j.get("id") if isinstance(j.get("id"), str) else ""
                mod = station_sounds()
                if not sid or mod is None or not mod.file_for(sid):
                    return self.send(400, {"error": "That sound isn't available."})
                body = {"id": sid}
            code, out = wake("POST", "/test-sound", body)
            if code == 202:
                return self.send(200, {"ok": True, "error": None})
            if code == 400:
                return self.send(400, {"error": "That sound isn't available."})
            return self.send(409, {"ok": False, "error": "The station is busy. Try again in a moment."})
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

    # wake words + enrollment. An old client may still send speaker; it is ignored, and speakers.json is never read.
    def api_get_wakewords(self, p, q, j):
        return self.send(200, {"words": wakewords(), "unused": unused_wakewords()})

    def api_put_wakewords(self, p, q, j):
        name = p.split("/")[3] if p.count("/") >= 3 else ""
        if j.get("unlink") is True:
            # Take the word off the bot. Recordings stay; it shows under Unused wake words.
            if name == BUILTIN_WORD:
                return self.send(409, {"error": BUILTIN_LOCKED})
            known = {w["name"]: w for w in wakewords()}
            if known.get(name, {}).get("builtin"):
                return self.send(409, {"error": BUILTIN_LOCKED})
            ov = read_json(path("wake-overrides.json"), {})
            if not isinstance(ov, dict):
                ov = {}
            o = ov.get(name) if isinstance(ov.get(name), dict) else None
            if name not in known and o is None:
                return self.send(404, {"error": "No such wake word."})
            row = dict(o or {})
            who = known.get(name, {}).get("persona")
            if "persona" not in row and persona_ok(str(who or "")):
                row["persona"] = who
            row["enabled"] = False
            row["unused"] = "unlinked"
            row.pop("hidden", None)
            ov[name] = row
            write_json(path("wake-overrides.json"), ov, 0o640)
            wake("POST", "/reload", {})
            return self.send(200, {"ok": True})
        known = {w["name"]: w for w in wakewords()}
        if name not in known:
            return self.send(404, {"error": "No such wake word."})
        if known[name].get("builtin") and j.get("enabled") is False:
            return self.send(409, {"error": BUILTIN_LOCKED})
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

    def api_delete_wakewords(self, p, q, j):
        """Delete one wake word: clips, trained model, config entry. Hey Grok can't be deleted.

        Works for orphaned or untrained entries too. Shipped and bot entries stay hidden so they are not re-added.
        """
        name = urllib.parse.unquote(p.split("/")[3]) if p.count("/") >= 3 else ""
        if name == BUILTIN_WORD:
            return self.send(409, {"error": "Hey Grok is built in and can't be deleted."})
        return self.send(*delete_wake_word(name))

    def api_post_enroll(self, p, q, j):
        if p == "/api/enroll/control":
            op = j.get("op") if isinstance(j, dict) else None
            if op not in ("pause", "resume", "discard"):
                return self.send(400, {"error": "bad op"})
            code, out = wake("POST", "/enroll/control", {"op": op})
            if not isinstance(out, dict):
                out = {}
            if code == 200:
                return self.send(200, {"ok": True, "op": op})
            if code == 409:
                if op == "discard":
                    # Nothing is recording on the listener, so a row that still says recording is stuck
                    # (the mic stopped mid-recording). Clear it so Cancel always gets the user out.
                    doc = read_enroll_status()
                    stuck = [k for k, r in doc.items() if isinstance(r, dict) and r.get("state") == "recording"]
                    for k in stuck:
                        doc[k] = _status_row(k, "idle", "Stopped.", int(time.time()), doc)
                    if stuck:
                        write_enroll_status(doc)
                    return self.send(200, {"ok": True, "op": op, "cleared": len(stuck)})
                return self.send(409, {"error": out.get("error") or "Nothing is recording."})
            return self.send(503, {"error": "Couldn't reach the station's microphone."})
        if p == "/api/enroll/check":
            asked = j.get("persona") if isinstance(j, dict) else None
            err = phrase_problem(j.get("phrase", ""), for_persona=asked if persona_ok(asked) else None)
            if err:
                return self.send(400, {"error": err})
            mid, _words = normalize_phrase(j.get("phrase", ""))
            return self.send(200, {"ok": True, "id": mid, "label": phrase_label(mid)})
        raw, count = j.get("phrase", ""), ENROLL_REPS  # fixed per session; "Train more" adds another session
        direct = str(raw or "").strip().lower()
        # hey_<bot id> can contain a hyphen or a digit; normalize_phrase would strip those.
        mid = direct if wake_name_ok(direct) else normalize_phrase(raw)[0]
        if not wake_name_ok(mid):
            return self.send(400, {"error": phrase_problem(raw) or "Pick a wake word."})
        personas = phrase_personas()
        persona = j.get("persona")
        ov_now = read_json(path("wake-overrides.json"), {})
        ov_mid = ov_now.get(mid) if isinstance(ov_now, dict) and isinstance(ov_now.get(mid), dict) else {}
        # An unused or hidden override is not a new word: a failed re-record must not drop it.
        creating = mid not in personas and not ov_mid
        if creating:
            err = phrase_problem(raw)
            if err:
                return self.send(400, {"error": err})
            if not isinstance(persona, str) or not persona_allowed(persona):
                return self.send(400, {"error": "Pick a bot."})
            save_phrase_persona(mid, persona)
            who = persona
        else:
            who = personas.get(mid) or (ov_mid.get("persona") if persona_ok(str(ov_mid.get("persona") or "")) else None) or "grok"
            relink = (ov_mid.get("unused") in ("deleted", "unlinked") or bool(ov_mid.get("hidden"))
                      or bool(ov_mid.get("forgotten")) or owner_absent(who))
            if relink and isinstance(persona, str) and persona_allowed(persona):
                who = persona
            elif isinstance(persona, str) and persona and persona != who:
                return self.send(400, {"error": duplicate_msg(phrase_label(mid))})
            if mid != BUILTIN_WORD:
                save_phrase_persona(mid, who)
        code, out = wake("POST", "/enroll", {"phrase": mid, "count": count, "topup": bool(j.get("topup")),
                                              "persona": who})
        if not isinstance(out, dict):
            out = {}
        if code != 202 and creating:
            drop_phrase_persona(mid)
        elif code == 202:
            clear_phrase_deleted(mid)
            set_enroll_state(mid, "recording", "Say it on the station.", who,
                             had_model=mid in enrolled_phrase_ids())
        elif not creating:
            set_enroll_state(mid, "failed", out.get("error") or "Couldn't start recording.", who)
        out = {**out, "id": mid}
        return self.send(code, out)

    def api_get_enroll(self, p, q, j):
        if p == "/api/enroll/status":
            doc, st = refresh_enroll_status()
            live = enroll_live(st)
            return self.send(200, {"words": doc, "buttons": buttons_for(doc),
                                   "level_dbfs": live["level_dbfs"], "session": live["session"],
                                   "capture": live["capture"], "enrolling": live["enrolling"]})
        code, st = wake("GET", "/status")
        return self.send(200, enroll_live(st if code == 200 else None))

    def api_get_personas(self, p, q, j):
        doc = read_json(PERSONAS, {})
        items = doc.get("personas", doc) if isinstance(doc, dict) else doc
        words = {w["persona"]: w["name"] for w in wakewords() if not w.get("builtin")}
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
        """Password check. An open grace window is enough unless always=True.

        always=True is for changing the grace period: a correct password is required even when a
        window is already open. A correct password opens the window (no-op when the setting is 0).
        A wrong password does not.
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
            self.send(403, {"error": PASSWORD_BAD})
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
        if p == "/api/bots/imported":
            return self.bots_imported()
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
        if p == "/api/bots/import":
            return self.bots_import()
        if p == "/api/bots/refresh-avatars":
            return self.bots_refresh_avatars()
        if p == "/api/bots/revoke-all":
            return self.bots_revoke_all(j)
        am = re.fullmatch(r"/api/bots/([a-z][a-z0-9-]{1,23})/avatar-request", p)
        if am and slug_ok(am.group(1)):
            return self.bots_avatar_request(am.group(1))
        hm = re.fullmatch(r"/api/bots/([a-z][a-z0-9-]{1,23})/helper-update", p)
        if hm and slug_ok(hm.group(1)):
            return self.bots_helper_update(hm.group(1))
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
        if p == "/api/bots/list-source":
            return self.bots_list_source(j)
        m = BOT_PATH.fullmatch(p)
        if not m:
            return self.send(404, {"error": "not found"})
        slug, kind, tid = m.groups()
        if slug and kind == "webhook" and not tid and slug_ok(slug):
            return self.bots_webhook_put(slug, j)
        if slug and kind == "key" and not tid and slug_ok(slug):
            return self.bots_key_put(slug, j)
        return self.send(404, {"error": "not found"})

    def api_delete_bots(self, p, q, j):
        dm = re.fullmatch(r"/api/bots/imported/(.+)", p)
        if dm:
            return self.bots_dismiss(urllib.parse.unquote(dm.group(1)))
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
        if kind == "key" and not tid:
            return self.bots_key_delete(slug, j)
        if kind == "test" and tid:
            return self.bots_test_delete(slug, tid)
        return self.send(404, {"error": "not found"})

    def _stepup(self):
        return AUTH.stepup_until(self.cookie("gs_session"))

    def bots_list(self):
        drop_stale_guest()
        got = relay_call("GET", "/admin/bots")
        if got is None:
            return self.send(200, {"bots": local_bots(), "voices": [], "default_voice": "",
                                   "message": RELAY_UNCONFIGURED,
                                   "stepup_until": self._stepup(), "station_pub": "", "station_pub_error": True,
                                   "killed": False,
                                   "stats": stats_view(None), "list_source": "", "routed": [],
                                   "imported": empty_imported(),
                                   "station_address": station_base(),
                               "fingerprint": tls_info()["fingerprint"]})
        status, data = got
        if status != 200:
            return self.relay_fail(status, data)
        bots = [b for b in (safe_bot(x) for x in (data.get("bots") or [])) if b]
        pub = data.get("station_pub")
        pub_ok = box_pub_ok(pub)
        if not pub_ok:
            print("admin: relay returned no usable station public key (see the relay log)", flush=True)
        src = data.get("list_source") if slug_ok(data.get("list_source")) else ""
        for b in bots:
            b["main"] = b["slug"] == src  # the main bot gets a star in the list
            b["avatar_v"] = portrait_version(b["slug"])  # cache-busting, so a new picture shows without a reload
        return self.send(200, {"bots": bots, "voices": safe_voices(data.get("voices")),
                               "default_voice": data.get("default_voice") if voice_ok(data.get("default_voice")) else "",
                               "stepup_until": self._stepup(),
                               "station_pub": "".join(pub.split()) if pub_ok else "",
                               "station_pub_error": not pub_ok,
                               "killed": data.get("killed") is True, "stats": stats_view(data.get("stats")),
                               "list_source": src, "routed": safe_routed(data.get("routed")),
                               "imported": safe_imported(data.get("imported")),
                               "station_address": station_base(),
                               "fingerprint": tls_info()["fingerprint"]})

    def bots_imported(self):
        got = relay_call("GET", "/admin/bots/imported")
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200:
            return self.relay_fail(status, data)
        src = data.get("list_source") if isinstance(data, dict) and slug_ok(data.get("list_source")) else ""
        return self.send(200, {"list_source": src, "routed": safe_routed(data.get("routed") if isinstance(data, dict) else None),
                               "imported": safe_imported(data.get("imported") if isinstance(data, dict) else None)})

    def bots_import(self):
        got = relay_call("POST", "/admin/bots/import", {"actor": RELAY_ACTOR}, timeout=40)
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200 or not isinstance(data, dict) or data.get("ok") is not True:
            return self.relay_fail(status, data)
        return self.send(200, {"ok": True, "n": _count(data, "n"), "new": _count(data, "new"),
                               "missing": _count(data, "missing"), "dropped": _count(data, "dropped")})

    def bots_dismiss(self, key):
        if not isinstance(key, str):
            return self.send(400, {"error": "Not on the list."})
        key = key.casefold()
        if _IMPORTED_KEY.fullmatch(key) is None:
            return self.send(400, {"error": "Not on the list."})
        got = relay_call("DELETE", "/admin/bots/imported/" + urllib.parse.quote(key, safe=""), {"actor": RELAY_ACTOR})
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200 or not isinstance(data, dict) or data.get("ok") is not True:
            return self.relay_fail(status, data)
        return self.send(200, {"ok": True})

    def bots_list_source(self, j):
        raw = j.get("list_source") if isinstance(j, dict) else None
        if raw in ("", None):
            body = {"list_source": "", "actor": RELAY_ACTOR}
        elif slug_ok(raw):
            body = {"list_source": raw, "actor": RELAY_ACTOR}
        else:
            return self.send(400, {"error": "Pick a bot."})
        got = relay_call("PUT", "/admin/bots/list-source", body)
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200 or not isinstance(data, dict) or data.get("ok") is not True:
            return self.relay_fail(status, data)
        src = data.get("list_source") if slug_ok(data.get("list_source")) else ""
        return self.send(200, {"ok": True, "list_source": src})

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
        if status == 502:
            return self.send(502, {"error": "Couldn't reach the bot."})  # the bot's webhook, not the relay
        if status not in (200, 202) or (isinstance(data, dict) and data.get("ok") is False):
            return self.relay_fail(status, data)
        schedule_persona_sync(30)
        return self.send(202, {"ok": True})

    def _station_base(self):
        return station_base()

    def _finish_inputs(self):
        got = relay_call("GET", "/admin/bots")
        if got is None or got[0] != 200:
            return None
        data = got[1]
        bots = [b for b in (safe_bot(x) for x in (data.get("bots") or [])) if b]
        pub = data.get("station_pub")
        pub = "".join(pub.split()) if box_pub_ok(pub) else ""
        return bots, self._station_base(), tls_info()["fingerprint"], pub

    def api_get_finish_update(self, p, q, j):
        if p != "/api/finish-update":
            return self.send(404, {"error": "not found"})
        got = self._finish_inputs()
        if got is None:
            return self.bots_unconfigured()
        return self.send(200, finish_rows(*got))

    def api_post_finish_update(self, p, q, j):
        if not isinstance(j, dict):
            return self.send(400, {"error": "bad request"})
        if p == "/api/finish-update/updater":
            key, slug = j.get("computer"), j.get("slug")
            if not (isinstance(key, str) and re.fullmatch(r"(c:[0-9a-f]{16}|h:[A-Za-z0-9.-]{1,63}|bot)", key)
                    and slug_ok(slug)):
                return self.send(400, {"error": "bad request"})
            doc = read_finish(); doc["updaters"][key] = slug
            write_json(path(FINISH), doc)
            return self.send(200, {"ok": True})
        if p not in ("/api/finish-update/send", "/api/finish-update/copied"):
            return self.send(404, {"error": "not found"})
        slug, sha = j.get("slug"), j.get("message_sha")
        if not slug_ok(slug) or not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{16}", sha)):
            return self.send(400, {"error": "bad request"})
        got = self._finish_inputs()
        if got is None:
            return self.bots_unconfigured()
        view = finish_rows(*got)
        row = next((r for g in view["computers"] for r in g["bots"] if r["slug"] == slug), None)
        if row is None:
            return self.send(404, {"error": "not found"})
        if row["error"] or row["instr"] != sha:
            return self.send(409, {"error": "The instructions changed. Reopen Finish update."})
        if p.endswith("/send") and row["state"] == "waiting":
            mins = max(1, -(-int(row.get("wait_left") or 0) // 60))
            return self.send(409, {"error": "Still waiting for %s to reply. Try again in %d min." % (row["name"], mins),
                                   "wait_left": row.get("wait_left") or 0})
        doc = read_finish()
        if p.endswith("/copied"):
            doc["copied"][slug] = {"instr": sha, "at": int(time.time())}
            write_json(path(FINISH), doc)
            audit(slug, "finish-copy")
            return self.send(200, {"ok": True})
        if not re.fullmatch(r"[0-9a-f]{40}", view["commit"] or ""):
            return self.send(500, {"error": "This release has no update commit pinned."})
        r = relay_call("POST", "/admin/bots/" + slug + "/update",
                       {"actor": RELAY_ACTOR, "release": view["release"], "commit": view["commit"]})
        if r is None:
            return self.bots_unconfigured()
        status, data = r
        if status == 429:
            return self.send(429, {"error": "Asked a few minutes ago. Wait, then try again."})
        if status == 502:
            return self.send(502, {"error": "Couldn't reach the bot."})
        if status not in (200, 202):
            return self.relay_fail(status, data)
        doc = read_finish()
        doc["sent"][slug] = {"instr": sha, "release": view["release"], "at": int(time.time())}
        write_json(path(FINISH), doc)
        audit(slug, "finish-send")
        return self.send(202, {"ok": True})

    def bots_helper_update(self, slug):
        """Ask a home bot to update its helper. The webhook carries only kind update_helper: no command, no URL."""
        got = relay_call("POST", "/admin/bots/" + slug + "/helper-update", {"actor": RELAY_ACTOR})
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status == 429:
            return self.send(429, {"error": "Asked a few minutes ago. Wait, then try again."})
        if status == 502:
            return self.send(502, {"error": "Couldn't reach the bot."})
        if status not in (200, 202) or (isinstance(data, dict) and data.get("ok") is False):
            return self.relay_fail(status, data)
        audit(slug, "helper-update")
        return self.send(202, {"ok": True})

    def bots_add(self, j):
        slug, name = j.get("slug"), clean_name(j.get("name"))
        problem = bot_id_problem(slug)
        if problem:
            return self.send(400, {"error": problem})
        if not name:
            return self.send(400, {"error": "Type a name."})
        raw = j.get("voice", None) if isinstance(j, dict) else None
        voice = None
        if raw is not None:
            if not isinstance(raw, str):
                return self.send(400, {"error": "Pick a voice."})
            voice = raw.strip()
            if voice == "":
                voice = None
            elif not voice_ok(voice):
                return self.send(400, {"error": "Pick a voice."})
        mode = None
        if isinstance(j, dict) and "key_mode" in j:
            mode = j.get("key_mode")
            if mode not in ("cloud", "home"):
                return self.send(400, {"error": "Pick where the key lives."})
        host = clean_host(j.get("reply_host", "")) if isinstance(j, dict) else ""
        if host is None:
            return self.send(400, {"error": HOST_BAD})
        if not self.need_password(j):
            return
        body = {"slug": slug, "name": name, "actor": RELAY_ACTOR}
        if host:
            body["reply_host"] = host
        if voice:
            body["voice"] = voice
        if mode in ("cloud", "home"):
            body["key_mode"] = mode
        got = relay_call("POST", "/admin/bots", body)
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
        """Rename, change voice, change where the key lives, or a combination.

        key_mode is optional. Sending it alone is enough. An invalid mode is rejected before the password.
        """
        if not isinstance(j, dict):
            return self.send(400, {"error": "Type a name."})
        has_name, has_voice, has_mode = "name" in j, "voice" in j, "key_mode" in j
        has_host = "reply_host" in j
        if not has_name and not has_voice and not has_mode and not has_host:
            return self.send(400, {"error": "Type a name."})
        host = clean_host(j.get("reply_host")) if has_host else ""
        if host is None:
            return self.send(400, {"error": HOST_BAD})
        name = clean_name(j.get("name")) if has_name else None
        if has_name and not name:
            return self.send(400, {"error": "Type a name."})
        voice = j.get("voice") if has_voice else None
        if has_voice:
            # An empty voice means "station default". The relay stores that default voice.
            if not isinstance(voice, str):
                return self.send(400, {"error": "Pick a voice."})
            voice = voice.strip()
            if voice and not voice_ok(voice):
                return self.send(400, {"error": "Pick a voice."})
        mode = None
        if has_mode:
            mode = j.get("key_mode")
            if mode not in ("cloud", "home"):
                return self.send(400, {"error": "Pick where the key lives."})
        # Only the computer name is a label, not access, so it doesn't ask for the password again.
        if (has_name or has_voice or has_mode) and not self.need_password(j):
            return
        body = {"actor": RELAY_ACTOR}
        if has_host:
            body["reply_host"] = host
        if has_name:
            body["name"] = name
        if has_voice:
            body["voice"] = voice
        if has_mode:
            body["key_mode"] = mode
        got = relay_call("PATCH", "/admin/bots/" + slug, body)
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200 or not isinstance(data, dict) or data.get("ok") is False:
            return self.relay_fail(status, data, (j.get("password"),))
        if has_name:
            audit(slug, "rename")
        if has_voice:
            audit(slug, "voice")
        out = {"ok": True}
        if data.get("needs_key") is True:
            out["needs_key"] = True
            audit(slug, "key-mode")
        return self.send(200, out)

    def bots_remove(self, slug, j):
        if not self.need_password(j):
            return
        got = relay_call("DELETE", "/admin/bots/" + slug, {"actor": RELAY_ACTOR})
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200 or data.get("ok") is False:
            return self.relay_fail(status, data, (j.get("password"),))
        audit(slug, "remove")
        try:
            forget_finish(slug)
        except OSError:
            pass  # the bot is already gone; a leftover finish row is not counted
        _gone, failed = delete_bot_phrases(slug)
        body = {"ok": True}
        if failed:
            body["wake_word_failures"] = failed  # the bot is gone; these words are still on the station
        return self.send(200, body)

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

    def bots_key_put(self, slug, j):
        """Pin the bot's Ed25519 verify key. The private key is never sent here."""
        key = j.get("public_key") if isinstance(j, dict) else None
        if not box_pub_ok(key):
            return self.send(400, {"error": "That public key isn't valid."})
        if not self.need_password(j):
            return
        got = relay_call("PUT", "/admin/bots/" + slug + "/key",
                         {"public_key": "".join(key.split()), "actor": RELAY_ACTOR})
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200 or data.get("ok") is False:
            return self.relay_fail(status, data, (j.get("password"), key))
        audit(slug, "pair")
        return self.send(200, {"ok": True})

    def bots_key_delete(self, slug, j):
        """Drop one pinned key. Replies from this bot are rejected until it is paired again."""
        if not self.need_password(j):
            return
        got = relay_call("DELETE", "/admin/bots/" + slug + "/key", {"actor": RELAY_ACTOR})
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200 or data.get("ok") is False:
            return self.relay_fail(status, data, (j.get("password"),))
        audit(slug, "revoke")
        return self.send(200, {"ok": True})

    def bots_revoke_all(self, j):
        """Drop every pinned key and reject every reply until a key is paired again."""
        if not self.need_password(j):
            return
        got = relay_call("POST", "/admin/bots/revoke-all", {"actor": RELAY_ACTOR})
        if got is None:
            return self.bots_unconfigured()
        status, data = got
        if status != 200 or data.get("ok") is False:
            return self.relay_fail(status, data, (j.get("password"),))
        audit("-", "revoke-all")
        return self.send(200, {"ok": True})

    def bots_webhook_delete(self, slug, j):
        if not self.need_password(j):
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
        if j.get("then_avatar") is True and "url" not in body and "key" not in body:
            body["then_avatar"] = True  # post-add check: the relay asks for the picture after a passing test
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
                               "tls": {**tls_info(), "url": admin_url()}, "password_grace_min": password_grace_min()})

    def api_put_system(self, p, q, j):
        if p != "/api/system/password-grace":
            return self.send(404, {"error": "not found"})
        if not isinstance(j, dict):
            return self.send(400, {"error": "Pick a grace period from 0 to 120 minutes."})
        n = j.get("password_grace_min")
        if isinstance(n, bool) or not isinstance(n, int) or n < 0 or n > GRACE_MAX:
            return self.send(400, {"error": "Pick a grace period from 0 to 120 minutes."})
        old = password_grace_min()
        if not self.need_password(j, always=True):
            return
        try:
            store_password_grace_min(n)
        except OSError:
            return self.send(500, {"error": "Couldn't save that."})
        token = self.cookie("gs_session")
        if n < old:
            AUTH.clear_stepups()
            until = None
        elif n > old:
            until = AUTH.open_stepup(token)
        else:
            until = AUTH.stepup_until(token)
        return self.send(200, {"ok": True, "password_grace_min": n, "stepup_until": until})

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

    # -- voice status: the relay runs on this station; voice is gated only on the local xAI key
    def api_get_relay(self, p, q, j):
        if p != "/api/relay":
            return self.send(404, {"error": "not found"})
        return self.send(200, {"state": "ready" if xai_key_set() else "no-key", "local": True, "audit": recent_audit(20)})

    def api_post_relay(self, p, q, j):
        return self.send(404, {"error": "not found"})

    # -- updates
    def api_get_updates(self, p, q, j):
        if p == "/api/updates/log":
            return self.send(200, update_log())
        if p != "/api/updates":
            return self.send(404, {"error": "not found"})
        return self.send(200, updates_view())

    def api_post_updates(self, p, q, j):
        # Check and apply use the signed-in session and the CSRF header only. No second password,
        # and no URL or ref from the caller: the unit always updates the fixed GitHub main path.
        if p == "/api/updates/check":
            run_update_check()
            return self.send(200, updates_view())
        if p == "/api/updates/apply":
            if busy() and not (isinstance(j, dict) and j.get("force")):
                return self.send(409, {"error": "A conversation or enrollment is running.", "can_force": True})
            if update_job()["state"] == "running":
                return self.send(409, {"error": "An update is already running."})
            code, body = start_update()
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
            doc = _settings_doc()
            doc["autocheck"] = on
            write_json(_settings_path(), doc)
        except OSError:
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


def main():
    os.makedirs(STATE_DIR, mode=0o750, exist_ok=True)
    drop_stale_guest()
    AUTH.publish()
    print(f"admin on :{PORT} setup_required={AUTH.setup_required()} mode={AUTH.mode()}", flush=True)
    threading.Thread(target=hotspot_loop, daemon=True).start()
    threading.Thread(target=update_check_loop, daemon=True).start()

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
