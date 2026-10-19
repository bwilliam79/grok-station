#!/usr/bin/env python3
"""grok-station: LAN HTTP listener + fullscreen framebuffer UI for a Raspberry Pi display.

POST /show {"text": "...", "from": "Grok", "heard": "..."} with "Authorization: Bearer $GROK_STATION_TOKEN".
Screens: idle (clock, date, weather), message/session, night clock, weather offline.
Renders with pygame off-screen into a 16bpp surface written straight to /dev/fb0.
`grok_station.py --demo OUTDIR [WxH]` renders every screen to PNGs (no fb, no network).
"""
import hmac, ipaddress, json, math, os, re, sys, threading, time, urllib.request
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
LAT, LON = E("GROK_STATION_LAT", "36.19"), E("GROK_STATION_LON", "-94.13")
UNITS = E("GROK_STATION_UNITS", "fahrenheit")         # or celsius
WX_CACHE = E("GROK_STATION_WEATHER_CACHE", "/var/cache/grok-station/weather.json")
WX_EVERY, WX_RETRY, WX_STALE = 900, 300, 3600         # refresh, retry after failure, offline after
FADE = E("GROK_STATION_FADE", "1") != "0"
MAX_TEXT, MAX_FROM, MAX_HEARD = 500, 40, 300
SHOW, WEATHER = pygame.USEREVENT + 1, pygame.USEREVENT + 2


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
        pygame.event.post(pygame.event.Event(SHOW, msg={"text": text, "from": who, "heard": heard}))
        print(f"show from {self.client_address[0]}: {len(text)} chars{' speaker=' + who if who else ''}", flush=True)
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
    slug = re.sub(r"[^a-z0-9_-]", "", name.lower().replace(" ", "-"))
    for d in PORTRAIT_DIRS:
        p = os.path.join(d, slug + ".png")
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


# Future voice states (listening ticks, thinking arc, handoff sweep) register a painter here; nothing else changes.
RINGS = {"speaking": ring_speaking, "idle": ring_idle}


def portrait(name, d, ring_w, state="speaking", t=1.0):
    """Circle-cropped photo (or monogram) with the state ring, supersampled. Returns (surface, offset)."""
    k = 2
    glow = int(ring_w * 2.2)
    pad = int(ring_w + glow + 4)
    size = (d + 2 * pad) * k
    c = pygame.Surface((size, size), pygame.SRCALPHA)
    cx = cy = size // 2
    r = d * k // 2
    path = find_portrait(name)
    photo = pygame.Surface((2 * r, 2 * r), pygame.SRCALPHA)
    if path:
        img = pygame.image.load(path)
        iw, ih = img.get_size()
        sc = 2 * r / min(iw, ih)
        img = pygame.transform.smoothscale(img, (max(2 * r, int(iw * sc)), max(2 * r, int(ih * sc))))
        photo.blit(img, ((2 * r - img.get_width()) // 2, int((2 * r - img.get_height()) * 0.3)))
    else:
        photo.fill(SURFACE2)
        mono = font("display", d * k * 0.42).render((name[:1] or "?").upper(), True, TEXT)
        photo.blit(mono, mono.get_rect(center=(r, r)))
    mask = pygame.Surface((2 * r, 2 * r), pygame.SRCALPHA)
    pygame.draw.circle(mask, (255, 255, 255, 255), (r, r), r)
    photo.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
    c.blit(photo, (cx - r, cy - r))
    RINGS.get(state, ring_speaking)(c, cx, cy, r + int(ring_w * k * 0.6), ring_w * k, glow * k, t)
    return pygame.transform.smoothscale(c, (size // k, size // k)), pad


# ---------------------------------------------------------------- screens
def draw_idle(surf, L, now, wx):
    surf.fill(BG)
    hhmm, ampm = clock_parts(now)
    top = L(70) + int(L.extra_y * 0.3)
    clock_px = L(320)
    base = top + int(font("display", clock_px).get_ascent() * 0.98)
    r = baseline_text(surf, "display", clock_px, hhmm, TEXT, L.m - L(8), base)
    baseline_text(surf, "display", L(84), ampm, MUTED, r.right + L(18), base)
    text_at(surf, "body", L(60), time.strftime("%A, %B %-d", now), TEXT, (L.m, base + L(40)))
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


def draw_night(surf, L, now):
    surf.fill(BLACK)
    hhmm, ampm = clock_parts(now)
    f, fm = font("night", L(340)), font("night", L(90))
    wc, wm = f.size(hhmm)[0], fm.size(ampm)[0]
    shift = (now.tm_min // 10) % 4  # burn-in: drift up to 16px (at 1080p) every 10 minutes
    dx, dy = L((-8, 8, 8, -8)[shift]), L((-8, -8, 8, 8)[shift])
    x = (L.w - (wc + L(20) + wm)) // 2 + dx
    base = L.h // 2 + int(f.get_ascent() * 0.36) + dy
    baseline_text(surf, "night", L(340), hhmm, NIGHT_TEXT, x, base)
    baseline_text(surf, "night", L(90), ampm, NIGHT_DIM, x + wc + L(20), base)


def draw_topbar_clock(surf, L, now):
    hhmm, ampm = clock_parts(now)
    y = L(34) + L(72) // 2
    r = text_at(surf, "display", L(56), f"{hhmm} {ampm}", TEXT, (L.w - L.m, y), "midright")
    return r


def draw_session(surf, L, now, msg, state="speaking", t=1.0):
    surf.fill(BG)
    draw_topbar_clock(surf, L, now)
    who, heard, text = msg.get("from", ""), msg.get("heard", ""), msg["text"]
    top_row = L(34) + L(72)
    if not who:
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
    img, pad = portrait(who, d, ring_w, state, t)
    surf.blit(img, (left - pad, photo_top - pad))
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
    msg, until, last_key = None, 0.0, None

    def paint(fade=False, snapshot=True):
        nonlocal last_key
        now = time.localtime()
        if msg:
            disp.show(draw_session, now, msg, fade=fade, snapshot=snapshot)
            key = ("session", now.tm_min)
        elif is_night(now):
            disp.show(draw_night, now, fade=fade, snapshot=snapshot)
            key = ("night", now.tm_min)
        else:
            disp.show(draw_idle, now, wx, fade=fade, snapshot=snapshot)
            key = ("idle", now.tm_min, wx.offline(), json.dumps(wx.data))
        last_key = key

    paint()
    threading.Thread(target=wx.loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"listening on :{PORT}", flush=True)
    while True:
        t = time.time()
        wait = 60.2 - (t % 60)  # wake just after the minute boundary; no busy loop
        if msg:
            wait = min(wait, max(0.05, until - t))
        ev = pygame.event.wait(int(wait * 1000))
        if ev.type == SHOW:
            msg, until = ev.msg, time.time() + HOLD
            paint()
            print("rendered", flush=True)
        elif ev.type == WEATHER:
            if not msg and not is_night(time.localtime()):
                paint(snapshot=False)
        elif ev.type == pygame.QUIT:
            break
        elif ev.type == pygame.NOEVENT:
            if msg and time.time() >= until:
                msg = None
                paint(fade=True)
                print("back to idle", flush=True)
            elif last_key is None or last_key[1] != time.localtime().tm_min:
                paint(snapshot=False)


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
        ("session", draw_session, (now, {"from": "Grok", "heard": "what's the weather like tomorrow",
                                          "text": "Tomorrow looks mild: partly sunny, a high of 71 and a low of 52, "
                                                  "with a light breeze in the afternoon. No rain expected."})),
        ("session-monogram", draw_session, (now, {"from": "Rhea", "heard": "", "text": "Done. The lights are set to warm white."})),
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
