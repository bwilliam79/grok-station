#!/usr/bin/env python3
"""grok-station admin web page (LAN only), per Noor's ADMIN-UI.md.

No default password: until an admin password is set, the station shows a one-time 6-character setup code (3+3, no
0/O/1/I), new on each boot until used, and voice stays off. Password >= 10 characters, stored as a scrypt hash.
Secrets are write-only (last 4 shown). 5 wrong tries (code or password) lock sign-in for 15 min. Privileged changes go
through grok-station-helper (root, fixed subcommands, validated input) via sudo. Speaker names stay on this Pi.
Stdlib only.
"""
import base64, hashlib, hmac, http.cookies, ipaddress, json, os, re, secrets, shutil, subprocess, threading, time
import urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

E = os.environ.get
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(E("ADMIN_PORT", "443"))  # HTTPS only; the app is never served over plain HTTP
HTTP_PORT = int(E("ADMIN_HTTP_PORT", "80"))  # 301 to https and nothing else (0 = off)
TLS_DIR = E("ADMIN_TLS_DIR", "/var/lib/grok-station/tls")  # made on the station by `grok-station tls-cert`
STATE_DIR = E("ADMIN_STATE_DIR", "/var/lib/grok-station/admin")
RUN_DIR = E("ADMIN_RUN_DIR", "/run/grok-station-admin")  # setup.json lives here (dir 0750, group grok)
WEB = E("ADMIN_WEB_DIR", os.path.join(HERE, "web"))
HELPER = E("ADMIN_HELPER", "sudo -n /usr/local/lib/grok-station/grok-station-helper").split()
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
SESSION_TTL, SETUP_TTL = 12 * 3600, 900
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
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
        self.sessions[hashlib.sha256(t.encode()).hexdigest()] = now() + SESSION_TTL
        return t

    def valid(self, token):
        if not token or self.setup_required():
            return False
        k = hashlib.sha256(token.encode()).hexdigest()
        exp = self.sessions.get(k)
        if not exp or exp < now():
            self.sessions.pop(k, None)
            return False
        return True

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
def wakewords():
    cfg = read_json(WAKE_CONFIG, {})
    ov = read_json(path("wake-overrides.json"), {})
    enrolled = {w.get("name"): w for w in read_json(ENROLLED, {}).get("words", []) if isinstance(w, dict)}
    models = read_json(path("models.json"), {})  # Pi-only: phrase -> {speakers: [ids], trained_at, clips}
    out = []
    for w in cfg.get("words", []) + cfg.get("enroll", []):
        n = w.get("name")
        if not n:
            continue
        o = ov.get(n, {})
        e = enrolled.get(n, {})
        has_model = n in enrolled or os.path.isfile(os.path.join(cfg.get("models_dir", ""), n + ".onnx"))
        out.append({"name": n, "persona": w.get("persona", "grok"), "enabled": o.get("enabled", True) and has_model,
                    "has_model": has_model, "sensitivity": round(1 - float(o.get("threshold", e.get("threshold", w.get("threshold", 0.5)))), 2),
                    "provisional": bool(w.get("provisional")), "enrolled": n in enrolled, "metrics": e.get("metrics"),
                    "trained_on": models.get(n)})
    fb = cfg.get("fallback")
    if fb:
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
PHRASE_RE = re.compile(r"^hey_[a-z]{2,16}$")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/admin.css": ("admin.css", "text/css; charset=utf-8")}


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

    def guard(self, write):
        if not lan_ok(self.client_address[0]):
            self.send(403, {"error": "LAN only"})
            return False
        if write:  # CSRF: JSON only, same-origin header (forms can't set it), SameSite=Strict cookies
            if self.headers.get("X-GS-Admin") != "1" or not self.headers.get("Content-Type", "").startswith("application/json"):
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

    def route(self, method):
        u = urllib.parse.urlsplit(self.path)
        p, q = u.path, dict(urllib.parse.parse_qsl(u.query))
        if not self.guard(method != "GET"):
            return
        try:
            if method == "GET" and (p in STATIC or p.startswith("/assets/")):
                return self.static(p)
            if not p.startswith("/api/"):
                return self.send(404, {"error": "not found"})
            j = self.body() if method in ("POST", "PUT") else {}
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
        if p.startswith("/assets/"):
            name = p[8:]
            if not re.fullmatch(r"[a-z0-9_-]{1,32}\.(png|svg|ttf)", name):
                return self.send(404, {"error": "not found"})
            f = os.path.join(WEB, "assets", name)
            ctype = {"png": "image/png", "svg": "image/svg+xml", "ttf": "font/ttf"}[name.rsplit(".", 1)[1]]
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
        return self.send(200, {"setup_required": AUTH.setup_required(), "mode": AUTH.mode(), "signed_in": self.authed(),
                               "locked_s": AUTH.locked(), "host": hostname() + ".local", "version": version(),
                               "wizard": read_json(path("wizard.json"), {"step": 2, "done": False}) if self.authed() else None})

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
        return self.send(200 if ok else 503, out if ok else {"error": "Can't read settings."})

    def api_put_settings(self, p, q, j):
        sec = p.split("/")[3] if p.count("/") >= 3 else ""
        if sec not in ("display", "time", "audio"):
            return self.send(404, {"error": "not found"})
        ok, out = helper("settings-set", sec, stdin=j)
        if not ok:
            return self.send(400, {"error": out.get("error", "Couldn't save.")})
        if sec == "audio" or out.get("restart_wake"):
            if not busy():
                helper("restart", "wake")
            else:
                out["pending"] = "Applies after the current conversation or enrollment."
        return self.send(200, {"ok": True, **out})

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
        return self.send(200, {"results": [{"name": x.get("name"), "admin1": x.get("admin1"), "country": x.get("country_code"),
                                            "lat": round(x["latitude"], 3), "lon": round(x["longitude"], 3),
                                            "timezone": x.get("timezone")} for x in res if "latitude" in x]})

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

    # secrets (write-only)
    def api_get_secrets(self, p, q, j):
        m = secrets_meta()
        ok, out = helper("secret-status")
        live = out.get("secrets", {}) if ok else {}
        res = {}
        for name in ("relay_token",):
            e = dict(m.get(name, {}))
            if live.get(name):
                e.update(set=live[name].get("set", False), last4=live[name].get("last4"))
            res[name] = e
        env = env_file("wake")
        res["relay_url"] = {"value": env.get("GROK_RELAY_URL", "")}
        res["xai_key"] = {"where": "Stored on the voice service computer, never on the station.",
                          "set": None, "replace": False}
        return self.send(200, res)

    def api_post_secrets(self, p, q, j):
        parts = p.split("/")  # /api/secrets/<name>[/test]
        name = parts[3] if len(parts) > 3 else ""
        if name == "relay_token":
            val = j.get("value")
            url = j.get("url")
            if val is not None and (not isinstance(val, str) or not 16 <= len(val) <= 200 or not re.fullmatch(r"[\x21-\x7e]+", val)):
                return self.send(400, {"error": "That doesn't look like a station token."})
            if url is not None and not re.fullmatch(r"wss://[A-Za-z0-9.-]{1,253}(:\d{1,5})?(/[A-Za-z0-9._~/-]*)?", str(url)):
                return self.send(400, {"error": "The relay address must start with wss://."})
            test_only = len(parts) > 4 and parts[4] == "test"
            ok, out = helper("relay-test", stdin={"token": val, "url": url}, timeout=20)
            note_secret(name, test=ok)
            if not ok:
                return self.send(400, {"ok": False, "error": out.get("error", "Couldn't reach the voice service."), "checks": out.get("checks")})
            if test_only or (val is None and url is None):
                return self.send(200, {"ok": True, "checks": out.get("checks")})
            ok2, out2 = helper("relay-save", stdin={"token": val, "url": url})
            if not ok2:
                return self.send(500, {"error": "Tested OK but couldn't save."})
            if val:
                note_secret(name, value=val)
            if not busy():
                helper("restart", "wake")
            return self.send(200, {"ok": True, "checks": out.get("checks"), "saved": True})
        if name == "xai_key":
            return self.send(409, {"error": "The xAI key lives on the voice service computer. Change it there (relay secrets)."})
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
        if name not in known or known[name].get("backup"):
            return self.send(404, {"error": "No such wake word."})
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
        phrase, count = str(j.get("phrase", "")), j.get("count", 10)
        spk = str(j.get("speaker", ""))
        if not PHRASE_RE.fullmatch(phrase) or not isinstance(count, int) or not 10 <= count <= 40:
            return self.send(400, {"error": "Pick a wake word and 10 to 40 clips."})
        if spk and spk not in speakers():
            return self.send(400, {"error": "Pick a speaker."})
        code, out = wake("POST", "/enroll", {"phrase": phrase, "count": count, "topup": bool(j.get("topup"))})
        if code == 202 and spk:  # speaker name -> opaque id stays on the Pi; uploads carry no name
            m = read_json(path("models.json"), {})
            e = m.setdefault(phrase, {"speakers": []})
            if spk not in e["speakers"]:
                e["speakers"].append(spk)
            e["last_session_at"] = int(now())
            write_json(path("models.json"), m, 0o600)
        return self.send(code, out)

    def api_get_enroll(self, p, q, j):
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

    # T1 voice notes: guest mode switch (read by the wake listener, sent to the relay with each session)
    def api_get_t1(self, p, q, j):
        d = read_json(path("t1.json"), {})
        return self.send(200, {"guest": d.get("guest") is True, "changed_at": d.get("changed_at")})

    def api_put_t1(self, p, q, j):
        if not isinstance(j.get("guest"), bool):
            return self.send(400, {"error": "bad value"})
        write_json(path("t1.json"), {"guest": j["guest"], "changed_at": int(now())}, 0o640)
        return self.send(200, {"ok": True, "guest": j["guest"]})

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
            try:
                req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/commits/main",
                                             headers={"Accept": "application/vnd.github+json", "User-Agent": "grok-station"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    c = json.loads(r.read(500000))
                latest = {"sha": c["sha"], "date": c["commit"]["committer"]["date"], "message": c["commit"]["message"].splitlines()[0][:120]}
            except Exception:
                return self.send(503, {"error": "Can't check for updates right now."})
            cur = version()
            return self.send(200, {"current": cur, "latest": latest, "update_ready": not latest["sha"].startswith(cur.split("-")[-1][:7]) if cur != "dev" else True})
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
        if p == "/api/system/update":
            sha = str(j.get("sha", ""))
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                return self.send(400, {"error": "bad version"})
            if busy():
                return self.send(409, {"error": "A conversation or enrollment is running. Try again when it ends."})
            ok, out = helper("update", sha, timeout=300)
            return self.send(202 if ok else 500, {"ok": ok} if ok else {"error": out.get("error", "Update failed; the old version is still running.")})
        return self.send(404, {"error": "not found"})


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
