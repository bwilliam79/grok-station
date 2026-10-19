#!/usr/bin/env python3
"""grok-station: LAN HTTP listener + fullscreen framebuffer UI for a Raspberry Pi display.

POST /show {"text": "...", "from": "Grok", "heard": "..."} with "Authorization: Bearer $GROK_STATION_TOKEN".
Screens: idle (clock, date, weather), message/session, night clock, weather offline.
Renders with pygame off-screen into a 16bpp surface written straight to /dev/fb0.
`grok_station.py --demo OUTDIR [WxH]` renders every screen to PNGs (no fb, no network).
The thinking ring spins and the clock colon blinks; both repaint only their own rectangle.
"""
import hmac, ipaddress, json, math, os, re, sys, threading, time, unicodedata, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
HOLD = float(E("GROK_STATION_HOLD", "60"))            # seconds a message stays up after the last one
NIGHT = E("GROK_STATION_NIGHT", "22-6")               # night clock hours, start-end (24h), "" disables
LAT, LON = E("GROK_STATION_LAT", ""), E("GROK_STATION_LON", "")  # no default: weather is off until both are set
UNITS = E("GROK_STATION_UNITS", "fahrenheit")         # or celsius
WX_CACHE = E("GROK_STATION_WEATHER_CACHE", "/var/cache/grok-station/weather.json")
WX_EVERY, WX_RETRY, WX_STALE = 900, 300, 3600         # refresh, retry after failure, offline after
FADE = E("GROK_STATION_FADE", "1") != "0"
MAX_TEXT, MAX_FROM, MAX_HEARD = 500, 40, 300
SHOW, WEATHER, STATE = pygame.USEREVENT + 1, pygame.USEREVENT + 2, pygame.USEREVENT + 3
LISTEN_HOLD = float(E("GROK_STATION_LISTEN_HOLD", "12"))  # seconds the listening screen stays up
REPLY_HOLD = float(E("GROK_STATION_REPLY_HOLD", "15"))   # seconds a spoken reply stays up after playback
VOICE_HOLDS = {"listening": LISTEN_HOLD, "thinking": 20, "speaking": 45, "error": 5, "notice": 4}  # safety timeouts per state
VOICE_TEXT = {"listening": "Go ahead, I’m listening.", "thinking": "Thinking…", "speaking": "…", "error": "Can’t reach Grok right now.", "notice": "Didn’t catch that."}
VOICE_NAME = E("GROK_STATION_VOICE_NAME", "Grok")        # portrait/name shown while listening
SPIN_PERIOD = 1.2                                         # seconds for one thinking-ring revolution
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
SUN = hexc("F2C46A")
NIGHT_TEXT, NIGHT_DIM, BLACK = hexc("9A7752"), hexc("5E4934"), hexc("000000")

_fonts = {}


def font(kind, px):
    files = {"display": "InterTight-SemiBold.ttf", "night": "InterTight-Medium.ttf",
             "body": "Inter-Medium.ttf", "italic": "Inter-MediumItalic.ttf"}
    key = (kind, max(8, int(px)))
    if key not in _fonts:
        if len(_fonts) > 40:
            _fonts.clear()
        path = os.path.join(ASSETS, "fonts", files[kind])
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
        self._reply(200, {"ok": True}) if self.path == "/health" else self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/state":
            return self._state()
        if not ipaddress.ip_address(self.client_address[0].removeprefix("::ffff:")).is_private:
            return self._reply(403, {"error": "lan only"})
        if self.path != "/show":
            return self._reply(404, {"error": "not found"})
        auth = self.headers.get("Authorization", "")
        if not TOKEN or not hmac.compare_digest(auth.encode(), f"Bearer {TOKEN}".encode()):
            return self._reply(401, {"error": "unauthorized"})
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

    def _state(self):
        """Localhost-only, no token: the wake listener on the same Pi drives voice states."""
        if self.client_address[0].removeprefix("::ffff:") not in ("127.0.0.1", "::1"):
            return self._reply(403, {"error": "localhost only"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 8192:
                return self._reply(413, {"error": "too large"})
            j = json.loads(self.rfile.read(n) or b"{}")
            state = j.get("state")
            heard = " ".join(str(j.get("heard") or "").split())[:MAX_HEARD]
            text = str(j.get("text") or "").strip()[:MAX_TEXT]
            who = persona_names().get(name_key(j.get("who") or VOICE_NAME), (VOICE_NAME, "grok"))[0]
        except Exception:
            return self._reply(400, {"error": "bad json"})
        if state not in ("idle", *VOICE_HOLDS):
            return self._reply(400, {"error": "state must be idle, " + ", ".join(VOICE_HOLDS)})
        pygame.event.post(pygame.event.Event(STATE, state=state, heard=heard, text=text, done=bool(j.get("done")), who=who))
        print(f"state {state} who={who}", flush=True)
        self._reply(200, {"ok": True})

    def log_message(self, *a):
        pass


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
    """Colon lit on even unix seconds. GROK_STATION_BLINK=0 keeps it on."""
    if E("GROK_STATION_BLINK", "1") == "0":
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
    """Portrait for a voice persona (Grok or a registry name) only; anything else gets the monogram."""
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


def ring_speaking(c, cx, cy, r, ring_w, glow, t):
    """Warm ring with a soft static outer glow. t is a 0..1 phase/amplitude for future animation."""
    for i in range(glow, 0, -1):
        a = int(70 * (1 - i / glow) ** 2)
        pygame.draw.circle(c, (*SPEAKING[:3], a), (cx, cy), r + ring_w / 2 + i, 2 * SS)
    pygame.draw.circle(c, SPEAKING, (cx, cy), r + ring_w / 2, int(ring_w))


def ring_idle(c, cx, cy, r, ring_w, glow, t):
    pygame.draw.circle(c, RULE, (cx, cy), r + ring_w / 2, int(ring_w / 3))


def ring_listening(c, cx, cy, r, ring_w, glow, t):
    """Teal tick ring (Noor S2): thin base ring plus 72 radial ticks, every 6th one longer. Static for now."""
    pygame.draw.circle(c, (*LISTENING[:3], 90), (cx, cy), r, max(2, int(ring_w / 4)))
    r0 = r + ring_w * 0.7
    for i in range(72):
        a = 2 * math.pi * i / 72 - math.pi / 2
        ln = ring_w * (2.2 if i % 6 == 0 else 1.4) * (0.75 + 0.25 * t)
        p0 = (cx + r0 * math.cos(a), cy + r0 * math.sin(a))
        p1 = (cx + (r0 + ln) * math.cos(a), cy + (r0 + ln) * math.sin(a))
        pygame.draw.line(c, LISTENING, p0, p1, max(2, int(ring_w / 2.4)))


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


# Voice states register a painter here; "error" and "notice" reuse the quiet idle ring.
RINGS = {"speaking": ring_speaking, "idle": ring_idle, "listening": ring_listening, "thinking": ring_thinking, "error": ring_idle, "notice": ring_idle}

_photo_cache = {}  # (path, mtime, d, k, initial, photo_ok) -> circle-cropped surface
LAST_PORTRAIT_RECT = LAST_PORTRAIT_ARGS = None  # last session portrait blit, so the ring can spin in place
LAST_COLON_RECT = LAST_COLON_GLYPH = None       # last clock colon, so it can blink without a full repaint
LAST_DIGITS_RECT = None


def _circle_photo(name, d, k, photo_ok):
    """Circle-cropped photo or monogram. image.load + smoothscale run once per key (at most 8 kept)."""
    path = find_portrait(name) if photo_ok else None  # /show messages never get a photo, only the monogram
    try:
        mt = os.stat(path).st_mtime if path else None
    except OSError:
        path, mt = None, None
    initial = (name[:1] or "?").upper()
    key = (path, mt, d, k, initial, bool(photo_ok))
    hit = _photo_cache.get(key)
    if hit is not None:
        return hit
    r = d * k // 2
    photo = pygame.Surface((2 * r, 2 * r), pygame.SRCALPHA)
    if path:
        img = pygame.image.load(path)
        iw, ih = img.get_size()
        sc = 2 * r / min(iw, ih)
        img = pygame.transform.smoothscale(img, (max(2 * r, int(iw * sc)), max(2 * r, int(ih * sc))))
        photo.blit(img, ((2 * r - img.get_width()) // 2, int((2 * r - img.get_height()) * 0.3)))
    else:
        photo.fill(SURFACE2)
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
    """Circle-cropped photo (or monogram) with the state ring, supersampled. Returns (surface, offset)."""
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


def draw_idle(surf, L, now, wx, colon_on=True):
    surf.fill(BG)
    hhmm, ampm = clock_parts(now)
    top = L(70) + int(L.extra_y * 0.3)
    clock_px = L(320)
    base = top + int(font("display", clock_px).get_ascent() * 0.98)
    x = L.m - L(8)
    digits, _colon = draw_clock(surf, "display", clock_px, hhmm, TEXT, x, base, colon_on)
    baseline_text(surf, "display", L(84), ampm, MUTED, x + font("display", clock_px).size(hhmm)[0] + L(18), base)
    date = time.strftime("%A, %B %-d", now)
    dimg = font("body", L(60)).render(date, True, TEXT)
    dbr = dimg.get_bounding_rect()
    bx = int(round(digits.x + digits.w / 2.0 - (dbr.x + dbr.w / 2.0)))
    surf.blit(dimg, (bx, base + L(40)))
    if wx.offline():
        draw_offline_badge(surf, L, base)
    elif wx.data:
        draw_weather(surf, L, wx.data, base)
    draw_next_up(surf, L, now)


def draw_weather(surf, L, d, clock_base):
    right = L.w - L.m
    label, kind = WMO.get(d["code"], ("", "cloud"))
    temp_px = L(180)
    tb = clock_base - L(80)  # temp baseline sits a little above the clock's
    r = baseline_text(surf, "display", temp_px, f"{d['temp']}°", TEXT, right, tb, align="right")
    icon_px = L(150)
    icon = weather_icon(kind, d["day"], icon_px, max(2, L(7)))
    surf.blit(icon, (r.left - icon_px - L(12), tb - L(140)))
    y = tb + L(36)
    if label:
        y = text_at(surf, "body", L(56), label, TEXT, (right, y), "topright").bottom + L(4)
    text_at(surf, "body", L(48), f"High {d['hi']}°  ·  Low {d['lo']}°", MUTED, (right, y), "topright")


def draw_offline_badge(surf, L, clock_base):
    right = L.w - L.m
    well = L(150)
    rect = pygame.Rect(right - well, clock_base - L(250), well, well)
    pygame.draw.rect(surf, SURFACE2, rect, border_radius=L(28))
    ic = offline_icon(L(104), max(2, L(7)))
    surf.blit(ic, ic.get_rect(center=rect.center))
    text_at(surf, "body", L(44), "Weather offline", MUTED, (right, rect.bottom + L(20)), "topright")


def draw_next_up(surf, L, now):
    """Hook: the lower third is reserved for a rotating "Next up" card once a data source exists."""
    return


def draw_night(surf, L, now, colon_on=True):
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


def draw_topbar_clock(surf, L, now):
    hhmm, ampm = clock_parts(now)
    y = L(34) + L(72) // 2
    r = text_at(surf, "display", L(56), f"{hhmm} {ampm}", TEXT, (L.w - L.m, y), "midright")
    return r


def draw_session(surf, L, now, msg, state="speaking", t=1.0):
    global LAST_PORTRAIT_RECT, LAST_PORTRAIT_ARGS
    state = msg.get("state", state)
    surf.fill(BG)
    if state == "listening":
        pygame.draw.rect(surf, LISTENING, (0, 0, L.w, max(3, L(8))))  # "mic open" bar along the top
    draw_topbar_clock(surf, L, now)
    who, heard, text = msg.get("from", ""), msg.get("heard", ""), msg["text"]
    top_row = L(34) + L(72)
    if not who:
        LAST_PORTRAIT_RECT = LAST_PORTRAIT_ARGS = None
        # plain {text}: big centered message, heard (if any) above it
        width = L.w - 2 * L.m
        y0, y1 = top_row + L(60), L.h - L(110)
        if heard:
            fh, lh, sh = fit_text("italic", f"“{heard}”", width, L(140), L(48), L(40), max_lines=2)
            draw_lines(surf, fh, lh, sh, MUTED, L.m, y0, "center", width)
            y0 += sh * len(lh) + L(40)
        f, lines, step = fit_text("display", text, width, y1 - y0, L(180), L(48), lh=1.0)
        y = y0 + max(0, (y1 - y0 - step * len(lines)) // 2)
        draw_lines(surf, f, lines, step, TEXT, L.m, y, "center", width)
        return
    d, ring_w = L(500), max(4, L(14))
    left = L(160)
    x = left + d + L(140)
    width = L.w - L.m - x
    bottom = L.h - L(96)
    photo_top = top_row + max(L(60), (L.h - top_row - d) // 2 - L(20))
    img, pad = portrait(who, d, ring_w, state, t, photo_ok=bool(msg.get("voice")))
    origin = (left - pad, photo_top - pad)
    surf.blit(img, origin)
    LAST_PORTRAIT_RECT = pygame.Rect(origin, img.get_size())
    LAST_PORTRAIT_ARGS = (who, d, ring_w, bool(msg.get("voice")))
    if state == "listening":
        lr = text_at(surf, "display", L(64), "Listening", LISTENING, (left + d // 2 + L(40), photo_top + d + L(150)), "center")
        for i, hh in enumerate((0.45, 0.8, 1.0, 0.65, 0.35)):  # small static waveform glyph
            bh = int(L(56) * hh)
            pygame.draw.rect(surf, LISTENING, (lr.left - L(110) + i * L(18), lr.centery - bh // 2, L(9), bh), border_radius=L(5))
        text_at(surf, "body", L(40), "Say “Hey Grok” to wake", MUTED, (L.m, L.h - L(70)), "midleft")
    elif state == "thinking":
        text_at(surf, "display", L(64), "Thinking", THINKING, (left + d // 2, photo_top + d + L(150)), "center")
    # measure the right column first so it can sit centered on the photo
    fn = font("display", L(120))
    name = who if fn.size(who)[0] <= width else ellipsize(fn, who, width)
    name_h = int(fn.get_ascent() * 0.98)
    heard_part = fit_text("italic", f"“{heard}”", width, L(130), L(48), L(40), lh=1.02, max_lines=2) if heard else None
    heard_h = heard_part[2] * len(heard_part[1]) + L(28) if heard else 0
    top_min = top_row + L(40)
    avail = bottom - top_min - name_h - L(20) - heard_h
    f, lines, step = fit_text("body", text, width, avail, L(76), L(40), lh=1.06)
    block = name_h + L(20) + heard_h + step * len(lines)
    y = max(top_min, min(photo_top + d // 2 - block // 2, bottom - block))
    baseline_text(surf, "display", L(120), name, TEXT, x - L(4), y + int(fn.get_ascent() * 0.78))
    y += name_h + L(20)
    if heard_part:
        y = draw_lines(surf, *heard_part, MUTED, x, y) + L(28)
    draw_lines(surf, f, lines, step, TEXT, x, y)


SCREENS = {"idle": draw_idle, "night": draw_night, "session": draw_session}


# ---------------------------------------------------------------- output
class Display:
    def __init__(self, fb=FB, size=None):
        self.fb = fb
        if size:
            self.screen = pygame.Surface(size, depth=16)
        else:
            sysfs = "/sys/class/graphics/" + os.path.basename(fb)
            w, h = map(int, open(f"{sysfs}/virtual_size").read().split(","))
            bpp = int(open(f"{sysfs}/bits_per_pixel").read())
            stride = int(open(f"{sysfs}/stride").read())
            self.screen = pygame.Surface((w, h), depth=bpp)
            if self.screen.get_pitch() != stride:
                raise SystemExit(f"fb stride {stride} != surface pitch {self.screen.get_pitch()}")
        self.L = Layout(*self.screen.get_size())

    def flush(self):
        if self.fb:
            with open(self.fb, "wb") as f:  # 16bpp RGB565 surface matches the vc4 fbdev layout
                f.write(self.screen.get_buffer().raw)

    def flush_rect(self, rect):
        """Write only rect's rows/columns. fb=None (demo, tests) is a no-op."""
        if not self.fb or not rect:
            return
        rect = pygame.Rect(rect).clip(self.screen.get_rect())
        if rect.w <= 0 or rect.h <= 0:
            return
        bpp, pitch = self.screen.get_bytesize(), self.screen.get_pitch()
        raw = self.screen.get_buffer().raw
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
                pygame.image.save(self.screen, SNAP)
            except Exception:
                pass


def main():
    pygame.display.init()  # dummy driver: needed for the event queue only
    pygame.font.init()
    disp = Display()
    wx = Weather()
    print(f"display {disp.screen.get_size()} fb={FB} assets={ASSETS}", flush=True)
    msg, until, last_key, thinking_started = None, 0.0, None, None

    def thinking():
        return bool(msg and msg.get("voice") and msg.get("state") == "thinking" and thinking_started)

    def paint(fade=False, snapshot=True):
        nonlocal last_key
        now = time.localtime()
        on = colon_blink_on()
        if thinking():
            phase = ((time.time() - thinking_started) / SPIN_PERIOD) % 1.0
            disp.show(draw_session, now, msg, "thinking", phase, fade=fade, snapshot=snapshot)
            key = ("session", now.tm_min)
        elif msg:
            disp.show(draw_session, now, msg, fade=fade, snapshot=snapshot)
            key = ("session", now.tm_min)
        elif is_night(now):
            disp.show(draw_night, now, on, fade=fade, snapshot=snapshot)
            key = ("night", now.tm_min)
        else:
            disp.show(draw_idle, now, wx, on, fade=fade, snapshot=snapshot)
            key = ("idle", now.tm_min, wx.offline(), json.dumps(wx.data))
        last_key = key

    spin_frames = {}  # (args, frame) -> opaque screen-format portrait; one revolution is rendered once, then reused

    def spin_portrait():
        """One thinking-ring frame: blit a cached frame into the portrait rect and flush only that rect."""
        rect, args = LAST_PORTRAIT_RECT, LAST_PORTRAIT_ARGS
        if not rect or not args or thinking_started is None:
            return
        n = max(1, round(SPIN_PERIOD * spin_fps()))
        i = int(((time.time() - thinking_started) / SPIN_PERIOD) % 1.0 * n) % n
        if any(k[0] != args for k in spin_frames):
            spin_frames.clear()
        frame = spin_frames.get((args, i))
        if frame is None:
            who, d, ring_w, photo_ok = args
            frame = pygame.Surface(rect.size, depth=disp.screen.get_bitsize())
            frame.fill(BG)
            frame.blit(portrait(who, d, ring_w, "thinking", i / n, photo_ok=photo_ok)[0], (0, 0))
            spin_frames[(args, i)] = frame
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

    paint()
    threading.Thread(target=wx.loop, daemon=True).start()
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
        elif E("GROK_STATION_BLINK", "1") != "0":
            delay = 1.0 - (t % 1.0)  # next whole second, for the colon
            if delay < 0.05:
                delay += 1.0
            wait = min(delay, minute)
        else:
            wait = minute
        ev = pygame.event.wait(max(1, int(wait * 1000)))
        if ev.type == STATE:
            if ev.state in VOICE_HOLDS:
                hold = REPLY_HOLD if ev.done else VOICE_HOLDS[ev.state]
                if ev.state == "thinking":
                    if not thinking_started:
                        thinking_started = time.time()
                else:
                    thinking_started = None
                msg = {"from": ev.who, "heard": ev.heard, "text": ev.text or VOICE_TEXT[ev.state], "state": ev.state, "voice": True}
                until = time.time() + hold
                paint()
            elif msg and msg.get("voice"):
                until, thinking_started = 0, None
            continue
        if ev.type == SHOW:
            msg, until, thinking_started = ev.msg, time.time() + HOLD, None
            paint()
            print("rendered", flush=True)
        elif ev.type == WEATHER:
            if not msg and not is_night(time.localtime()):
                paint(snapshot=False)
        elif ev.type == pygame.QUIT:
            break
        elif ev.type == pygame.NOEVENT:
            if msg and time.time() >= until:
                msg, thinking_started = None, None
                paint(fade=True)
                print("back to idle", flush=True)
            elif thinking():
                spin_portrait()  # no full-screen repaint, no snapshot
            elif last_key is None or last_key[1] != time.localtime().tm_min:
                paint(snapshot=False)
            elif not msg and E("GROK_STATION_BLINK", "1") != "0":
                blink_colon()


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
    for name, painter, args in shots:
        disp.screen.fill(BG)
        painter(disp.screen, disp.L, *args)
        pygame.image.save(disp.screen, os.path.join(out, f"{name}.png"))
        print(os.path.join(out, f"{name}.png"))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--demo":
        size = tuple(map(int, (sys.argv[3] if len(sys.argv) > 3 else "2560x1706").split("x")))
        demo(sys.argv[2], size)
    else:
        main()
