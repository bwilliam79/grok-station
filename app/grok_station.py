#!/usr/bin/env python3
"""grok-station: LAN HTTP listener + fullscreen framebuffer UI for a Raspberry Pi display.

POST /show {"text": "...", "from": "Grok", "heard": "..."} with "Authorization: Bearer $GROK_STATION_TOKEN".
Screens: idle (clock, date, weather), message/session, night clock, weather offline.
A tool card (weather, time, unit conversion) sits in the session text column and holds 4 s after the
session ends. Timers are a bottom strip of chips on the idle clock and in sessions; the strip rect
repaints once a second, and the wake listener pushes the list only when it changes.
Renders with pygame off-screen into a 16bpp surface written straight to /dev/fb0.
`grok_station.py --demo OUTDIR [WxH]` renders every screen to PNGs (no fb, no network).
The thinking ring spins by repainting only its own rectangle; the clock colon is steady (GROK_STATION_BLINK=1 blinks it).
Small status icons (network, relay, mic) sit in the top-right corner. A daemon thread polls them; --demo does not.
"""
import hmac, ipaddress, json, math, os, re, sys, threading, time, unicodedata, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from avatars import default_avatar_index
except ImportError:  # installed next to this file once the helper is shipped; the formula stays the same
    import hashlib

    def default_avatar_index(slug):
        return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % 18 + 1

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")  # no SDL display; we write /dev/fb0 directly
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame

E = os.environ.get
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = E("GROK_STATION_TOKEN", "")
PORT = int(E("GROK_STATION_PORT", "8787"))
SNAP = E("GROK_STATION_SNAPSHOT", "/run/grok-station/last.png")
FB = E("GROK_STATION_FB", "/dev/fb0")
ASSETS = E("GROK_STATION_ASSETS") or next((p for p in (os.path.join(HERE, "assets"), os.path.join(HERE, "..", "assets")) if os.path.isdir(p)), "assets")
PORTRAIT_DIRS = [os.path.join(ASSETS, "portraits"), E("GROK_STATION_PORTRAITS", "/opt/grok-station/portraits")]
AVATAR_DIR = os.path.join(ASSETS, "avatars")
PORTRAIT_MAX = 2 << 20  # same cap the relay will serve; a larger file is not a usable photo
HOLD = float(E("GROK_STATION_HOLD", "60"))            # seconds a message stays up after the last one
NIGHT = E("GROK_STATION_NIGHT", "22-6")               # night clock hours, start-end (24h), "" disables
LAT, LON = E("GROK_STATION_LAT", ""), E("GROK_STATION_LON", "")  # no default: weather is off until both are set
UNITS = E("GROK_STATION_UNITS", "fahrenheit")         # or celsius
WX_CACHE = E("GROK_STATION_WEATHER_CACHE", "/var/cache/grok-station/weather.json")
WX_EVERY, WX_RETRY, WX_STALE = 900, 300, 3600         # refresh, retry after failure, offline after
FADE = E("GROK_STATION_FADE", "1") != "0"
MAX_TEXT, MAX_FROM, MAX_HEARD = 500, 40, 300
SHOW, WEATHER, STATE, STATUS_EV = pygame.USEREVENT + 1, pygame.USEREVENT + 2, pygame.USEREVENT + 3, pygame.USEREVENT + 4
LISTEN_HOLD = float(E("GROK_STATION_LISTEN_HOLD", "12"))  # seconds the listening screen stays up
REPLY_HOLD = float(E("GROK_STATION_REPLY_HOLD", "15"))   # seconds a spoken reply stays up after playback
CARD_HOLD = 4  # a finished session that showed a card stays up this long; a notice uses VOICE_HOLDS["notice"]
VOICE_HOLDS = {"listening": LISTEN_HOLD, "thinking": 20, "speaking": 45, "error": 5, "notice": 10, "enroll": 30, "confirm": 30}  # safety timeouts per state
VOICE_TEXT = {"listening": "Go ahead, I’m listening.", "thinking": "Thinking…", "speaking": "…", "error": "Can’t reach Grok right now.", "notice": "Didn’t catch that.", "enroll": "Get ready…", "confirm": "…"}
WAKE_CONTROL = E("GROK_STATION_WAKE_CONTROL", "http://127.0.0.1:8789/enroll")  # wake listener's local enroll endpoint
STATUS_DIR = os.path.join(ASSETS, "status")
VOICE_NAME = E("GROK_STATION_VOICE_NAME", "Grok")        # portrait/name shown while listening
SPIN_PERIOD = 1.2                                         # seconds for one thinking-ring revolution
PULSE_PERIOD = 1.0                                        # seconds per listening-ring pulse (~1 Hz)
ANIM = {"thinking": SPIN_PERIOD, "listening": PULSE_PERIOD}  # animated ring states -> loop period
PERSONAS_FILE = os.path.join(PORTRAIT_DIRS[1], "personas.json")  # cached by the wake client from the relay
_personas = {"mtime": None, "names": {}}


def name_key(s):
    """Comparison form of a name: NFKC, format/control characters (zero-width, bidi, soft hyphen) removed, casefolded,
    letters and digits only. "Grok.", "H a n k!", "Hank\u200b" and "Ha\u00adnk" all reduce to the plain name."""
    s = "".join(c for c in unicodedata.normalize("NFKC", str(s)) if unicodedata.category(c) not in ("Cf", "Cc"))
    return "".join(c for c in unicodedata.normalize("NFKC", s.casefold()) if c.isalnum())


def persona_names():
    """casefolded name/slug/alias -> (display name, slug) for every voice persona, Grok included."""
    try:
        mt = os.stat(PERSONAS_FILE).st_mtime
    except OSError:
        mt = None
    if mt != _personas["mtime"]:
        names = {}
        try:
            with open(PERSONAS_FILE) as f:
                for p in json.load(f).get("personas", []):
                    slug, name = str(p.get("slug", "")), " ".join(str(p.get("name", "")).split())[:MAX_FROM]
                    if re.fullmatch(r"[a-z]{2,16}", slug) and name:
                        for k in (slug, name, *[str(a) for a in p.get("aliases", [])]):
                            if name_key(k):
                                names[name_key(k)] = (name, slug)
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        _personas.update(mtime=mt, names=names)
    return {name_key(VOICE_NAME): (VOICE_NAME, "grok"), "grok": (VOICE_NAME, "grok"), **_personas["names"]}


def hexc(s):
    return pygame.Color("#" + s)


# Palette (Noor's "Hearth" spec). Listening/thinking colors kept for the future voice states.
BG, SURFACE, SURFACE2, RULE = hexc("0D0B09"), hexc("17130F"), hexc("211B16"), hexc("2E2620")
TEXT, MUTED, FAINT = hexc("F5EFE6"), hexc("A89B8C"), hexc("6F655A")
LISTENING, THINKING, SPEAKING, AMBER = hexc("3FD3B6"), hexc("A99CFF"), hexc("FFE6BF"), hexc("F5C04A")
RED = hexc("FF5A52")  # the single red (design.md): mic off only on the display; problems use text on a quiet ring
SUN = hexc("F2C46A")
NIGHT_TEXT, NIGHT_DIM, BLACK = hexc("9A7752"), hexc("5E4934"), hexc("000000")

_fonts = {}
MONO_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
SETUP_FILE = E("GROK_SETUP_FILE", "/run/grok-station-admin/setup.json")  # written by the admin page while setup is pending
WARM_WHITE = hexc("FFF6E8")


def font(kind, px):
    files = {"display": "InterTight-SemiBold.ttf", "night": "InterTight-Medium.ttf",
             "body": "Inter-Medium.ttf", "italic": "Inter-MediumItalic.ttf", "mono": "InterTight-SemiBold.ttf"}
    key = (kind, max(14 if kind == "mono" else 8, int(px)))
    if key not in _fonts:
        if len(_fonts) > 40:
            _fonts.clear()
        path = os.path.join(ASSETS, "fonts", files[kind])
        if kind == "mono":  # system mono if present (setup code); Inter Tight otherwise (the code alphabet is unambiguous)
            path = next((p for p in MONO_FONTS if os.path.exists(p)), path)
        _fonts[key] = pygame.font.Font(path if os.path.exists(path) else None, key[1])
    return _fonts[key]


# ---------------------------------------------------------------- weather (Open-Meteo, no key)
WMO = {0: ("Clear", "clear"), 1: ("Mostly clear", "partly"), 2: ("Partly cloudy", "partly"), 3: ("Cloudy", "cloud"),
       45: ("Fog", "fog"), 48: ("Fog", "fog"), 51: ("Drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Drizzle", "rain"),
       56: ("Freezing drizzle", "rain"), 57: ("Freezing drizzle", "rain"), 61: ("Light rain", "rain"), 63: ("Rain", "rain"),
       65: ("Heavy rain", "rain"), 66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"), 71: ("Light snow", "snow"),
       73: ("Snow", "snow"), 75: ("Heavy snow", "snow"), 77: ("Snow grains", "snow"), 80: ("Showers", "rain"),
       81: ("Showers", "rain"), 82: ("Heavy showers", "rain"), 85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
       95: ("Thunderstorms", "storm"), 96: ("Thunderstorms", "storm"), 99: ("Thunderstorms", "storm")}


class Weather:
    def __init__(self):
        self.data, self.ok_at, self.started = None, 0.0, time.time()
        try:
            with open(WX_CACHE) as f:
                c = json.load(f)
            self.data, self.ok_at = c["data"], float(c["ok_at"])
        except Exception:
            pass

    def offline(self, now=None):
        if not (LAT and LON):
            return False  # weather not configured: show nothing rather than an offline badge
        now = now or time.time()
        return now - (self.ok_at or self.started) > WX_STALE

    def fetch(self):
        url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&current=temperature_2m,weather_code,is_day"
               "&daily=temperature_2m_max,temperature_2m_min&temperature_unit=%s&timezone=auto&forecast_days=1") % (LAT, LON, UNITS)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "grok-station"}), timeout=15) as r:
            j = json.load(r)
        cur, day = j["current"], j["daily"]
        return {"temp": round(cur["temperature_2m"]), "code": int(cur["weather_code"]), "day": bool(cur.get("is_day", 1)),
                "hi": round(day["temperature_2m_max"][0]), "lo": round(day["temperature_2m_min"][0])}

    def loop(self):
        if not (LAT and LON):
            print("weather off: set GROK_STATION_LAT and GROK_STATION_LON", flush=True)
            return
        while True:
            try:
                self.data, self.ok_at = self.fetch(), time.time()
                try:
                    os.makedirs(os.path.dirname(WX_CACHE), exist_ok=True)
                    with open(WX_CACHE, "w") as f:
                        json.dump({"data": self.data, "ok_at": self.ok_at}, f)
                except OSError:
                    pass
                pygame.event.post(pygame.event.Event(WEATHER))
                time.sleep(WX_EVERY)
            except Exception as e:
                print(f"weather fetch failed: {type(e).__name__}", flush=True)
                time.sleep(WX_RETRY)


# ---------------------------------------------------------------- HTTP listener
class Handler(BaseHTTPRequestHandler):
    server_version = "grok-station"

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._reply(200, {"ok": True})
        if self.path == "/status":
            return self._status()
        return self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/state":
            return self._state()
        if not ipaddress.ip_address(self.client_address[0].removeprefix("::ffff:")).is_private:
            return self._reply(403, {"error": "lan only"})
        if self.path not in ("/show", "/enroll"):
            return self._reply(404, {"error": "not found"})
        auth = self.headers.get("Authorization", "")
        if not TOKEN or not hmac.compare_digest(auth.encode(), f"Bearer {TOKEN}".encode()):
            return self._reply(401, {"error": "unauthorized"})
        if self.path == "/enroll":
            return self._enroll()
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 8192:
                return self._reply(413, {"error": "too large"})
            j = json.loads(self.rfile.read(n) or b"{}")
            clean = lambda k, m: " ".join(str(j.get(k) or "").split())[:m] if k != "text" else str(j.get(k) or "").strip()[:m]
            text, who, heard = clean("text", MAX_TEXT), clean("from", MAX_FROM), clean("heard", MAX_HEARD)
        except Exception:
            return self._reply(400, {"error": "bad json"})
        if not text:
            return self._reply(400, {"error": "text required"})
        names = persona_names()
        if who and (name_key(who) in names or any(name_key(w) in names for w in re.split(r"[\s\W_]+", who) if w)):
            who = ""  # /show can't speak as Grok or any voice persona, however the name is spelled or padded
        pygame.event.post(pygame.event.Event(SHOW, msg={"text": text, "from": who, "heard": heard}))
        print(f"show from {self.client_address[0]}: {len(text)} chars{' speaker=' + who if who else ''}", flush=True)
        self._reply(200, {"ok": True})

    def _enroll(self):
        """LAN POST /enroll {"phrase":"hey_finn","count":25} (show token): hand it to the wake listener, which owns the mic."""
        try:
            n = int(self.headers.get("Content-Length", 0))
            j = json.loads(self.rfile.read(min(n, 1024)) or b"{}")
            body = json.dumps({"phrase": str(j.get("phrase", ""))[:24], "count": int(j.get("count", 10)), "topup": bool(j.get("topup"))}).encode()
        except Exception:
            return self._reply(400, {"error": "bad json"})
        try:
            req = urllib.request.Request(WAKE_CONTROL, body, {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as r:
                code, out = r.status, json.loads(r.read(4096))
        except urllib.error.HTTPError as e:
            code, out = e.code, {"error": "rejected by wake listener"}
            try:
                out = json.loads(e.read(4096))
            except Exception:
                pass
        except Exception:
            code, out = 503, {"error": "wake listener unreachable"}
        print(f"enroll request from {self.client_address[0]}: {code}", flush=True)
        self._reply(code, out)

    def _state(self):
        """Localhost-only, no token: the wake listener on the same Pi drives voice states.

        A body may be a voice state, a tool card, a timer list, or any mix. Card and timer
        payloads are validated; a bad attachment is ignored rather than wiping a good one.
        The log line is the state and the attachment kinds, never the values.
        """
        if not _localhost_client(self.client_address[0]):
            return self._reply(403, {"error": "localhost only"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 8192:
                return self._reply(413, {"error": "too large"})
            j = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._reply(400, {"error": "bad json"})
        try:
            ev = state_event(j)
        except ValueError as e:
            err = str(e)
            return self._reply(400, {"error": err if err.startswith("state must") else "bad json"})
        pygame.event.post(pygame.event.Event(STATE, **ev))
        bits = [ev["state"] or "-"]
        if ev["has_card"]:
            bits.append("card" if ev["card"] else "card-ignored")
        if ev["has_timers"]:
            bits.append(f"timers {len(ev['timers'])}")
        print(f"state {' '.join(bits)} who={ev['who']}", flush=True)
        self._reply(200, {"ok": True})

    def _status(self):
        """Localhost-only snapshot of the status icons. No relay URL, host, or address."""
        if not _localhost_client(self.client_address[0]):
            return self._reply(403, {"error": "localhost only"})
        self._reply(200, status_snapshot())

    def log_message(self, *a):
        pass


def _localhost_client(addr):
    return addr.removeprefix("::ffff:") in ("127.0.0.1", "::1")


# ---------------------------------------------------------------- status (network, relay, mic)
# Polled off the UI thread. Every check takes its inputs as arguments so tests need no hardware.
_STATUS_LOCK = threading.Lock()
STATUS = {
    "net": {"kind": "none", "up": False},
    "relay": {"ok": None, "detail": ""},
    "mic": {"ok": None, "detail": ""},
    "at": 0,
}
_STATUS_ICON_CACHE = {}


def status_snapshot():
    with _STATUS_LOCK:
        st = STATUS
        return {"net": dict(st["net"]), "relay": dict(st["relay"]), "mic": dict(st["mic"]), "at": st["at"]}


def _status_visible(st):
    return (st["net"].get("kind"), bool(st["net"].get("up")), st["relay"].get("ok"), st["relay"].get("detail"),
            st["mic"].get("ok"), st["mic"].get("detail"))


def store_status(nxt):
    """Replace STATUS. True when the icons would change (the timestamp alone does not)."""
    global STATUS
    with _STATUS_LOCK:
        changed = _status_visible(STATUS) != _status_visible(nxt)
        STATUS = nxt
        return changed


def status_poll_s():
    try:
        v = float(E("GROK_STATION_STATUS_POLL", "12"))
    except (TypeError, ValueError):
        return 12.0
    return v if v > 0 else 12.0


def iface_kind(name, wireless):
    """wifi if the iface has a wireless dir or a wl* name; ethernet for eth*/en*; lo is ignored."""
    if name == "lo":
        return None
    if wireless or name.startswith("wl"):
        return "wifi"
    if name.startswith("eth") or name.startswith("en"):
        return "ethernet"
    return "other"


def has_default_route(route_text):
    """True when /proc/net/route has a non-lo default route (destination 00000000)."""
    for line in (route_text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in ("Iface", "lo") and parts[1] == "00000000":
            return True
    return False


def net_status(ifaces, route_text):
    """ifaces: (name, operstate, has_wireless). up = any non-lo iface is up AND a default route exists."""
    up_kinds = []
    any_up = False
    for name, state, wireless in ifaces:
        kind = iface_kind(name, wireless)
        if kind is None:
            continue
        if str(state).strip() == "up":
            any_up = True
            up_kinds.append(kind)
    if "wifi" in up_kinds:
        kind = "wifi"
    elif "ethernet" in up_kinds:
        kind = "ethernet"
    else:
        kind = "none"
    return {"kind": kind, "up": any_up and has_default_route(route_text)}


def read_net(net_root="/sys/class/net", route_path="/proc/net/route"):
    ifaces = []
    try:
        names = os.listdir(net_root)
    except OSError:
        names = []
    for name in names:
        base = os.path.join(net_root, name)
        try:
            with open(os.path.join(base, "operstate")) as f:
                state = f.read().strip()
        except OSError:
            state = ""
        ifaces.append((name, state, os.path.isdir(os.path.join(base, "wireless"))))
    try:
        with open(route_path) as f:
            route = f.read()
    except OSError:
        route = ""
    return net_status(ifaces, route)


def relay_token():
    """Bearer for the loopback relay. Never logged."""
    path = E("GROK_LOCAL_TOKEN_FILE", "/etc/grok-station/secrets/local_token")
    try:
        with open(path) as f:
            tok = f.read().strip()
        if tok and "\n" not in tok and "\r" not in tok:
            return tok
    except OSError:
        pass
    return E("RELAY_TOKEN", "") or E("GROK_RELAY_TOKEN", "")


def relay_health_url():
    """Loopback relay only. The icon does not open a connection to a LAN or public host."""
    raw = str(E("RELAY_PORT", "8790") or "")
    port = int(raw) if raw.isdigit() and 1 <= int(raw) <= 65535 else 8790
    return f"http://127.0.0.1:{port}/health"


def relay_icon(body):
    """Authorized /health JSON -> icon. No paired bot, or a body with no bots field, stays unconfigured.

    ok True is green (paired, and the last request did not fail, including before any request).
    ok False is red (last request timed out or its webhook POST failed). Detail never names a host.
    """
    if not isinstance(body, dict):
        return {"ok": None, "detail": ""}
    bots = body.get("bots")
    if not isinstance(bots, dict) or bots.get("paired") is not True:
        return {"ok": None, "detail": ""}
    if bots.get("ok") is True:
        return {"ok": True, "detail": "ok"}
    if bots.get("ok") is False:
        return {"ok": False, "detail": "down"}
    return {"ok": None, "detail": ""}


def check_relay(fetch, previous=None):
    """fetch() -> health JSON or None. A failed poll keeps the previous icon instead of inventing an outage."""
    try:
        body = fetch()
    except Exception:
        body = None
    if not isinstance(body, dict):
        if isinstance(previous, dict) and previous.get("ok") is not None:
            return {"ok": previous.get("ok"), "detail": previous.get("detail") or ""}
        return {"ok": None, "detail": ""}
    return relay_icon(body)


def fetch_relay_health(timeout=2):
    """GET loopback /health with the local bearer. None on any failure. The token is not logged."""
    token = relay_token()
    if not token:
        return None
    req = urllib.request.Request(relay_health_url(), headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read(8192))
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def read_relay():
    return check_relay(fetch_relay_health, status_snapshot().get("relay"))


def speakerphone_present(cards_text, match="emeet"):
    """True if any /proc/asound/cards line contains the match (default emeet, also M0 for that default)."""
    needles = []
    for part in re.split(r"[\s,]+", str(match or "")):
        p = part.casefold()
        if p and p not in needles:
            needles.append(p)
    if not needles:
        needles = ["emeet"]
    if "emeet" in needles and "m0" not in needles:
        needles.append("m0")
    for line in str(cards_text or "").splitlines():
        low = line.casefold()
        if any(n in low for n in needles):
            return True
    return False


def check_mic(now, fetch, cards_text, stale_s=30, match="emeet"):
    """fetch() -> wake /status JSON or None. ok when the speakerphone is present and level_at is within stale_s."""
    if not speakerphone_present(cards_text, match):
        return {"ok": False, "detail": "speakerphone missing"}
    try:
        payload = fetch()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return {"ok": False, "detail": "wake listener down"}
    if "capture" in payload and not payload.get("capture"):  # listener up but no input device open
        return {"ok": False, "detail": "no capture device"}
    try:
        level_at = float(payload.get("level_at"))
    except (TypeError, ValueError):
        return {"ok": False, "detail": "capture stalled"}
    try:
        window = float(stale_s)
    except (TypeError, ValueError):
        window = 30.0
    if now - level_at > window:
        return {"ok": False, "detail": "capture stalled"}
    return {"ok": True, "detail": "ok"}


def mic_stale_s():
    try:
        return float(E("GROK_STATION_MIC_STALE", "30"))
    except (TypeError, ValueError):
        return 30.0


def mic_match():
    return E("GROK_STATION_MIC_MATCH", "emeet") or "emeet"


def wake_status_url(control=None):
    """Host and port come from WAKE_CONTROL; the path is the wake listener's /status."""
    raw = WAKE_CONTROL if control is None else control
    parts = urllib.parse.urlsplit(raw)
    host = parts.hostname or "127.0.0.1"
    if parts.port:
        port = parts.port
    else:
        port = 443 if parts.scheme == "https" else 80
    scheme = "https" if parts.scheme == "https" else "http"
    return f"{scheme}://{host}:{port}/status"


def fetch_wake_status(url=None, timeout=2):
    target = wake_status_url() if url is None else url
    try:
        with urllib.request.urlopen(urllib.request.Request(target), timeout=timeout) as r:
            payload = json.loads(r.read(8192))
    except Exception:
        return None
    return payload


def read_cards(path="/proc/asound/cards"):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def read_mic(now):
    return check_mic(now, fetch_wake_status, read_cards(), stale_s=mic_stale_s(), match=mic_match())


def collect_status(now=None):
    now = time.time() if now is None else now
    return {"net": read_net(), "relay": read_relay(), "mic": read_mic(now), "at": now}


def status_loop():
    """Daemon poll. Posts STATUS_EV only when an icon would change. Never logs a URL or host."""
    while True:
        started = time.time()
        try:
            if store_status(collect_status(now=started)):
                pygame.event.post(pygame.event.Event(STATUS_EV))
        except Exception as e:
            print(f"status poll failed: {type(e).__name__}", flush=True)
        delay = status_poll_s() - (time.time() - started)
        if delay > 0:
            time.sleep(delay)


def net_icon_name(net):
    up = bool(net.get("up"))
    if net.get("kind") == "ethernet":
        return "ethernet-connected" if up else "ethernet-disconnected"
    return "wifi-connected" if up else "wifi-disconnected"


def status_icon_names(st):
    """Right to left: network, relay (hidden when unconfigured), mic (hidden until the first reading)."""
    names = [net_icon_name(st.get("net") or {})]
    relay = st.get("relay") or {}
    if relay.get("ok") is not None:
        names.append("relay-connected" if relay.get("ok") else "relay-disconnected")
    mic = st.get("mic") or {}
    if mic.get("ok") is not None:
        names.append("mic-ok" if mic.get("ok") else "mic-problem")
    return names


def status_icon_px(scale):
    """44 px at 1080p times the display scale, never under 28 px on screen."""
    return max(28, int(round(44 * scale)))


def _fallback_status_icon(name, px):
    """Teal glyph when the state is good. A bad relay is red. Other bad icons stay muted plus an amber slash."""
    px = max(1, int(px))
    surf = pygame.Surface((px, px), pygame.SRCALPHA)
    bad = name.endswith("disconnected") or name.endswith("problem")
    color = MUTED if bad else LISTENING
    sw = max(1, px // 10)
    pad = max(1, px // 6)

    def stroke(width, limit):
        return max(1, min(width, limit))

    if name.startswith("wifi"):
        cx, cy = px // 2, px - pad
        pygame.draw.circle(surf, color, (cx, max(0, cy)), stroke(sw, max(1, px // 3)))
        for frac in (0.45, 0.75, 1.05):
            r = max(2, int((px - 2 * pad) * frac / 2))
            rect = pygame.Rect(cx - r, cy - r, r * 2, r * 2)
            if rect.w > 2 and rect.h > 2:
                pygame.draw.arc(surf, color, rect, math.radians(45), math.radians(135), stroke(sw, rect.w // 2))
    elif name.startswith("ethernet"):
        body = pygame.Rect(pad, pad, max(1, px - 2 * pad), max(1, px // 2 - pad // 2))
        rad = min(body.w, body.h) // 2
        pygame.draw.rect(surf, color, body, stroke(sw, max(1, min(body.w, body.h) // 2)), border_radius=max(0, rad))
        pygame.draw.line(surf, color, (px // 2, body.bottom), (px // 2, max(body.bottom, px - pad)), sw)
    elif name.startswith("relay"):
        r = max(2, px // 6)
        pygame.draw.circle(surf, color, (pad + r, px // 2), r, stroke(sw, r))
        pygame.draw.circle(surf, color, (max(pad + r, px - pad - r), px // 2), r, stroke(sw, r))
        pygame.draw.line(surf, color, (pad + 2 * r, px // 2), (max(pad + 2 * r, px - pad - 2 * r), px // 2), sw)
    else:
        w, h = max(2, px // 3), max(2, px // 2)
        body = pygame.Rect((px - w) // 2, pad, w, h)
        rad = min(w, h) // 2
        pygame.draw.rect(surf, color, body, stroke(sw, min(w, h) // 2), border_radius=rad)
        pygame.draw.line(surf, color, (px // 2, body.bottom), (px // 2, max(body.bottom, px - pad)), sw)
    if bad and px >= 4:
        a, b = (pad, pad), (px - pad - 1, px - pad - 1)
        pygame.draw.line(surf, hexc("0D0B09"), a, b, sw + 2)
        pygame.draw.line(surf, RED if name.startswith("relay") else AMBER, a, b, sw)
    return surf


def _load_svg(path, px):
    """SVG via pygame.image.load, smoothscaled to px. None when missing, unreadable, or empty."""
    px = max(1, int(px))
    try:
        if not path or not os.path.isfile(path):
            return None
        img = pygame.image.load(path)
        if not (img.get_flags() & pygame.SRCALPHA):
            img = img.convert_alpha()
        if img.get_width() <= 0 or img.get_height() <= 0:
            return None
        if img.get_size() != (px, px):
            img = pygame.transform.smoothscale(img, (px, px))
        return img
    except Exception:
        return None


def load_status_icon(name, px, directory=None):
    """SVG from assets/status/<name>.svg, smoothscaled. A missing or unreadable file uses the vector fallback."""
    px = max(1, int(px))
    directory = STATUS_DIR if directory is None else directory
    key = (directory, name, px)
    hit = _STATUS_ICON_CACHE.get(key)
    if hit is not None:
        return hit
    surf = _load_svg(os.path.join(directory, f"{name}.svg"), px)
    if surf is None:
        surf = _fallback_status_icon(name, px)
    if len(_STATUS_ICON_CACHE) > 48:
        _STATUS_ICON_CACHE.pop(next(iter(_STATUS_ICON_CACHE)))
    _STATUS_ICON_CACHE[key] = surf
    return surf


def draw_status_icons(surf, L, status=None, right=None, cy=None):
    """Right to left in the top corner. `right`/`cy` move the row left of a top-bar clock. Returns the row rect."""
    global LAST_STATUS_RECT
    names = status_icon_names(status_snapshot() if status is None else status)
    px = status_icon_px(L.s)
    gap = max(1, int(round(8 * L.s)))
    if right is None:
        right = L.w - max(6, int(round(16 * L.s)))
    if cy is None:
        cy = max(4, int(round(8 * L.s))) + px // 2  # flush with the top edge
    y = int(cy) - px // 2
    x = int(right)
    rects = []
    for name in names:
        x -= px
        rect = pygame.Rect(x, y, px, px)
        surf.blit(load_status_icon(name, px), rect.topleft)
        rects.append(rect)
        x -= gap
    LAST_STATUS_RECT = rects[0].unionall(rects[1:]) if len(rects) > 1 else rects[0]
    return LAST_STATUS_RECT


# ---------------------------------------------------------------- drawing helpers
def text_at(surf, kind, px, s, color, pos, anchor="topleft"):
    img = font(kind, px).render(s, True, color)
    r = img.get_rect(**{anchor: pos})
    surf.blit(img, r)
    return r


def baseline_text(surf, kind, px, s, color, x, baseline, align="left"):
    f = font(kind, px)
    img = f.render(s, True, color)
    left = x if align == "left" else x - img.get_width()
    surf.blit(img, (left, baseline - f.get_ascent()))
    return pygame.Rect(left, baseline - f.get_ascent(), img.get_width(), img.get_height())


def wrap(f, text, width):
    lines = []
    for para in text.splitlines() or [""]:
        line = ""
        for word in para.split():
            trial = f"{line} {word}".strip()
            if f.size(trial)[0] <= width or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return [l for l in lines] or [""]


def ellipsize(f, line, width):
    line = line.rstrip() + "…"
    while f.size(line)[0] > width and len(line) > 1:
        line = line[:-2].rstrip() + "…"
    return line


def fit_text(kind, text, width, height, start, minimum, lh=1.22, max_lines=None):
    """Largest size from start down to minimum whose wrap fits; truncate with … at the minimum."""
    px = start
    while True:
        f = font(kind, px)
        lines = wrap(f, text, width)
        step = int(f.get_height() * lh)
        cap = min(max_lines or 999, max(1, int((height - f.get_height()) // step) + 1))
        fits = len(lines) <= cap and all(f.size(l)[0] <= width for l in lines)
        if fits or px <= minimum:
            if not fits:
                lines = lines[:cap]
                lines[-1] = ellipsize(f, lines[-1], width)
            return f, lines, step
        px = max(minimum, int(px * 0.92))


def draw_lines(surf, f, lines, step, color, x, y, align="left", width=0):
    for l in lines:
        img = f.render(l, True, color)
        surf.blit(img, (x + (width - img.get_width()) // 2 if align == "center" else x, y))
        y += step
    return y


def spin_fps():
    """Thinking-ring frames per second (GROK_STATION_SPIN_FPS, default 15)."""
    try:
        return min(60.0, max(1.0, float(E("GROK_STATION_SPIN_FPS", "15"))))
    except ValueError:
        return 15.0


def colon_blink_on(now=None):
    """Steady colon by default; GROK_STATION_BLINK=1 blinks it (lit on even unix seconds)."""
    if E("GROK_STATION_BLINK", "0") == "0":
        return True
    return int(time.time() if now is None else now) % 2 == 0


def clock_parts(now):
    return time.strftime("%-I:%M", now), time.strftime("%p", now)


def is_night(now):
    if not NIGHT:
        return False
    try:
        a, b = (int(x) for x in NIGHT.split("-"))
    except ValueError:
        return False
    h = now.tm_hour
    return (a <= h or h < b) if a > b else (a <= h < b)


class Layout:
    """Spec coordinates are at 1920x1080; scale by the limiting dimension, place relative to the real edges."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.s = min(h / 1080, w / 1920)
        self.m = int(96 * self.s)               # side margin
        self.extra_y = max(0, h - 1080 * self.s)  # spare height on screens taller than 16:9

    def __call__(self, v):
        return int(v * self.s)


# ---------------------------------------------------------------- icons (supersampled primitives)
SS = 3


def _cloud_shapes(sz):
    # circles (cx, cy, r) and a base rect, in a unit box of size sz
    return [(0.36, 0.60, 0.20), (0.56, 0.48, 0.25), (0.76, 0.64, 0.17)], (0.20, 0.62, 0.70, 0.18)


def _draw_union(surf, sz, circles, rect, inset, color):
    for cx, cy, r in circles:
        pygame.draw.circle(surf, color, (cx * sz, cy * sz), max(1, r * sz - inset))
    x, y, w, h = rect
    rr = pygame.Rect(x * sz + inset, y * sz + inset, w * sz - 2 * inset, h * sz - 2 * inset)
    pygame.draw.rect(surf, color, rr, border_radius=int(max(1, h * sz / 2 - inset)))


def weather_icon(kind, day, px, stroke_px):
    sz, sw = px * SS, stroke_px * SS
    c = pygame.Surface((sz, sz), pygame.SRCALPHA)
    clear = (0, 0, 0, 0)
    circles, rect = _cloud_shapes(sz)

    def sun(cx, cy, r, rays=True):
        pygame.draw.circle(c, SUN, (cx, cy), r)
        pygame.draw.circle(c, clear, (cx, cy), r - sw)
        if rays:
            for i in range(8):
                a = i * math.pi / 4
                p1 = (cx + math.cos(a) * (r + sw * 1.6), cy + math.sin(a) * (r + sw * 1.6))
                p2 = (cx + math.cos(a) * (r + sw * 3.6), cy + math.sin(a) * (r + sw * 3.6))
                pygame.draw.line(c, SUN, p1, p2, sw)
                for p in (p1, p2):
                    pygame.draw.circle(c, SUN, p, sw / 2)

    def moon(cx, cy, r):
        ring = pygame.Surface((sz, sz), pygame.SRCALPHA)
        pygame.draw.circle(ring, SUN, (cx + r * 0.55, cy - r * 0.45), r * 0.85)
        pygame.draw.circle(ring, clear, (cx + r * 0.55, cy - r * 0.45), r * 0.85 - sw)
        mask = pygame.Surface((sz, sz), pygame.SRCALPHA)
        pygame.draw.circle(mask, (255, 255, 255, 255), (cx, cy), r)
        ring.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
        pygame.draw.circle(c, SUN, (cx, cy), r)
        pygame.draw.circle(c, clear, (cx, cy), r - sw)
        pygame.draw.circle(c, clear, (cx + r * 0.55, cy - r * 0.45), r * 0.85)
        c.blit(ring, (0, 0))

    def cloud(dy=0.0):
        cs = [(x, y + dy, r) for x, y, r in circles]
        rc = (rect[0], rect[1] + dy, rect[2], rect[3])
        _draw_union(c, sz, cs, rc, -sw * 1.2, clear)  # gap around the cloud
        _draw_union(c, sz, cs, rc, 0, TEXT)
        _draw_union(c, sz, cs, rc, sw, clear)

    if kind == "clear":
        (sun if day else moon)(sz * 0.5, sz * 0.5, sz * (0.22 if day else 0.30))
    else:
        dy = -0.12 if kind in ("rain", "snow", "storm", "fog") else 0.0
        if kind == "partly":
            (sun if day else moon)(sz * (0.36 if day else 0.30), sz * (0.36 if day else 0.28), sz * (0.15 if day else 0.20))
        cloud(dy)
        bottom = sz * (rect[1] + rect[3] + dy) + sw * 2.2
        if kind == "rain":
            for x in (0.36, 0.54, 0.72):
                pygame.draw.line(c, TEXT, (sz * x, bottom), (sz * x - sw * 1.5, bottom + sz * 0.16), sw)
        elif kind == "snow":
            for i, x in enumerate((0.36, 0.54, 0.72)):
                pygame.draw.circle(c, TEXT, (sz * x, bottom + sz * (0.05 + 0.07 * (i % 2))), sw * 0.9)
        elif kind == "storm":
            pts = [(0.56, 0.0), (0.46, 0.13), (0.56, 0.13), (0.48, 0.27)]
            pygame.draw.lines(c, SUN, False, [(sz * x, bottom + sz * y) for x, y in pts], sw)
        elif kind == "fog":
            for i, (a, b) in enumerate(((0.22, 0.78), (0.30, 0.86))):
                y = bottom + sz * (0.03 + 0.11 * i)
                pygame.draw.line(c, TEXT, (sz * a, y), (sz * b, y), sw)
    return pygame.transform.smoothscale(c, (px, px))


def offline_icon(px, stroke_px):
    sz, sw = px * SS, stroke_px * SS
    c = pygame.Surface((sz, sz), pygame.SRCALPHA)
    circles, rect = _cloud_shapes(sz)
    circles = [(x, y - 0.08, r) for x, y, r in circles]
    rect = (rect[0], rect[1] - 0.08, rect[2], rect[3])
    _draw_union(c, sz, circles, rect, 0, AMBER)
    _draw_union(c, sz, circles, rect, sw, (0, 0, 0, 0))
    a, b = (sz * 0.2, sz * 0.18), (sz * 0.82, sz * 0.88)
    pygame.draw.line(c, (0, 0, 0, 0), a, b, int(sw * 3.2))
    pygame.draw.line(c, AMBER, a, b, sw)
    for p in (a, b):
        pygame.draw.circle(c, AMBER, p, sw / 2)
    return pygame.transform.smoothscale(c, (px, px))


# ---------------------------------------------------------------- portrait + state ring
def find_portrait(name):
    """Portrait for a voice persona (the station voice or a registry name) only; anything else gets the monogram."""
    hit = persona_names().get(name_key(name))
    if not hit:
        return None
    slug = hit[1]
    for d in PORTRAIT_DIRS:
        for ext in (".png", ".jpg"):
            p = os.path.join(d, slug + ext)
            if slug and os.path.isfile(p):
                return p
    return None


def _persona_slug(name):
    hit = persona_names().get(name_key(name))
    return hit[1] if hit else None


def default_avatar_path(slug):
    """assets/avatars/default-NN.svg for this slug, or None."""
    n = default_avatar_index(slug)
    if not isinstance(n, int) or n < 1 or n > 18:
        return None
    return os.path.join(AVATAR_DIR, "default-%02d.svg" % n)


def _portrait_ok(path):
    """Present and not over the 2 MB the relay will serve."""
    try:
        return bool(path) and os.path.isfile(path) and 0 < os.path.getsize(path) <= PORTRAIT_MAX
    except OSError:
        return False


def ring_speaking(c, cx, cy, r, ring_w, glow, t):
    """Warm ring with a soft static outer glow. t is a 0..1 phase/amplitude for future animation."""
    for i in range(glow, 0, -1):
        a = int(70 * (1 - i / glow) ** 2)
        pygame.draw.circle(c, (*SPEAKING[:3], a), (cx, cy), r + ring_w / 2 + i, 2 * SS)
    pygame.draw.circle(c, SPEAKING, (cx, cy), r + ring_w / 2, int(ring_w))


def ring_idle(c, cx, cy, r, ring_w, glow, t):
    pygame.draw.circle(c, RULE, (cx, cy), r + ring_w / 2, int(ring_w / 3))


def ring_listening(c, cx, cy, r, ring_w, glow, t):
    """Teal solid ring, same style as the thinking arc but full and continuous, no ticks. t is the pulse phase in
    [0, 1): once per loop the ring thickens and brightens and a soft glow swells (smooth cosine, rest at t=0)."""
    p = round(0.5 - 0.5 * math.cos(2 * math.pi * (t % 1.0)), 4)  # rounded: mirror phases render identically
    rr = r + ring_w / 2
    gw = max(2, int(ring_w * 1.4))
    for i in range(gw, 0, -2):  # soft glow outside the ring, inside the portrait pad
        a = int(80 * p * (1 - i / gw) ** 2)
        if a:
            pygame.draw.circle(c, (*LISTENING[:3], a), (cx, cy), rr + ring_w * 0.5 + i, 2 * SS)
    w = ring_w * (0.8 + 0.3 * p)
    color = tuple(int(v * (0.72 + 0.28 * p)) for v in LISTENING[:3])
    steps = 240
    for i in range(steps):  # thick ring as overlapping dots (round, gap-free), like ring_thinking
        a = 2 * math.pi * i / steps
        pygame.draw.circle(c, color, (cx + rr * math.cos(a), cy + rr * math.sin(a)), w / 2)


def ring_thinking(c, cx, cy, r, ring_w, glow, t):
    """Violet arc (Noor S3): faint full track plus a bright 270° arc with rounded ends.
    t is a rotation phase in [0, 1); the arc's start angle advances by 2*pi*t."""
    rr = r + ring_w / 2
    pygame.draw.circle(c, (*THINKING[:3], 60), (cx, cy), rr, max(2, int(ring_w / 3)))
    a0 = -math.pi / 2 + 2 * math.pi * (t % 1.0)
    a1 = a0 + 1.5 * math.pi
    steps = 180
    for i in range(steps + 1):  # thick arc as overlapping dots: pygame.draw.arc leaves gaps at this width
        a = a0 + (a1 - a0) * i / steps
        pygame.draw.circle(c, THINKING, (cx + rr * math.cos(a), cy + rr * math.sin(a)), ring_w / 2)


def ring_success(c, cx, cy, r, ring_w, glow, t):
    """Teal static ring (saved, uploaded, passed)."""
    pygame.draw.circle(c, LISTENING, (cx, cy), r + ring_w / 2, int(ring_w))


def ring_ready(c, cx, cy, r, ring_w, glow, t):
    """Get ready: warm arc on a rule track that fills clockwise from 12 o'clock as t goes 0 -> 1 (the 0.9 s pause)."""
    rr = r + ring_w / 2
    pygame.draw.circle(c, RULE, (cx, cy), rr, max(2, int(ring_w / 3)))
    t = max(0.0, min(1.0, t))
    steps = int(240 * t)
    for i in range(steps + 1):
        a = -math.pi / 2 + 2 * math.pi * t * i / max(1, steps)
        pygame.draw.circle(c, SPEAKING, (cx + rr * math.cos(a), cy + rr * math.sin(a)), ring_w / 2)


def ring_saved(c, cx, cy, r, ring_w, glow, t):
    """Clip accepted: one teal pulse (width up to 2x plus a soft halo at 35%), ease-out; plain teal ring at t=1."""
    t = max(0.0, min(1.0, t))
    p = math.sin(math.pi * (1 - (1 - t) ** 2))  # ease-out bump, 0 at both ends
    w = ring_w * (1 + p)
    if p > 0.01:
        halo = (*LISTENING[:3], int(255 * 0.35 * p))
        pygame.draw.circle(c, halo, (cx, cy), r + ring_w / 2 + w / 2 + ring_w * 0.4, max(2, int(ring_w * 0.8)))
    pygame.draw.circle(c, LISTENING, (cx, cy), r + ring_w / 2, int(w))


# Voice states register a painter here; "error" and "notice" reuse the quiet idle ring.
RINGS = {"speaking": ring_speaking, "idle": ring_idle, "listening": ring_listening, "thinking": ring_thinking, "error": ring_idle, "notice": ring_idle,
         "success": ring_success, "enroll_ready": ring_ready, "enroll_saved": ring_saved}

_photo_cache = {}  # (path, mtime, svg, svg_mtime, d, k, initial, photo_ok, kind) -> circle-cropped surface
_MARK_CACHE = {}
_MARK_PX = (64, 128, 256, 512, 1024)  # assets/brand/mark-<px>.png, rendered from mark.svg by tools/render_brand.py
LAST_PORTRAIT_RECT = LAST_PORTRAIT_ARGS = None  # last session portrait blit, so the ring can spin in place
LAST_COLON_RECT = LAST_COLON_GLYPH = None       # last clock colon, so it can blink without a full repaint
LAST_DIGITS_RECT = None
LAST_CLOCK_RECT = LAST_DATE_RECT = LAST_WEATHER_RECT = LAST_OFFLINE_RECT = None
LAST_TOPBAR_RECT = LAST_CARD_RECT = LAST_TIMER_RECT = LAST_STATUS_RECT = None
TIMER_CHIPS = []    # validated timers: {id, label, ends_at_epoch, ringing}; the display counts down locally
TIMER_EPOCH = None  # pin the countdown (render script); None uses time.time()
_CARD_ICON = ("sun", "partly", "cloud", "fog", "rain", "snow", "storm")
_CARD_MAX = 640     # bytes of JSON; anything larger is dropped


def _no_persona(name):
    """The built-in assistant (persona slug grok) has no photo. Its avatar is the Echo mark."""
    hit = persona_names().get(name_key(name))
    return bool(hit) and hit[1] == "grok"


def _echo_mark(px):
    """Echo mark at px (the nearest shipped PNG, smoothscaled). None if assets/brand has no mark PNG."""
    px = max(8, int(px))
    hit = _MARK_CACHE.get(px)
    if hit is not None:
        return hit
    pick = next((s for s in _MARK_PX if s >= px), _MARK_PX[-1])
    path = os.path.join(ASSETS, "brand", f"mark-{pick}.png")
    if not os.path.isfile(path):
        path = next((os.path.join(ASSETS, "brand", f"mark-{s}.png") for s in reversed(_MARK_PX)
                     if os.path.isfile(os.path.join(ASSETS, "brand", f"mark-{s}.png"))), "")
    if not path:
        return None
    img = pygame.image.load(path)
    if not (img.get_flags() & pygame.SRCALPHA):
        img = img.convert_alpha()
    if img.get_size() != (px, px):
        img = pygame.transform.smoothscale(img, (px, px))
    if len(_MARK_CACHE) > 12:
        _MARK_CACHE.pop(next(iter(_MARK_CACHE)))
    _MARK_CACHE[px] = img
    return img


def _blit_cover(photo, img, r):
    """Cover-crop img onto the circle's square, biased slightly upward, the same way a photo is cropped."""
    iw, ih = img.get_size()
    if iw <= 0 or ih <= 0:
        return False
    side = 2 * r
    sc = side / min(iw, ih)
    img = pygame.transform.smoothscale(img, (max(side, int(iw * sc)), max(side, int(ih * sc))))
    photo.blit(img, ((side - img.get_width()) // 2, int((side - img.get_height()) * 0.3)))
    return True


def _circle_photo(name, d, k, photo_ok):
    """Circle-cropped photo, the Echo mark on surface-2, a default avatar, or a monogram.

    A known bot with no usable photo (missing, unreadable, over 2 MB, or photo_ok false) gets
    assets/avatars/default-NN.svg, loaded like the status icons and circle-cropped like a photo.
    The station persona keeps the Echo mark. The monogram is only the last fallback, when that SVG
    cannot be loaded. image.load runs once per key (at most 8 kept). A wake word with no persona
    gets the Echo mark on a surface-2 circle, not a portrait file."""
    slug = _persona_slug(name)
    use_mark = bool(photo_ok) and slug == "grok"
    known_bot = bool(slug) and slug != "grok"
    path = None
    if photo_ok and not use_mark:
        candidate = find_portrait(name)
        if _portrait_ok(candidate):
            path = candidate
    svg = default_avatar_path(slug) if known_bot else None
    try:
        mt = os.stat(path).st_mtime if path else None
    except OSError:
        path, mt = None, None
    try:
        svg_mt = os.stat(svg).st_mtime if svg else None
    except OSError:
        svg_mt = None
    initial = (name[:1] or "?").upper()
    kind = "mark" if use_mark else ("default" if known_bot and not path else "photo")
    key = (path, mt, svg if kind == "default" else None, svg_mt if kind == "default" else None, d, k, initial, bool(photo_ok), kind)
    hit = _photo_cache.get(key)
    if hit is not None:
        return hit
    r = d * k // 2
    photo = pygame.Surface((2 * r, 2 * r), pygame.SRCALPHA)
    drawn = False
    if path:
        try:
            drawn = _blit_cover(photo, pygame.image.load(path), r)
        except Exception:
            drawn = False
    if not drawn and known_bot and svg:
        img = _load_svg(svg, 2 * r)
        if img is not None:
            photo.blit(img, (0, 0))
            drawn = True
    if not drawn:
        photo.fill(SURFACE2)
        mark = _echo_mark(max(8, int(2 * r * 0.78))) if use_mark else None
        if mark is not None:
            photo.blit(mark, mark.get_rect(center=(r, r)))
        else:
            mono = font("display", d * k * 0.42).render(initial, True, TEXT)
            photo.blit(mono, mono.get_rect(center=(r, r)))
    mask = pygame.Surface((2 * r, 2 * r), pygame.SRCALPHA)
    pygame.draw.circle(mask, (255, 255, 255, 255), (r, r), r)
    photo.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
    if len(_photo_cache) >= 8:
        _photo_cache.pop(next(iter(_photo_cache)))
    _photo_cache[key] = photo
    return photo


def portrait(name, d, ring_w, state="speaking", t=1.0, photo_ok=True):
    """Circle-cropped photo (Echo mark, default avatar, or monogram) with the state ring. Returns (surface, offset)."""
    k = 2
    glow = int(ring_w * 2.2)
    pad = int(ring_w + glow + 4)
    size = (d + 2 * pad) * k
    c = pygame.Surface((size, size), pygame.SRCALPHA)
    cx = cy = size // 2
    r = d * k // 2
    c.blit(_circle_photo(name, d, k, photo_ok), (cx - r, cy - r))
    RINGS.get(state, ring_speaking)(c, cx, cy, r + int(ring_w * k * 0.6), ring_w * k, glow * k, t)
    return pygame.transform.smoothscale(c, (size // k, size // k)), pad


# ---------------------------------------------------------------- screens
def draw_clock(surf, kind, px, hhmm, color, x, baseline, colon_on=True):
    """Draw HH:MM. The colon's ink sits in the middle of the hour/minute ink gap and on the
    vertical ink center of digits 0-9 (not the font line box). Hour and minute stay on their
    normal advances, so the gap width matches a single render of the string.
    Returns (digits_rect, colon_rect): digits_rect unions the advance box and the ink (no AM/PM)."""
    global LAST_COLON_RECT, LAST_COLON_GLYPH, LAST_DIGITS_RECT
    f = font(kind, px)
    hour, minute = hhmm.split(":", 1)
    top = baseline - f.get_ascent()
    pens, pen = [], 0
    for m in f.metrics(hhmm) or []:
        pens.append(pen)
        pen += m[4] if m else 0
    ci = hhmm.index(":")
    hour_img, min_img = f.render(hour, True, color), f.render(minute, True, color)
    col_img = f.render(":", True, color)
    surf.blit(hour_img, (x, top))
    min_x = x + pens[ci + 1]
    surf.blit(min_img, (min_x, top))
    hbr, mbr, cbr = hour_img.get_bounding_rect(), min_img.get_bounding_rect(), col_img.get_bounding_rect()
    gap_cx = (x + hbr.right + min_x + mbr.left) / 2.0
    ref = f.render("0123456789", True, color).get_bounding_rect()
    col_x = int(round(gap_cx - (cbr.x + cbr.w / 2.0)))
    col_y = int(round(top + ref.y + ref.h / 2.0 - (cbr.y + cbr.h / 2.0)))
    if colon_on:
        surf.blit(col_img, (col_x, col_y))
    colon_rect = pygame.Rect(col_x + cbr.x, col_y + cbr.y, max(1, cbr.w), max(1, cbr.h))
    digits = pygame.Rect(x, top, f.size(hhmm)[0], f.get_height())
    digits = digits.union(pygame.Rect(x + hbr.x, top + hbr.y, hbr.w, hbr.h))
    digits = digits.union(pygame.Rect(min_x + mbr.x, top + mbr.y, mbr.w, mbr.h)).union(colon_rect)
    LAST_COLON_RECT, LAST_COLON_GLYPH, LAST_DIGITS_RECT = colon_rect, (col_img, (col_x, col_y)), digits
    return digits, colon_rect


def tpx(se, frac):
    """Text size as a fraction of the short edge. Never under 14 px."""
    return max(14, round(frac * se))


def safe_insets(w, h):
    """(side, top, bottom) safe margins from the short edge. 0.075 / 0.0516 / 0.0657."""
    se = min(w, h)
    return round(0.075 * se), round(0.0516 * se), round(0.0657 * se)


def _finite(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not math.isfinite(float(v)) or not lo <= float(v) <= hi:
        return None
    return int(round(float(v)))


def _clip_str(v, n):
    if not isinstance(v, str):
        return None
    s = " ".join(v.split())
    return s[:n] if s else None


def state_event(j):
    """Validate a POST /state body into STATE-event fields.

    `state` is None for a card-only or timers-only post. `card` is the cleaned card, or None
    when the post omitted it or the payload was unusable. `has_timers` is false when `timers`
    is missing or not a list (the previous chips stay); an empty list clears them.
    Raises ValueError when nothing usable is left.
    """
    if not isinstance(j, dict):
        raise ValueError("bad json")
    raw_state = j.get("state")
    has_state = "state" in j and raw_state not in (None, "")
    if has_state and raw_state not in ("idle", *VOICE_HOLDS):
        raise ValueError("state must be idle, " + ", ".join(VOICE_HOLDS))
    has_card = "card" in j
    card = clean_card(j.get("card")) if has_card else None
    timers = clean_timers(j.get("timers")) if "timers" in j else None
    if not has_state and card is None and timers is None:
        raise ValueError("nothing to apply")
    heard = " ".join(str(j.get("heard") or "").split())[:MAX_HEARD]
    text = str(j.get("text") or "").strip()[:MAX_TEXT]
    who = persona_names().get(name_key(j.get("who") or VOICE_NAME), (VOICE_NAME, "grok"))[0]
    return {"state": raw_state if has_state else None, "heard": heard, "text": text, "done": bool(j.get("done")),
            "who": who, "has_card": has_card, "card": card, "has_timers": timers is not None, "timers": timers}


def merge_card(prior, has_card, card, done):
    """Card to keep on a new voice message.

    A usable card replaces the previous one. A bad payload is ignored (the previous card stays).
    A finished message that didn't include a card — the timer notice — does not keep one.
    """
    if has_card:
        return card if card else prior
    if done:
        return None
    return prior


def clean_card(c):
    """Allowlisted display fields only. Bad types and unknown icons are dropped; an oversized payload is ignored."""
    if not isinstance(c, dict):
        return None
    if c.get("type") not in (None, "card"):
        return None
    try:
        if len(json.dumps(c)) > _CARD_MAX:
            return None
    except (TypeError, ValueError):
        return None
    kind = c.get("kind")
    if kind == "weather":
        out = {"kind": "weather"}
        if c.get("icon") in _CARD_ICON:
            out["icon"] = c["icon"]
        temp = _finite(c.get("now_temp"), -150, 150)
        if temp is not None:
            out["now_temp"] = temp
        if c.get("units") in ("°F", "°C"):
            out["units"] = c["units"]
        cond = _clip_str(c.get("conditions"), 32)
        if cond:
            out["conditions"] = cond
        for name in ("today", "tomorrow"):
            hl = c.get(name)
            if not isinstance(hl, dict):
                continue
            hi, lo = _finite(hl.get("high"), -150, 150), _finite(hl.get("low"), -150, 150)
            if hi is not None and lo is not None:
                out[name] = {"high": hi, "low": lo}
        return out if len(out) > 1 else None
    if kind == "time":
        out = {"kind": "time"}
        for key, n in (("zone_label", 24), ("hhmm", 16), ("day", 4), ("offset", 12)):
            s = _clip_str(c.get(key), n)
            if s:
                out[key] = s
        return out if "hhmm" in out else None
    if kind == "convert":
        out = {"kind": "convert"}
        for key, n in (("value", 16), ("from_unit", 12), ("result", 16), ("to_unit", 12)):
            s = _clip_str(c.get(key), n)
            if s:
                out[key] = s
        return out if {"value", "from_unit", "result", "to_unit"} <= out.keys() else None
    return None


def clean_timers(items):
    """None if `items` isn't a list (caller keeps the previous set). Otherwise the valid chips, possibly empty."""
    if not isinstance(items, list):
        return None
    out, seen = [], set()
    for t in items[:10]:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id", ""))
        if not re.fullmatch(r"[a-z0-9]{4,12}", tid) or tid in seen:
            continue
        ends = t.get("ends_at_epoch")
        if isinstance(ends, bool) or not isinstance(ends, (int, float)) or not math.isfinite(float(ends)):
            continue
        if not 1_000_000_000 <= float(ends) <= 20_000_000_000:
            continue
        label = " ".join(re.sub(r"[^\w '\-]", " ", str(t.get("label") or "")).split())[:24] or "timer"
        out.append({"id": tid, "label": label, "ends_at_epoch": float(ends), "ringing": bool(t.get("ringing"))})
        seen.add(tid)
    return out


def fmt_remaining(sec):
    """mm:ss, or h:mm:ss once an hour is left. Counts down; never negative."""
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _epoch(epoch):
    if epoch is not None:
        return epoch
    return time.time() if TIMER_EPOCH is None else TIMER_EPOCH


def timer_strip_height(se):
    px = tpx(se, 0.0328)
    return font("body", px).get_height() + 2 * max(4, round(0.006 * se))


def timer_strip_rect(L):
    """Bottom band inside the safe area. Full safe width so a once-a-second redraw never shifts the rect."""
    side, top, bot = safe_insets(L.w, L.h)
    sh = timer_strip_height(min(L.w, L.h))
    y = max(top, L.h - bot - sh)
    return pygame.Rect(side, y, max(1, L.w - 2 * side), sh)


def content_limit(L):
    """Lowest y session text or a card may use: above the timer strip when one is showing."""
    _side, _top, bot = safe_insets(L.w, L.h)
    gap = max(6, round(0.008 * min(L.w, L.h)))
    if TIMER_CHIPS:
        return timer_strip_rect(L).top - gap
    return L.h - max(L(96), bot)


def draw_timer_chips(surf, rect, timers, epoch):
    se = min(surf.get_width(), surf.get_height())
    px = tpx(se, 0.0328)
    fl, fn = font("body", px), font("mono", px)
    pad_x = max(8, round(0.012 * se))
    gap = max(6, round(0.008 * se))
    inner = max(6, round(0.008 * se))
    ordered = sorted(timers, key=lambda t: (not t.get("ringing"), t.get("ends_at_epoch", 0)))[:10]
    shown, extra = ordered[:4], max(0, len(ordered) - 4)

    def right_of(t):
        if t.get("ringing"):
            return "Done"
        return fmt_remaining(t.get("ends_at_epoch", epoch) - epoch)

    labels = [t.get("label") or "timer" for t in shown]
    times = [right_of(t) for t in shown]
    plus = f"+{extra}" if extra else ""

    def chip_w(lab, tim):
        return pad_x + fl.size(lab)[0] + inner + fn.size(tim)[0] + pad_x

    def total(labs):
        w = sum(chip_w(a, b) for a, b in zip(labs, times))
        n = len(labs) + (1 if plus else 0)
        if plus:
            w += pad_x + fl.size(plus)[0] + pad_x
        return w + gap * max(0, n - 1)

    for _ in range(48):
        if not labels or total(labels) <= rect.w:
            break
        i = max(range(len(labels)), key=lambda k: fl.size(labels[k])[0])
        base = labels[i].rstrip("…").rstrip()
        if len(base) <= 1:
            labels[i] = base[:1] or "…"
            break
        labels[i] = base[:-1] + "…"
    widths = [chip_w(a, b) for a, b in zip(labels, times)]
    plus_w = pad_x + fl.size(plus)[0] + pad_x if plus else 0
    if plus:
        widths.append(plus_w)
    row = sum(widths) + gap * max(0, len(widths) - 1)
    x = rect.x + max(0, (rect.w - row) // 2)
    phase = int(epoch) % 2 == 0
    prev = surf.get_clip()
    surf.set_clip(rect)
    amber_bg = hexc("33270F")
    for i, (t, lab, tim) in enumerate(zip(shown, labels, times)):
        w = widths[i]
        box = pygame.Rect(x, rect.y, w, rect.h)
        rad = min(box.w, box.h) // 2
        if t.get("ringing"):
            bg = AMBER if phase else amber_bg
            fg = BG if phase else AMBER
            lc = BG if phase else SPEAKING
        else:
            bg, fg, lc = SURFACE2, TEXT, MUTED
        pygame.draw.rect(surf, bg, box, border_radius=rad)
        if t.get("ringing"):
            pygame.draw.rect(surf, AMBER, box, max(2, round(0.002 * se)), border_radius=rad)
        li, ni = fl.render(lab, True, lc), fn.render(tim, True, fg)
        surf.blit(li, (x + pad_x, rect.y + (rect.h - li.get_height()) // 2))
        surf.blit(ni, (x + w - pad_x - ni.get_width(), rect.y + (rect.h - ni.get_height()) // 2))
        x += w + gap
    if plus:
        box = pygame.Rect(x, rect.y, plus_w, rect.h)
        pygame.draw.rect(surf, SURFACE2, box, border_radius=min(box.w, box.h) // 2)
        pi = fl.render(plus, True, MUTED)
        surf.blit(pi, (x + (plus_w - pi.get_width()) // 2, rect.y + (rect.h - pi.get_height()) // 2))
    surf.set_clip(prev)


def draw_timer_strip(surf, L, epoch=None):
    """Pill chips along the bottom safe edge. No-op (and clears LAST_TIMER_RECT) when nothing is running."""
    global LAST_TIMER_RECT
    if not TIMER_CHIPS:
        LAST_TIMER_RECT = None
        return None
    rect = timer_strip_rect(L)
    surf.fill(BG, rect)
    draw_timer_chips(surf, rect, TIMER_CHIPS, _epoch(epoch))
    LAST_TIMER_RECT = rect
    return rect


def repaint_timer_strip(disp, epoch=None):
    """Redraw only the strip and flush that rectangle. Called once a second while any timer is up."""
    rect = LAST_TIMER_RECT
    if not rect or not TIMER_CHIPS:
        return
    disp.screen.fill(BG, rect)
    draw_timer_chips(disp.screen, rect, TIMER_CHIPS, _epoch(epoch))
    disp.flush_rect(rect)


def card_icon(kind, px):
    mapped = "clear" if kind == "sun" else kind
    if mapped not in ("clear", "partly", "cloud", "fog", "rain", "snow", "storm"):
        return None
    return weather_icon(mapped, True, max(8, px), max(2, px // 12))


def _card_lines(card):
    if card["kind"] == "time":
        lines = [("muted", card["zone_label"])] if card.get("zone_label") else []
        lines.append(("hero", card.get("hhmm", "")))
        meta = "  ·  ".join(p for p in (card.get("day"), card.get("offset")) if p)
        if meta:
            lines.append(("muted", meta))
        return lines
    if card["kind"] == "convert":
        return [("muted", f"{card['value']} {card['from_unit']}"), ("hero", f"= {card['result']} {card['to_unit']}")]
    lines = []
    if "now_temp" in card:
        lines.append(("hero", f"{card['now_temp']}{card.get('units', '')}"))
    if card.get("conditions"):
        lines.append(("sub", card["conditions"]))
    for key, title in (("today", "Today"), ("tomorrow", "Tomorrow")):
        hl = card.get(key)
        if hl:
            lines.append(("muted", f"{title}  {hl['high']}° / {hl['low']}°"))
    return lines


def _role_font(role, hero, sub):
    if role == "hero":
        return "display", hero
    return "body", sub


def draw_card(surf, card, x, y, max_w, max_h):
    """Surface-2 panel. Weather puts the icon beside the text, or above it when the row doesn't fit. Returns the rect."""
    if not card or max_w < 48 or max_h < 28:
        return None
    se = min(surf.get_width(), surf.get_height())
    pad = max(8, round(0.016 * se))
    gap = max(4, round(0.008 * se))
    hero, sub = tpx(se, 0.0627), tpx(se, 0.0328)
    icon_px = max(sub, round(0.07 * se)) if card["kind"] == "weather" and card.get("icon") else 0
    lines = _card_lines(card)
    if not lines and not icon_px:
        return None
    panel_w = min(max_w, max(48, round(0.72 * se)))

    def text_h(hero, sub):
        h = 0
        for i, (role, _text) in enumerate(lines):
            kind, px = _role_font(role, hero, sub)
            h += font(kind, px).get_height()
            if i:
                h += gap
        return h

    def text_w(hero, sub):
        w = 0
        for role, text in lines:
            kind, px = _role_font(role, hero, sub)
            w = max(w, font(kind, px).size(text)[0])
        return w

    for _ in range(6):
        th, tw = text_h(hero, sub), text_w(hero, sub)
        beside = bool(icon_px) and icon_px + gap + tw <= panel_w - 2 * pad
        inner = max(icon_px, th) if beside else ((icon_px + gap) if icon_px else 0) + th
        if inner + 2 * pad <= max_h or (hero <= 14 and sub <= 14):
            break
        if hero > 14:
            hero = max(14, int(hero * 0.85))
        if sub > 14:
            sub = max(14, int(sub * 0.85))
        if icon_px > sub:
            icon_px = max(sub, int(icon_px * 0.85))
    th, tw = text_h(hero, sub), text_w(hero, sub)
    inner_w = panel_w - 2 * pad
    beside = bool(icon_px) and icon_px + gap + min(tw, inner_w) <= inner_w
    inner = max(icon_px, th) if beside else ((icon_px + gap) if icon_px else 0) + th
    rect = pygame.Rect(x, y, panel_w, min(max_h, max(sub + 2 * pad, inner + 2 * pad)))
    rect.clamp_ip(surf.get_rect())
    rad = min(max(6, round(0.019 * se)), rect.w // 2, rect.h // 2)
    pygame.draw.rect(surf, SURFACE2, rect, border_radius=rad)
    prev = surf.get_clip()
    surf.set_clip(rect)
    col_w = inner_w - (icon_px + gap if beside else 0)
    if beside:
        ic = card_icon(card["icon"], icon_px)
        if ic:
            surf.blit(ic, (rect.left + pad, rect.top + (rect.h - icon_px) // 2))
        tx = rect.left + pad + icon_px + gap
        ty = rect.top + max(pad, (rect.h - th) // 2)
        anchor = "left"
    else:
        ty = rect.top + pad
        if icon_px:
            ic = card_icon(card["icon"], icon_px)
            if ic:
                surf.blit(ic, (rect.centerx - icon_px // 2, ty))
            ty += icon_px + gap
        tx = rect.centerx
        anchor = "center"
    for role, text in lines:
        kind, px = _role_font(role, hero, sub)
        f = font(kind, px)
        shown = text if f.size(text)[0] <= col_w else ellipsize(f, text, col_w)
        img = f.render(shown, True, TEXT if role in ("hero", "sub") else MUTED)
        surf.blit(img, (tx if anchor == "left" else tx - img.get_width() // 2, ty))
        ty += img.get_height() + gap
    surf.set_clip(prev)
    return rect


def draw_idle(surf, L, now, wx, colon_on=True):
    global LAST_TOPBAR_RECT, LAST_CARD_RECT, LAST_PORTRAIT_RECT, LAST_PORTRAIT_ARGS
    global LAST_WEATHER_RECT, LAST_OFFLINE_RECT, LAST_DATE_RECT, LAST_CLOCK_RECT
    LAST_TOPBAR_RECT = LAST_CARD_RECT = LAST_PORTRAIT_RECT = LAST_PORTRAIT_ARGS = None
    LAST_WEATHER_RECT = LAST_OFFLINE_RECT = LAST_DATE_RECT = LAST_CLOCK_RECT = None
    surf.fill(BG)
    hhmm, ampm = clock_parts(now)
    top = L(70) + int(L.extra_y * 0.3)
    clock_px = L(320)
    base = top + int(font("display", clock_px).get_ascent() * 0.98)
    x = L.m - L(8)
    digits, _colon = draw_clock(surf, "display", clock_px, hhmm, TEXT, x, base, colon_on)
    ampm_r = baseline_text(surf, "display", L(84), ampm, MUTED, x + font("display", clock_px).size(hhmm)[0] + L(18), base)
    LAST_CLOCK_RECT = digits.union(ampm_r)
    date = time.strftime("%A, %B %-d", now)
    dimg = font("body", L(60)).render(date, True, TEXT)
    dbr = dimg.get_bounding_rect()
    bx = int(round(digits.x + digits.w / 2.0 - (dbr.x + dbr.w / 2.0)))
    date_y = base + L(40)
    surf.blit(dimg, (bx, date_y))
    LAST_DATE_RECT = pygame.Rect(bx, date_y, dimg.get_width(), dimg.get_height())
    if wx.offline():
        draw_offline_badge(surf, L, base)
    elif wx.data:
        draw_weather(surf, L, wx.data, base)
    draw_next_up(surf, L, now)
    draw_timer_strip(surf, L)
    draw_status_icons(surf, L)


def draw_weather(surf, L, d, clock_base):
    global LAST_WEATHER_RECT
    right = L.w - L.m
    label, kind = WMO.get(d["code"], ("", "cloud"))
    temp_px = L(180)
    tb = clock_base - L(80)  # temp baseline sits a little above the clock's
    r = baseline_text(surf, "display", temp_px, f"{d['temp']}°", TEXT, right, tb, align="right")
    icon_px = L(150)
    icon = weather_icon(kind, d["day"], icon_px, max(2, L(7)))
    icon_at = (r.left - icon_px - L(12), tb - L(140))
    surf.blit(icon, icon_at)
    marks = [r, pygame.Rect(icon_at, (icon_px, icon_px))]
    y = tb + L(36)
    if label:
        lr = text_at(surf, "body", L(56), label, TEXT, (right, y), "topright")
        marks.append(lr)
        y = lr.bottom + L(4)
    marks.append(text_at(surf, "body", L(48), f"High {d['hi']}°  ·  Low {d['lo']}°", MUTED, (right, y), "topright"))
    LAST_WEATHER_RECT = marks[0].unionall(marks[1:])


def draw_offline_badge(surf, L, clock_base):
    global LAST_OFFLINE_RECT
    right = L.w - L.m
    well = L(150)
    rect = pygame.Rect(right - well, clock_base - L(250), well, well)
    pygame.draw.rect(surf, SURFACE2, rect, border_radius=L(28))
    ic = offline_icon(L(104), max(2, L(7)))
    surf.blit(ic, ic.get_rect(center=rect.center))
    tr = text_at(surf, "body", L(44), "Weather offline", MUTED, (right, rect.bottom + L(20)), "topright")
    LAST_OFFLINE_RECT = rect.union(tr)


def draw_next_up(surf, L, now):
    """Hook: the lower third is reserved for a rotating "Next up" card once a data source exists."""
    return


def draw_night(surf, L, now, colon_on=True):
    global LAST_TIMER_RECT
    LAST_TIMER_RECT = None  # the dim clock doesn't carry the timer strip
    surf.fill(BLACK)
    hhmm, ampm = clock_parts(now)
    f, fm = font("night", L(340)), font("night", L(90))
    wc, wm = f.size(hhmm)[0], fm.size(ampm)[0]
    shift = (now.tm_min // 10) % 4  # burn-in: drift up to 16px (at 1080p) every 10 minutes
    dx, dy = L((-8, 8, 8, -8)[shift]), L((-8, -8, 8, 8)[shift])
    x = (L.w - (wc + L(20) + wm)) // 2 + dx
    base = L.h // 2 + int(f.get_ascent() * 0.36) + dy
    draw_clock(surf, "night", L(340), hhmm, NIGHT_TEXT, x, base, colon_on)
    baseline_text(surf, "night", L(90), ampm, NIGHT_DIM, x + wc + L(20), base)
    draw_status_icons(surf, L)


def draw_topbar_clock(surf, L, now):
    global LAST_TOPBAR_RECT
    hhmm, ampm = clock_parts(now)
    y = L(34) + L(72) // 2
    icons = draw_status_icons(surf, L)
    r = text_at(surf, "display", L(56), f"{hhmm} {ampm}", TEXT, (icons.left - L(24), y), "midright")
    LAST_TOPBAR_RECT = r
    return r


ENROLL_STAGES = {
    # stage: (ring, label, label color, meter) ; copy is built in enroll_copy()
    "prompt": ("idle", "Ready", MUTED, None),
    "ready": ("enroll_ready", "Get ready", SPEAKING, None),
    "recording": ("listening", "Recording", LISTENING, "live"),
    "loud": ("idle", "Too loud", TEXT, "full"),
    "quiet": ("idle", "Try again", TEXT, "flat"),
    "saved": ("enroll_saved", "Saved", LISTENING, None),
    "uploaded": ("success", "Done", LISTENING, None),
    "queued": ("thinking", "Queued", THINKING, None),
    "passed": ("success", "Ready", LISTENING, None),
    "failed": ("idle", "Not yet", MUTED, None),
    "error": ("idle", "Stopped", MUTED, None),
}
ENROLL_ANIM = {"ready": ("enroll_ready", 0.9, False), "saved": ("enroll_saved", 0.3, False),
               "recording": ("listening", PULSE_PERIOD, True), "queued": ("thinking", SPIN_PERIOD, True)}


def enroll_parse(msg):
    """heard = "i/n|stage|extra" (extra: queue position for queued, top-up size for failed). Old free-text status
    lines ("3/25|Recording… speak now") map to stage "custom"."""
    parts = (msg.get("heard") or "").split("|")
    m = re.match(r"(\d+)/(\d+)$", parts[0].strip()) if parts else None
    i, n = (int(m.group(1)), max(1, int(m.group(2)))) if m else (0, 0)
    stage = parts[1].strip() if len(parts) > 1 else ""
    extra = parts[2].strip() if len(parts) > 2 else ""
    if stage not in ENROLL_STAGES:
        stage, extra = "custom", stage
    return i, n, stage, extra


def anim_of(msg):
    """(ring state, period s, loops) for a message whose portrait ring animates, else None. Enrollment stages animate
    too: the get-ready fill (0.9 s, once), the clip-saved pulse (0.3 s, once), recording pulse, queued spin."""
    if not msg or not msg.get("voice"):
        return None
    st = msg.get("state")
    if st == "enroll":
        return ENROLL_ANIM.get(enroll_parse(msg)[2])
    return (st, ANIM[st], True) if st in ANIM else None


def ordinal(k):
    return f"{k}{'th' if 10 <= k % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(k % 10, 'th')}"


def enroll_copy(phrase, i, n, stage, extra):
    """(headline, sub-line, sub color, status line or None, bar fill 0..1 or None) for one enrollment screen."""
    q = f"“{phrase}”"
    clip = f"Clip {i} of {n}"
    bar = (i - 1) / n if n else None
    if stage == "prompt":
        return f"Say {q}", "Say it the way you normally would, after each chime.", MUTED, \
            f"{n} clips · about {max(1, round(n * 6 / 60))} min", 0.0
    if stage == "ready":
        return "Get ready…", f"Say {q} right after the chime.", MUTED, clip, bar
    if stage == "recording":
        return f"Say {q}", "Speak now", LISTENING, clip, bar
    if stage == "loud":
        return "Too loud", f"Step back a little or speak softer. Clip {i} will retry.", MUTED, clip, bar
    if stage == "quiet":
        return "Didn’t hear it", f"Say {q} right after the chime. Clip {i} will retry.", MUTED, clip, bar
    if stage == "saved":
        nxt = f"Clip {min(i + 1, n)} of {n}"
        return "Got it", f"Clip {i} saved. Next one in a moment.", MUTED, nxt, i / n if n else None
    if stage == "uploaded":
        return f"All {n} clips saved", "Uploaded for training. You can use the station as usual.", MUTED, "Uploaded ✓", 1.0
    if stage == "queued":
        k = int(extra) if extra.isdigit() else 1
        when = "training now, about 10 min" if k <= 1 else f"{ordinal(k)} in line, about {10 * k} min"
        return "Training queued", f"{q} is {when}. We’ll show the result here.", MUTED, None, None
    if stage == "passed":
        return f"{q} is ready", "Try it now.", MUTED, None, None
    if stage == "failed":
        more = int(extra) if extra.isdigit() else 10
        return f"Needs {more} more clips", f"A short top-up will make {q} more reliable.", MUTED, None, None
    if stage == "error":
        return "Enrollment stopped", "Nothing was lost. Try again later.", MUTED, None, None
    return f"Say {q}" if phrase else "Wake word setup", extra, MUTED, clip if n else None, bar


def draw_meter(surf, L, cx, cy, kind):
    """7 rounded bars under the state label: live (teal, varied), full (text color: too loud), flat (faint: nothing)."""
    bw, gap, hmax = max(2, L(16)), max(2, L(12)), L(90)
    hs = {"live": (0.35, 0.6, 0.85, 1.0, 0.75, 0.5, 0.3), "full": (1.0,) * 7, "flat": (0.08,) * 7}[kind]
    color = {"live": LISTENING, "full": TEXT, "flat": FAINT}[kind]
    x0 = cx - (7 * bw + 6 * gap) // 2
    for k, hh in enumerate(hs):
        bh = max(bw // 2, int(hmax * hh))
        pygame.draw.rect(surf, color, (x0 + k * (bw + gap), cy - bh // 2, bw, bh), border_radius=bw // 2)


def draw_enroll(surf, L, now, msg, t=1.0):
    """Enrollment screens 01-10 (docs/design.md, mockups): avatar with the stage ring at the left, headline, sub-line,
    "Clip N of M" status and progress bar at the right; muted "Wake word setup · <name>" top bar. The persona's
    portrait, or the Echo mark on a surface-2 circle for a wake word without a persona."""
    global LAST_PORTRAIT_RECT, LAST_PORTRAIT_ARGS
    surf.fill(BG)
    draw_topbar_clock(surf, L, now)
    i, n, stage, extra = enroll_parse(msg)
    prompt = msg.get("text") or ""
    m = re.search(r"[“\"](.+?)[”\"]", prompt)
    phrase = m.group(1) if m else prompt
    who = msg.get("from") or VOICE_NAME
    top = f"Wake word setup · {who if who != VOICE_NAME else (phrase or who)}"
    ft = font("body", L(54))
    text_at(surf, "body", L(54), top if ft.size(top)[0] <= L.w // 2 else ellipsize(ft, top, L.w // 2), MUTED,
            (L.m, L(34) + L(72) // 2), "midleft")
    ring, label, lcolor, meter = ENROLL_STAGES.get(stage, ("idle", "", MUTED, None))
    d, ring_w = L(500), max(4, L(14))
    left = L(160)
    x = left + d + L(140)
    width = L.w - L.m - x
    top_row = L(34) + L(72)
    col_h = d + L(110) + L(40) + (L(60) + L(45) if meter else 0)  # avatar, state label, level meter
    photo_top = top_row + max(L(30), (L.h - top_row - L(60) - col_h) // 2)
    img, pad = portrait(who, d, ring_w, ring, t, photo_ok=True)
    origin = (left - pad, photo_top - pad)
    surf.blit(img, origin)
    LAST_PORTRAIT_RECT = pygame.Rect(origin, img.get_size())
    LAST_PORTRAIT_ARGS = (who, d, ring_w, True)
    acx = left + d // 2
    if label:
        lr = text_at(surf, "display", L(64), label, lcolor, (acx, photo_top + d + L(110)), "center")
        if meter:
            draw_meter(surf, L, acx, lr.bottom + L(60), meter)
    head, sub, scolor, status, bar = enroll_copy(phrase, i, n, stage, extra)
    fh, hl, hstep = fit_text("display", head, width, L(240), L(120), L(64), lh=1.02, max_lines=2)
    fs, sl, sstep = fit_text("body", sub, width, L(170), L(68), L(40), lh=1.18, max_lines=2) if sub else (None, [], 0)
    st_h = L(80) + L(30) if status else 0
    bar_h = L(24) + L(28) if bar is not None else 0
    block = hstep * len(hl) + (L(16) + sstep * len(sl) if sl else 0) + (L(28) + st_h if status else 0) + bar_h
    y = max(top_row + L(40), photo_top + d // 2 - block // 2)
    y = draw_lines(surf, fh, hl, hstep, TEXT, x, y)
    if sl:
        y = draw_lines(surf, fs, sl, sstep, scolor, x, y + L(16))
    if status:
        fst = font("display", L(80))
        check = status.endswith(" ✓")  # Inter has no check glyph: draw it
        stext = status[:-2] if check else status
        stext = stext if fst.size(stext)[0] <= width - L(80) else ellipsize(fst, stext, width - L(80))
        r = text_at(surf, "display", L(80), stext, LISTENING, (x, y + L(28)), "topleft")
        if check:
            u, cy = L(40), r.centery
            pts = [(r.right + L(22), cy), (r.right + L(22) + u * 0.38, cy + u * 0.38), (r.right + L(22) + u, cy - u * 0.5)]
            pygame.draw.lines(surf, LISTENING, False, pts, max(2, L(8)))
        y = r.bottom + L(30)
    if bar is not None:
        track = pygame.Rect(x, y, width, L(24))
        pygame.draw.rect(surf, SURFACE2, track, border_radius=L(12))
        if bar > 0:
            pygame.draw.rect(surf, LISTENING, (x, y, max(L(24), int(width * min(1.0, bar))), L(24)), border_radius=L(12))


T1_VERBS = {"add": "add to", "remove": "remove from", "remember": "remember", "undo": "undo"}


def draw_confirm(surf, L, now, msg):
    """T1 read-back card: the exact note text, and how to confirm. heard = "op|list|needs_tap" (from the wake listener)."""
    surf.fill(BG)
    w, h = surf.get_size()
    se = min(w, h)
    m = round(0.075 * se)
    who = msg.get("from") or "Grok"
    op, lst, tap = (str(msg.get("heard", "")).split("|") + ["", "", ""])[:3]
    topbar(surf, se, f"{who} · Voice note", now)
    verb = T1_VERBS.get(op, "save")
    where = (f"the {lst} list" if lst else "your notes") if op in ("add", "remove") else ""
    head = f"{who} will {verb} {where}:".replace("  ", " ") if verb != "undo" else f"{who} will undo the note:"
    y = round(0.17 * se)
    hr = font("display", round(0.0627 * se)).render(head, True, MUTED)
    surf.blit(hr, (m, y))
    y += hr.get_height() + round(0.03 * se)
    box = pygame.Rect(m, y, w - 2 * m, round(0.36 * se))
    pygame.draw.rect(surf, SURFACE, box, border_radius=round(0.0188 * se))
    pygame.draw.rect(surf, AMBER, box, max(2, round(0.0018 * se)), border_radius=round(0.0188 * se))
    pad = round(0.03 * se)
    f, lines, step = fit_text("display", msg.get("text", ""), box.w - 2 * pad, box.h - 2 * pad, round(0.0938 * se), round(0.04 * se), max_lines=3)
    draw_lines(surf, f, lines, step, TEXT, box.x + pad, box.y + pad)
    y = box.bottom + round(0.04 * se)
    how = "Press the call button on the speaker to save." if tap == "1" else "Say “yes, save it”, or press the call button."
    hw = font("display", round(0.0563 * se)).render(how, True, LISTENING)
    surf.blit(hw, (m, y))
    foot = font("body", round(0.0328 * se)).render("Anything else cancels. Nothing is saved until you confirm.", True, MUTED)
    surf.blit(foot, (m, h - round(0.0657 * se) - foot.get_height()))


def thinking_body(text):
    """Drop a placeholder 'Thinking' body so it isn't repeated under the transcript."""
    t = (text or "").strip().rstrip(".\u2026").strip()
    return "" if t.lower() in ("", "thinking") else text


def _voice_text_width(L, who, heard, text, max_w):
    """Natural width of the side column (name, heard, reply) at most max_w, so the group can be centered."""
    fn = font("display", L(120))
    w = fn.size(who)[0]
    if heard:
        fh, lh, _ = fit_text("italic", f"“{heard}”", max_w, L(130), L(48), L(40), lh=1.02, max_lines=2)
        w = max([w] + [fh.size(x)[0] for x in lh])
    if text:
        f, lines, _ = fit_text("body", text, max_w, max(L(40), L.h - L(400)), L(76), L(40), lh=1.06)
        w = max([w] + [f.size(x)[0] for x in lines])
    return max(L(200), min(max_w, w + L(8)))


def _draw_voice_column(surf, L, who, heard, text, x, width, top_min, bottom, photo_top, d, card, stack):
    """Name, heard quote, reply and (when present) the tool card. Returns the y under the block."""
    global LAST_CARD_RECT
    fn = font("display", L(120))
    name = who if fn.size(who)[0] <= width else ellipsize(fn, who, width)
    name_h = int(fn.get_ascent() * 0.98)
    heard_part = fit_text("italic", f"“{heard}”", width, L(130), L(48), L(40), lh=1.02, max_lines=2) if heard else None
    heard_h = heard_part[2] * len(heard_part[1]) + L(28) if heard else 0
    if not card:
        avail = bottom - top_min - name_h - L(20) - heard_h
        f, lines, step = fit_text("body", text, width, avail, L(76), L(40), lh=1.06)
        block = name_h + L(20) + heard_h + step * len(lines)
        y = top_min if stack else max(top_min, min(photo_top + d // 2 - block // 2, bottom - block))
        baseline_text(surf, "display", L(120), name, TEXT, x - L(4), y + int(fn.get_ascent() * 0.78))
        y += name_h + L(20)
        if heard_part:
            y = draw_lines(surf, *heard_part, MUTED, x, y) + L(28)
        draw_lines(surf, f, lines, step, TEXT, x, y)
        LAST_CARD_RECT = None
        return y + step * len(lines)
    se = min(L.w, L.h)
    card_gap = L(16)
    room = bottom - top_min - name_h - L(20) - heard_h - card_gap
    pref = min(max(0, room), max(tpx(se, 0.16), round(0.22 * se)))
    avail = room - pref
    if avail < L(40):
        f, lines, step = font("body", L(40)), [], L(40)
        pref = max(0, room)
    else:
        f, lines, step = fit_text("body", text, width, avail, L(76), L(40), lh=1.06)
    block = name_h + L(20) + heard_h + step * len(lines) + card_gap + pref
    y = top_min if stack else max(top_min, min(photo_top + d // 2 - block // 2, bottom - block))
    if stack:
        img = fn.render(name, True, TEXT)
        surf.blit(img, (x + (width - img.get_width()) // 2, y))
    else:
        baseline_text(surf, "display", L(120), name, TEXT, x - L(4), y + int(fn.get_ascent() * 0.78))
    y += name_h + L(20)
    if heard_part:
        y = draw_lines(surf, *heard_part, MUTED, x, y, "center" if stack else "left", width if stack else 0) + L(28)
    if lines:
        y = draw_lines(surf, f, lines, step, TEXT, x, y, "center" if stack else "left", width if stack else 0)
        y += card_gap
    LAST_CARD_RECT = draw_card(surf, card, x, y, width, max(0, bottom - y))
    return (LAST_CARD_RECT.bottom if LAST_CARD_RECT else y)


def draw_session(surf, L, now, msg, state="speaking", t=1.0):
    global LAST_PORTRAIT_RECT, LAST_PORTRAIT_ARGS, LAST_CARD_RECT, LAST_TOPBAR_RECT
    global LAST_CLOCK_RECT, LAST_DATE_RECT, LAST_WEATHER_RECT, LAST_OFFLINE_RECT
    LAST_CARD_RECT = LAST_TOPBAR_RECT = None
    LAST_CLOCK_RECT = LAST_DATE_RECT = LAST_WEATHER_RECT = LAST_OFFLINE_RECT = None
    state = msg.get("state", state)
    if state == "enroll":
        draw_enroll(surf, L, now, msg, t)
        draw_timer_strip(surf, L)
        return
    if state == "confirm":
        return draw_confirm(surf, L, now, msg)
    surf.fill(BG)
    draw_status_icons(surf, L)  # no clock while a chat is up
    who, heard, text = msg.get("from", ""), msg.get("heard", ""), msg["text"]
    card = clean_card(msg.get("card")) if isinstance(msg.get("card"), dict) else None
    top_row = L(34) + L(72)
    if not who:
        LAST_PORTRAIT_RECT = LAST_PORTRAIT_ARGS = None
        # plain {text}: big centered message, heard (if any) above it
        width = L.w - 2 * L.m
        y0, y1 = top_row + L(60), L.h - L(110)
        if card or TIMER_CHIPS:
            y1 = min(y1, content_limit(L))
        if heard:
            fh, lh, sh = fit_text("italic", f"“{heard}”", width, L(140), L(48), L(40), max_lines=2)
            draw_lines(surf, fh, lh, sh, MUTED, L.m, y0, "center", width)
            y0 += sh * len(lh) + L(40)
        reserve = 0 if not card else min(max(0, y1 - y0), round(0.28 * min(L.w, L.h)))
        text_h = max(0, y1 - y0 - reserve - (L(12) if card else 0))
        f, lines, step = fit_text("display", text, width, text_h or 1, L(180), L(48), lh=1.0)
        if card and text_h < L(48):
            lines = []
        block_h = step * len(lines) + (L(12) + reserve if card and lines else 0)
        y = y0 + max(0, (y1 - y0 - (block_h or step)) // 2) if not card else y0
        if lines:
            draw_lines(surf, f, lines, step, TEXT, L.m, y, "center", width)
            y += step * len(lines) + (L(12) if card else 0)
        if card:
            LAST_CARD_RECT = draw_card(surf, card, L.m, y, width, max(0, y1 - y))
        draw_timer_strip(surf, L)
        return
    d, ring_w = L(500), max(4, L(14))
    se = min(L.w, L.h)
    gap_col = L(140)
    side, _top, _bot = safe_insets(L.w, L.h)
    safe_w = L.w - 2 * side
    # sized by the short edge; wrap under the avatar only when that row is wider than the safe area (4:3)
    stack = bool(card) and d + gap_col + min(round(0.8 * se), safe_w) > safe_w
    if stack:
        width = min(round(0.8 * se), safe_w)
        left = (L.w - d) // 2
        x = (L.w - width) // 2
        photo_top = top_row + L(8)
    else:
        # avatar + text block centered as one group, both ways
        max_w = L.w - L.m - L(160) - d - gap_col
        width = min(max_w, _voice_text_width(L, who, heard, thinking_body(text) if state == "thinking" else text, max_w)) if not card else max_w
        left = max(L(160), (L.w - (d + gap_col + width)) // 2)
        x = left + d + gap_col
        label_room = L(150) + L(36) if state in ("listening", "thinking") else 0
        photo_top = max(top_row, (L.h - d - label_room) // 2)
    bottom = L.h - L(96)
    if card or TIMER_CHIPS:
        bottom = min(bottom, content_limit(L))
    img, pad = portrait(who, d, ring_w, state, t, photo_ok=bool(msg.get("voice")))
    origin = (left - pad, photo_top - pad)
    surf.blit(img, origin)
    LAST_PORTRAIT_RECT = pygame.Rect(origin, img.get_size())
    LAST_PORTRAIT_ARGS = (who, d, ring_w, bool(msg.get("voice")))
    label_bottom = photo_top + d
    label_y = photo_top + d + L(150)
    if TIMER_CHIPS:  # keep the state label above the strip; unchanged when no timer is up
        label_y = min(label_y, max(photo_top + d + L(16), content_limit(L) - L(36)))
    if state == "listening":
        lr = text_at(surf, "display", L(64), "Listening", LISTENING, (left + d // 2 + L(40), label_y), "center")
        for i, hh in enumerate((0.45, 0.8, 1.0, 0.65, 0.35)):  # small static waveform glyph
            bh = int(L(56) * hh)
            pygame.draw.rect(surf, LISTENING, (lr.left - L(110) + i * L(18), lr.centery - bh // 2, L(9), bh), border_radius=L(5))
        label_bottom = lr.bottom
    elif state == "thinking":
        text = thinking_body(text)  # the label under the avatar already says Thinking
        label_bottom = text_at(surf, "display", L(64), "Thinking", THINKING, (left + d // 2, label_y), "center").bottom
    top_min = max(top_row + L(40), label_bottom + L(12)) if stack else top_row + L(40)
    _draw_voice_column(surf, L, who, heard, text, x, width, top_min, bottom, photo_top, d, card, stack)
    draw_timer_strip(surf, L)


# ---------------------------------------------------------------- setup needed / password reset / hotspot (admin page)
def read_setup():
    try:
        with open(SETUP_FILE) as f:
            d = json.load(f)
        if d.get("mode") == "key":  # password set, xAI key still missing: no code to show
            return d
        return d if d.get("mode") in ("setup", "reset", "hotspot") and re.fullmatch(r"[2-9A-HJ-NP-Z]{6}", str(d.get("code", ""))) else None
    except (OSError, ValueError, AttributeError):
        return None


def lock_ring(surf, cx, cy, d, ring_w):
    pygame.draw.circle(surf, hexc("33270F"), (cx, cy), d // 2)
    pygame.draw.circle(surf, AMBER, (cx, cy), d // 2, ring_w)
    u = d / 24 * 0.5  # icon is half the ring diameter on a 24-unit grid
    x0, y0 = cx - 12 * u, cy - 12 * u
    sw = max(2, round(1.6 * u))
    body = pygame.Rect(round(x0 + 5 * u), round(y0 + 11 * u), round(14 * u), round(10 * u))
    pygame.draw.rect(surf, AMBER, body, sw, border_radius=round(2 * u))
    arc = pygame.Rect(round(x0 + 8 * u), round(y0 + 4 * u), round(8 * u), round(8 * u))
    pygame.draw.arc(surf, AMBER, arc, 0, 3.1416, sw)
    pygame.draw.line(surf, AMBER, (arc.left + sw // 2, arc.centery), (arc.left + sw // 2, body.top), sw)
    pygame.draw.line(surf, AMBER, (arc.right - sw // 2 - 1, arc.centery), (arc.right - sw // 2 - 1, body.top), sw)


def code_box(surf, se, code, x, y, code_px=0.0938, align="left"):
    """Amber-bordered setup-code box (label + 3+3 code). Returns its rect."""
    lab = font("body", round(0.0328 * se)).render("Setup code", True, MUTED)
    f = font("mono", round(code_px * se))
    txt = f.render(f"{code[:3]} {code[3:]}", True, SPEAKING)
    padx, pady, gap = round(0.0375 * se), round(0.0164 * se), round(0.0082 * se)
    w = max(lab.get_width(), txt.get_width()) + 2 * padx
    h = lab.get_height() + gap + txt.get_height() + 2 * pady
    r = pygame.Rect(0, y, w, h)
    if align == "center":
        r.centerx = x
    else:
        r.left = x
    pygame.draw.rect(surf, SURFACE, r, border_radius=round(0.0188 * se))
    pygame.draw.rect(surf, AMBER, r, max(2, round(0.0018 * se)), border_radius=round(0.0188 * se))
    surf.blit(lab, (r.centerx - lab.get_width() // 2, r.top + pady))
    surf.blit(txt, (r.centerx - txt.get_width() // 2, r.top + pady + lab.get_height() + gap))
    return r


def topbar(surf, se, label, now):
    m = round(0.075 * se)
    y = round(0.0516 * se)
    lab = font("body", round(0.0422 * se)).render(label, True, MUTED)
    hhmm, ampm = clock_parts(now)
    clk = font("display", round(0.0563 * se)).render(f"{hhmm} {ampm}", True, hexc("D9CFC2"))
    hgt = max(lab.get_height(), clk.get_height())
    surf.blit(lab, (m, y + (hgt - lab.get_height()) // 2))
    clk_y = y + (hgt - clk.get_height()) // 2
    lay = Layout(surf.get_width(), surf.get_height())
    icons = draw_status_icons(surf, lay)
    clk_x = icons.left - max(1, int(round(24 * lay.s))) - clk.get_width()
    surf.blit(clk, (clk_x, clk_y))


def draw_setup(surf, L, now, d):
    """Setup needed (first run) or Password reset: amber lock ring beside (or above, on narrow screens) the steps."""
    surf.fill(BG)
    w, h = surf.get_size()
    se = min(w, h)
    m = round(0.075 * se)
    reset = d.get("mode") == "reset"
    key = d.get("mode") == "key"
    host = str(d.get("host") or "grok-station.local")[:40]
    host = str(d.get("url") or "https://" + host)[:48]
    ip = str(d.get("ip") or "")
    ip = ip if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) else ""
    lines = [("display", 0.0938, "Almost done" if key else "Password reset" if reset else "Setup needed", TEXT, 0),
             ("body", 0.0498, "To finish setup, open one of these on your phone or laptop", hexc("E6DCCF"), 0.0164),
             ("display", 0.0627, host, LISTENING, 0.0059),
             *([("display", 0.0627, "https://" + ip, LISTENING, 0.0059)] if ip else []),
             ("body", 0.0498, "and add your xAI API key" if key else "and enter the new code:" if reset else "and enter this code:",
              hexc("E6DCCF"), 0.0164)]
    rend = [(font(k, round(px * se)).render(t, True, c), round(gap * se)) for k, px, t, c, gap in lines]
    ring_d, ring_w, gap = round(0.2345 * se), max(3, round(0.0111 * se)), round(0.0703 * se)
    code_h = 0 if key else round(0.0328 * se * 1.25 + 0.0938 * se * 1.2 + 0.0082 * se + 2 * 0.0164 * se)
    text_w = max(r.get_width() for r, _ in rend)
    text_h = sum(r.get_height() + g for r, g in rend) + (0 if key else round(0.0141 * se)) + code_h
    top, bottom = 0, h  # the whole group (ring + text + code) is centered on the screen both ways
    side = ring_d + gap + text_w <= w - 2 * m
    if side:
        x0 = (w - (ring_d + gap + text_w)) // 2
        cy = (top + bottom) // 2
        lock_ring(surf, x0 + ring_d // 2, cy, ring_d, ring_w)
        x, y = x0 + ring_d + gap, cy - text_h // 2
        align = "left"
    else:  # stacked: ring above, text centered
        ring_d = round(ring_d * 0.7)
        total = ring_d + round(0.03 * se) + text_h
        y0 = max(top, (top + bottom - total) // 2)
        lock_ring(surf, w // 2, y0 + ring_d // 2, ring_d, ring_w)  # stacked: centered column
        x, y, align = w // 2, y0 + ring_d + round(0.03 * se), "center"
    for r, g in rend:
        y += g
        surf.blit(r, (x if align == "left" else x - r.get_width() // 2, y))
        y += r.get_height()
    if not key:
        code_box(surf, se, d["code"], x, y + round(0.0141 * se), align=align)


def draw_qr(surf, matrix, x, y, size):
    """QR at an integer module size with a 2-module quiet zone on a warm-white rounded tile. Returns the tile rect."""
    n = len(matrix)
    mod = max(1, size // (n + 4))
    tile = pygame.Rect(x, y, mod * (n + 4), mod * (n + 4))
    pygame.draw.rect(surf, WARM_WHITE, tile, border_radius=max(4, round(tile.w * 0.05)))
    for r, row in enumerate(matrix):
        for c, bit in enumerate(row):
            if bit == "1":
                surf.fill(BG, (x + (c + 2) * mod, y + (r + 2) * mod, mod, mod))
    return tile


def draw_hotspot(surf, L, now, d):
    """No network: three numbered steps (join Wi-Fi QR, open setup QR, enter the code)."""
    surf.fill(BG)
    w, h = surf.get_size()
    se = min(w, h)
    m = round(0.075 * se)
    topbar(surf, se, "No network · Setup hotspot on", now)
    head = font("display", round(0.0762 * se)).render("Connect to set me up", True, TEXT)
    hy = round(0.1524 * se)
    surf.blit(head, ((w - head.get_width()) // 2, hy))
    cols = 3
    col_w = (w - 2 * m) // cols
    qr = min(round(0.276 * se), col_w - round(0.02 * se))
    y0 = hy + head.get_height() + round(0.04 * se)
    steps = [("Join my Wi-Fi", d.get("qr_join"), [(str(d.get("ssid", ""))[:32], LISTENING), (str(d.get("password", ""))[:32], TEXT)]),
             ("Open setup", d.get("qr_open"), [(str(d.get("url", "https://10.42.0.1"))[:40], LISTENING)]),
             ("Enter the code", None, [])]
    for i, (title, matrix, sub) in enumerate(steps):
        cx = m + col_w * i + col_w // 2
        num_d = round(0.0516 * se)
        tf = font("display", round(0.0498 * se))
        t = tf.render(title, True, TEXT)
        if num_d + round(0.015 * se) + t.get_width() > col_w:
            tf = font("display", round(0.0498 * se * col_w / (num_d + t.get_width() + round(0.015 * se) + 1)))
            t = tf.render(title, True, TEXT)
        rowx = cx - (num_d + round(0.015 * se) + t.get_width()) // 2
        pygame.draw.circle(surf, SPEAKING, (rowx + num_d // 2, y0 + num_d // 2), num_d // 2)
        n = font("display", round(num_d * 0.6)).render(str(i + 1), True, BG)
        surf.blit(n, (rowx + (num_d - n.get_width()) // 2, y0 + (num_d - n.get_height()) // 2))
        surf.blit(t, (rowx + num_d + round(0.015 * se), y0 + (num_d - t.get_height()) // 2))
        y = y0 + num_d + round(0.03 * se)
        if i < 2:
            if matrix:
                tile = draw_qr(surf, matrix, cx - qr // 2, y, qr)
            else:
                tile = pygame.Rect(cx - qr // 2, y, qr, qr)
                pygame.draw.rect(surf, SURFACE2, tile, border_radius=round(qr * 0.05))
            y = tile.bottom + round(0.015 * se)
            for text, color in sub:
                f = font("mono", round(0.0352 * se))
                r = f.render(text, True, color)
                surf.blit(r, (cx - r.get_width() // 2, y))
                y += r.get_height()
        else:
            code_box(surf, se, d["code"], cx, y + qr // 4, code_px=min(0.0692, 0.0692 * col_w / (0.42 * se)), align="center")
    foot = font("body", round(0.0328 * se)).render("Plug in a network cable instead and this goes away", True, MUTED)
    surf.blit(foot, (m, h - round(0.0657 * se) - foot.get_height()))


def screen_for(setup_doc, busy, msg, night):
    """Which screen paint() shows. Setup (or hotspot) until setup is done; after that a conversation, the night
    clock, or the idle clock with date and weather. Nothing else (no certificate or pairing screen) can take over."""
    if setup_doc:
        return "hotspot" if setup_doc.get("mode") == "hotspot" else "setup"
    if busy:
        return "thinking"
    if msg:
        return "session"
    return "night" if night else "idle"


SCREENS = {"idle": draw_idle, "night": draw_night, "session": draw_session, "setup": draw_setup, "hotspot": draw_hotspot}


# ---------------------------------------------------------------- output
MIN_SIZE, MAX_SIZE = (800, 480), (3840, 2160)  # supported framebuffer range (README "Resolution")


def canvas_rect(w, h):
    """Drawing area inside a w x h framebuffer: the whole screen within the supported range; larger screens are
    letterboxed (centered, at most MAX_SIZE per axis). Smaller than MIN_SIZE draws at native size (best effort)."""
    cw, ch = min(w, MAX_SIZE[0]), min(h, MAX_SIZE[1])
    return pygame.Rect((w - cw) // 2, (h - ch) // 2, cw, ch)


class Display:
    def __init__(self, fb=FB, size=None):
        self.fb = fb
        if size:
            self.full = pygame.Surface(size, depth=16)
        else:
            sysfs = "/sys/class/graphics/" + os.path.basename(fb)
            w, h = map(int, open(f"{sysfs}/virtual_size").read().split(","))
            bpp = int(open(f"{sysfs}/bits_per_pixel").read())
            stride = int(open(f"{sysfs}/stride").read())
            self.full = pygame.Surface((w, h), depth=bpp)
            if self.full.get_pitch() != stride:
                raise SystemExit(f"fb stride {stride} != surface pitch {self.full.get_pitch()}")
        fw, fh = self.full.get_size()
        if fw < MIN_SIZE[0] or fh < MIN_SIZE[1]:
            print(f"display {fw}x{fh} is below the supported {MIN_SIZE[0]}x{MIN_SIZE[1]}; drawing best effort", flush=True)
        self.view = canvas_rect(fw, fh)
        self.full.fill(BG)
        # painters draw into the canvas; a subsurface shares the framebuffer's pixels, so letterboxing is free
        self.screen = self.full if self.view.size == (fw, fh) else self.full.subsurface(self.view)
        self.L = Layout(*self.screen.get_size())

    def flush(self):
        if self.fb:
            with open(self.fb, "wb") as f:  # 16bpp RGB565 surface matches the vc4 fbdev layout
                f.write(self.full.get_buffer().raw)

    def flush_rect(self, rect):
        """Write only rect's rows/columns. fb=None (demo, tests) is a no-op."""
        if not self.fb or not rect:
            return
        rect = pygame.Rect(rect).clip(self.screen.get_rect()).move(self.view.topleft)  # canvas -> framebuffer coords
        if rect.w <= 0 or rect.h <= 0:
            return
        bpp, pitch = self.full.get_bytesize(), self.full.get_pitch()
        raw = self.full.get_buffer().raw
        x, y, w, h = rect
        with open(self.fb, "r+b") as f:
            for row in range(y, y + h):
                off = row * pitch + x * bpp
                f.seek(off)
                f.write(raw[off:off + w * bpp])

    def show(self, painter, *args, fade=False, snapshot=True):
        if fade and FADE:
            nxt = self.screen.copy()
            painter(nxt, self.L, *args)
            steps = 8
            for i in range(steps):  # crossfade without a third full-screen buffer
                nxt.set_alpha(int(255 / (steps - i)))
                self.screen.blit(nxt, (0, 0))
                self.flush()
                time.sleep(0.04)
            del nxt
        else:
            painter(self.screen, self.L, *args)
            self.flush()
        if snapshot and SNAP:
            try:
                pygame.image.save(self.full, SNAP)
            except Exception:
                pass


def main():
    global LAST_TIMER_RECT
    pygame.display.init()  # dummy driver: needed for the event queue only
    pygame.font.init()
    disp = Display()
    wx = Weather()
    print(f"display {disp.screen.get_size()} fb={FB} assets={ASSETS}", flush=True)
    msg, until, last_key, anim_started = None, 0.0, None, None
    card_hold = False   # the card hold after a session that showed a card has started
    strip_at = 0.0      # last time the timer strip was painted; ticks are at least 0.9 s apart

    def thinking():  # any animated ring (thinking spin, listening pulse, enrollment fill/pulse until it finishes)
        a = anim_of(msg)
        if not a or not anim_started:
            return False
        return a[2] or time.time() - anim_started <= a[1] + 0.1

    def phase_now():
        _, period, loops = anim_of(msg)
        x = (time.time() - anim_started) / period
        return x % 1.0 if loops else min(1.0, x)

    setup = {"doc": None, "checked": 0.0}

    def paint(fade=False, snapshot=True):
        nonlocal last_key, strip_at
        global LAST_TIMER_RECT
        now = time.localtime()
        on = colon_blink_on()
        sd = setup["doc"]
        if sd:  # until setup is done this is the idle screen and the screen on wake (voice is off)
            LAST_TIMER_RECT = None
            disp.show(draw_hotspot if sd["mode"] == "hotspot" else draw_setup, now, sd, fade=fade, snapshot=snapshot)
            last_key = ("setup", now.tm_min, json.dumps(sd, sort_keys=True))
            return
        view = screen_for(sd, thinking(), msg, is_night(now))
        if view == "thinking":
            disp.show(draw_session, now, msg, msg["state"], phase_now(), fade=fade, snapshot=snapshot)
            key = ("session", now.tm_min)
        elif view == "session":
            disp.show(draw_session, now, msg, fade=fade, snapshot=snapshot)
            key = ("session", now.tm_min)
        elif view == "night":
            disp.show(draw_night, now, on, fade=fade, snapshot=snapshot)
            key = ("night", now.tm_min)
        else:
            disp.show(draw_idle, now, wx, on, fade=fade, snapshot=snapshot)
            key = ("idle", now.tm_min, wx.offline(), json.dumps(wx.data))
        last_key = key
        if TIMER_CHIPS and key[0] != "night":
            strip_at = time.time()

    spin_frames = {}  # (args, frame) -> opaque screen-format portrait; one revolution is rendered once, then reused

    def spin_portrait():
        """One animated-ring frame (thinking spin or listening pulse): blit a cached frame into the portrait rect and
        flush only that rect. One loop per (portrait, state) is rendered once, then reused."""
        rect, args = LAST_PORTRAIT_RECT, LAST_PORTRAIT_ARGS
        if not rect or not args or anim_started is None:
            return
        state, period, loops = anim_of(msg)
        n = max(1, round(period * spin_fps()))
        i = min(n, int(phase_now() * n)) if not loops else int(phase_now() * n) % n
        if any(k[0] != args for k in spin_frames):
            spin_frames.clear()
        frame = spin_frames.get((args, state, i))
        if frame is None:
            who, d, ring_w, photo_ok = args
            frame = pygame.Surface(rect.size, depth=disp.screen.get_bitsize())
            frame.fill(BG)
            frame.blit(portrait(who, d, ring_w, state, i / n, photo_ok=photo_ok)[0], (0, 0))
            spin_frames[(args, state, i)] = frame
        disp.screen.blit(frame, rect.topleft)
        disp.flush_rect(rect)

    def blink_colon():
        """Redraw only the colon glyph (or the background behind it) and flush that rect."""
        if not LAST_COLON_RECT or not LAST_COLON_GLYPH:
            return
        bg = BLACK if last_key and last_key[0] == "night" else BG
        disp.screen.fill(bg, LAST_COLON_RECT)
        if colon_blink_on():
            disp.screen.blit(*LAST_COLON_GLYPH)
        disp.flush_rect(LAST_COLON_RECT)

    def maybe_strip():
        """Countdown tick: redraw the timer strip only, and not on the night clock."""
        nonlocal strip_at
        if not TIMER_CHIPS or setup["doc"] or (last_key and last_key[0] == "night"):
            return
        now = time.time()
        if now - strip_at < 0.9:
            return
        strip_at = now
        if not LAST_TIMER_RECT:
            paint(snapshot=False)
        else:
            repaint_timer_strip(disp)

    def apply_chips(ev):
        if getattr(ev, "has_timers", False) and ev.timers is not None:
            TIMER_CHIPS[:] = [dict(t) for t in ev.timers]
            return True
        return False

    paint()
    threading.Thread(target=wx.loop, daemon=True).start()
    threading.Thread(target=status_loop, name="status", daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"listening on :{PORT}", flush=True)
    while True:
        t = time.time()
        minute = 60.2 - (t % 60)  # wake just after the minute boundary; no busy loop
        if thinking():
            wait = 1.0 / spin_fps()
        elif msg:
            wait = min(minute, max(0.05, until - t))
        elif E("GROK_STATION_BLINK", "0") != "0":
            delay = 1.0 - (t % 1.0)  # next whole second, for the colon
            if delay < 0.05:
                delay += 1.0
            wait = min(delay, minute)
        else:
            wait = minute
        if time.time() - setup["checked"] >= 2:
            setup["checked"] = time.time()
            sd = read_setup()
            if sd != setup["doc"]:
                setup["doc"], msg, anim_started, card_hold = sd, None, None, False
                paint(fade=True)
                print(f"setup screen: {sd['mode'] if sd else 'off'}", flush=True)
        if setup["doc"]:
            wait = min(wait, 2.0)
        if TIMER_CHIPS and not setup["doc"] and not (last_key and last_key[0] == "night"):
            delay = 1.0 - (t % 1.0)  # next whole second, so mm:ss and the ringing pulse step together
            if delay < 0.05:
                delay += 1.0
            wait = min(wait, delay)
        ev = pygame.event.wait(max(1, int(wait * 1000)))
        if setup["doc"] and ev.type in (STATE, SHOW):
            continue  # nothing else is shown until setup is done
        if ev.type == STATE:
            chips_changed = apply_chips(ev)
            if ev.state in VOICE_HOLDS:
                hold = REPLY_HOLD if ev.done else VOICE_HOLDS[ev.state]
                new = {"from": ev.who, "heard": ev.heard, "text": ev.text or VOICE_TEXT[ev.state], "state": ev.state, "voice": True}
                kept = merge_card(msg.get("card") if msg else None, getattr(ev, "has_card", False), getattr(ev, "card", None), bool(ev.done))
                if kept:
                    new["card"] = kept
                card_hold = False  # a new voice state restarts the hold; the card hold starts when it ends
                if anim_of(new):
                    if (not anim_started or not msg or msg.get("state") != ev.state
                            or (ev.state == "enroll" and msg.get("heard") != ev.heard)):
                        anim_started = time.time()  # each enrollment step restarts its fill/pulse
                else:
                    anim_started = None
                msg = new
                until = time.time() + hold
                paint()
            elif ev.state == "idle":
                # session end: one card hold while a card is up, then the idle clock
                if msg and msg.get("card") and not card_hold:
                    card_hold = True
                    until = time.time() + CARD_HOLD
                    paint()
                elif msg and msg.get("voice"):
                    until, anim_started = 0, None
                elif chips_changed and not (last_key and last_key[0] == "night"):
                    paint(snapshot=False)
            elif chips_changed or (getattr(ev, "has_card", False) and getattr(ev, "card", None) and msg):
                if getattr(ev, "card", None) and msg:
                    msg["card"] = ev.card
                    card_hold = False
                if msg or chips_changed:
                    if last_key and last_key[0] == "night" and not msg:
                        pass  # chips are stored and drawn the next time the idle clock is painted
                    else:
                        paint(snapshot=not msg)
            continue
        if ev.type == SHOW:
            msg, until, anim_started, card_hold = ev.msg, time.time() + HOLD, None, False
            paint()
            print("rendered", flush=True)
        elif ev.type == WEATHER:
            if not msg and not is_night(time.localtime()):
                paint(snapshot=False)
        elif ev.type == STATUS_EV:
            paint(snapshot=False)
        elif ev.type == pygame.QUIT:
            break
        elif ev.type == pygame.NOEVENT:
            if msg and time.time() >= until:
                if msg.get("card") and not card_hold:
                    card_hold = True
                    until = time.time() + CARD_HOLD
                else:
                    msg, anim_started, card_hold = None, None, False
                    paint(fade=True)
                    print("back to idle", flush=True)
            elif thinking():
                spin_portrait()  # no full-screen repaint, no snapshot
                maybe_strip()
            elif last_key is None or last_key[1] != time.localtime().tm_min:
                paint(snapshot=False)
            elif not msg and E("GROK_STATION_BLINK", "0") != "0":
                blink_colon()
                maybe_strip()
            else:
                maybe_strip()


def demo(out, size):
    """Render every screen to PNG with fixed sample data (no framebuffer, no network)."""
    global WX_CACHE
    pygame.display.init()
    pygame.font.init()
    os.makedirs(out, exist_ok=True)
    WX_CACHE = "/nonexistent"
    disp = Display(fb=None, size=size)
    now = time.strptime("2026-10-18 19:42", "%Y-%m-%d %H:%M")
    wx = Weather()
    wx.data, wx.ok_at = {"temp": 64, "code": 2, "day": False, "hi": 71, "lo": 52}, time.time()
    shots = [
        ("idle", draw_idle, (now, wx)),
        ("session", draw_session, (now, {"from": "Grok", "voice": True, "heard": "what's the weather like tomorrow",
                                          "text": "Tomorrow looks mild: partly sunny, a high of 71 and a low of 52, "
                                                  "with a light breeze in the afternoon. No rain expected."})),
        ("session-monogram", draw_session, (now, {"from": "Rhea", "heard": "", "text": "Done. The lights are set to warm white."})),
        ("listening", draw_session, (now, {"from": "Grok", "voice": True, "heard": "", "text": "Go ahead, I’m listening.", "state": "listening"})),
        ("listening-persona", draw_session, (now, {"from": "Kai", "voice": True, "heard": "", "text": "Go ahead, I’m listening.", "state": "listening"})),
        ("thinking", draw_session, (now, {"from": "Grok", "voice": True, "heard": "What is the capital of France?", "text": "Thinking…", "state": "thinking"})),
        ("error", draw_session, (now, {"from": "Grok", "voice": True, "heard": "", "text": "Can’t reach Grok right now.", "state": "error"})),
        ("text", draw_session, (now, {"from": "", "heard": "", "text": "Dinner in 10 minutes"})),
        ("night", draw_night, (time.strptime("2026-10-18 23:07", "%Y-%m-%d %H:%M"),)),
    ]
    for code, name in ((0, "clear"), (3, "cloud"), (63, "rain"), (73, "snow"), (95, "storm"), (45, "fog")):
        w2 = Weather()
        w2.data, w2.ok_at = {"temp": 58, "code": code, "day": True, "hi": 66, "lo": 49}, time.time()
        shots.append((f"idle-{name}", draw_idle, (now, w2)))
    off = Weather()
    off.ok_at = off.started = time.time() - 7200
    shots.append(("offline", draw_idle, (now, off)))
    think = {"from": "Grok", "voice": True, "heard": "What is the capital of France?", "text": "Thinking…", "state": "thinking"}
    for i, phase in enumerate((0.0, 0.33, 0.66)):
        shots.append((f"thinking-spin-{i}", draw_session, (now, think, "thinking", phase)))
    listen = {"from": "Grok", "voice": True, "heard": "", "text": "Go ahead, I’m listening.", "state": "listening"}
    for i, phase in enumerate((0.0, 0.25, 0.5)):
        shots.append((f"listening-pulse-{i}", draw_session, (now, listen, "listening", phase)))
    for k, (stage, i, extra, t) in enumerate((("prompt", 1, "", 1), ("ready", 3, "", 0.6), ("recording", 3, "", 0.5),
                                               ("loud", 3, "", 1), ("quiet", 3, "", 1), ("saved", 3, "", 0.5),
                                               ("uploaded", 10, "", 1), ("queued", 0, "2", 0.3), ("passed", 0, "", 1),
                                               ("failed", 0, "10", 1)), 1):
        em = {"from": "Finn", "voice": True, "state": "enroll", "text": "“Hey Finn”", "heard": f"{i}/10|{stage}|{extra}"}
        shots.append((f"enroll-{k:02d}-{stage}", draw_session, (now, em, "enroll", t)))
    shots.append(("enroll-no-persona", draw_session, (now, {"from": "Grok", "voice": True, "state": "enroll",
                                                             "text": "“Hey Nova”", "heard": "1/10|prompt|"}, "enroll", 1)))
    sd = {"mode": "setup", "code": "K7P4QX", "host": "grok-station.local", "url": "https://grok-station.local"}
    shots.append(("setup-needed", draw_setup, (now, sd)))
    shots.append(("setup-reset", draw_setup, (now, dict(sd, mode="reset", code="R4M9TW"))))
    demo_qr = [("".join("1" if ((r * 7 + c * 3) % 5 < 2 or (r < 7 and c < 7)) else "0" for c in range(29))) for r in range(29)]
    shots.append(("setup-hotspot", draw_hotspot, (now, dict(sd, mode="hotspot", ssid="grok-station-7F3A", password="hearth-4821",
                                                            url="https://10.42.0.1", qr_join=demo_qr, qr_open=demo_qr))))
    shots.append(("t1-confirm", draw_session, (now, {"from": "Finn", "heard": "add|grocery|0", "text": "oat milk and two lemons", "state": "confirm"})))
    for name, painter, args in shots:
        disp.screen.fill(BG)
        painter(disp.screen, disp.L, *args)
        pygame.image.save(disp.full, os.path.join(out, f"{name}.png"))
        print(os.path.join(out, f"{name}.png"))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--demo":
        size = tuple(map(int, (sys.argv[3] if len(sys.argv) > 3 else "2560x1706").split("x")))
        demo(sys.argv[2], size)
    else:
        main()
