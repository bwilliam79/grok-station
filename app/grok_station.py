#!/usr/bin/env python3
"""grok-station: LAN HTTP listener + fullscreen framebuffer UI for a Raspberry Pi display.

POST /show {"text": "...", "from": "Grok", "heard": "..."} with "Authorization: Bearer $GROK_STATION_TOKEN".
A localhost caller with the same bearer may also put or clear a display card (docs/cards.md).
Screens: idle (clock, date, weather, home cards), message/session, night clock, weather offline,
recipe / sports / calendar cards.
A tool card (weather, time, unit conversion) sits in the session text column and holds 4 s after the
session ends. On the idle screen, timers are up to four countdown rings (overflow as chips); a ringing
timer takes the screen with an amber pulse. During a conversation they stay a bottom chip strip so the
reply stays visible. The strip or the rings repaint locally; the wake listener pushes the list only
when it changes. The countdown follows a monotonic anchor taken when the list arrives, so a wall-clock
step does not jump the digits.
Renders with pygame off-screen into a 16bpp surface written straight to /dev/fb0.
When Display dimming is on, brightness follows a light sensor, a clock schedule, or local sunrise.
Off leaves the backlight alone. A missing backlight file is a black overlay on a copy of the frame.
`grok_station.py --demo OUTDIR [WxH]` renders every screen to PNGs (no fb, no network).
The thinking ring spins by repainting only its own rectangle; the clock colon is steady (GROK_STATION_BLINK=1 blinks it).
Small status icons (network, Grok, mic, and an update arrow only while a newer version is cached) sit in the top-right corner. The Grok icon takes the old relay slot and covers relay health and the Grok service. A daemon thread polls them; --demo does not.
"""
import hashlib, hmac, ipaddress, json, math, os, re, socket, sys, threading, time, unicodedata, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from avatars import default_avatar_index
except ImportError:  # installed next to this file once the helper is shipped; the formula stays the same
    import hashlib

    def default_avatar_index(slug):
        return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % 18 + 1

import cards

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")  # no SDL display; we write /dev/fb0 directly
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame

try:
    import dimming
except ImportError:  # display still runs; dimming stays off
    dimming = None

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
FEED = pygame.USEREVENT + 5  # a display card was put or cleared; the deck already changed
LISTEN_HOLD = float(E("GROK_STATION_LISTEN_HOLD", "12"))  # seconds the listening screen stays up
REPLY_HOLD = float(E("GROK_STATION_REPLY_HOLD", "15"))   # seconds a spoken reply stays up after playback
def score_hold_s(env=None):
    """GROK_STATION_SCORE_HOLD: seconds a tile reply stays up (1-120, default 10). Admin Settings, Display page."""
    raw = str((os.environ if env is None else env).get("GROK_STATION_SCORE_HOLD", "10")).strip()
    return int(raw) if raw.isdigit() and 1 <= int(raw) <= 120 else 10


SCORE_HOLD = score_hold_s()
# 0.12.16: only a recipe takes the whole screen. Every other tile, and every bot state, uses the bottom zone
# under the clock, date and weather (docs/display.md "Bottom zone").
FULL_TILES = ("recipe",)
ZONE_TILES = ("sports", "calendar")
TILE_TYPES = FULL_TILES + ZONE_TILES
ZONE_STATES = ("listening", "thinking", "speaking", "error", "notice")  # voice states drawn in the bottom zone
STATE_WORDS = {"listening": "Listening", "thinking": "Thinking", "speaking": "Replying"}
# A spoken reply and its card arrive as two posts. Hold the reply text this long so a card
# from the same persona can take the screen without a flash of text. Audio is not delayed.
TEXT_CARD_WAIT = 1.5
# A full-screen tile a bot posted during a conversation (items 5 and 9): shown at once, alone, while the bot speaks.
TILE = {}


BADGE_STATES = ("listening", "thinking", "speaking", "error")


def tile_step(tile, prev_state, state, now, hold_s):
    """0.12.6: what a new voice state does to a tile that is up during a session. Returns "keep" or "clear".

    A new user turn (listening) or the tile's hold running out marks the tile stale; a stale tile is never
    pulled mid-listen, it clears when the bot starts its response (speaking). A tile the response
    posts replaces the old one (the FEED handler stores it fresh), so it shows."""
    if not tile:
        return "keep"
    # One state after a recipe step command: reading the step must not drop the card.
    if tile.pop("step_cmd", None):
        tile.pop("stale", None)
        return "keep"
    if state == "listening" and prev_state is not None:
        tile["stale"] = True
    if now >= float(tile.get("shown", now)) + float(hold_s):
        tile["stale"] = True
    if state == "speaking" and prev_state != "speaking" and tile.get("stale"):
        return "clear"
    return "keep"


class ChipTray:
    """0.12.16: tiles that arrive while a recipe is up, shown as top-bar chips (docs/display.md "Recipe chips").

    At most MAX chips, oldest first. A third replaces the oldest. Each chip ends on its tile's own TTL
    ("until", epoch seconds). The HTTP thread (voice "dismiss") and the main loop share it, so it locks."""

    MAX = 2

    def __init__(self):
        self._lock = threading.Lock()
        self._chips = []

    def _prune(self, now):
        before = len(self._chips)
        self._chips = [c for c in self._chips if c["until"] > now]
        return len(self._chips) != before

    def add(self, tile, now):
        """Store a tile {"bot", "id", "card", "expires"} as a chip. Returns the chip it replaced, or None."""
        until = float(tile.get("expires") or now + SCORE_HOLD)
        chip = {"bot": tile.get("bot"), "id": tile.get("id"), "card": tile.get("card"), "until": until, "at": now}
        with self._lock:
            self._prune(now)
            self._chips = [c for c in self._chips if (c["bot"], c["id"]) != (chip["bot"], chip["id"])]
            dropped = self._chips.pop(0) if len(self._chips) >= self.MAX else None
            self._chips.append(chip)
            return dropped

    def visible(self, now):
        with self._lock:
            self._prune(now)
            return [dict(c) for c in self._chips]

    def prune(self, now):
        """Drop expired chips. True when one went, so the screen repaints."""
        with self._lock:
            return self._prune(now)

    def dismiss(self, now):
        """Voice "dismiss" / "clear that", or a tap: the newest chip (nearest the clock) goes. Returns it, or None."""
        with self._lock:
            self._prune(now)
            return self._chips.pop() if self._chips else None

    def handoff(self, now):
        """The recipe ended: the newest chip with TTL left moves to the bottom zone. The tray empties."""
        with self._lock:
            self._prune(now)
            chip = self._chips[-1] if self._chips else None
            self._chips = []
            return chip

    def next_change(self, now):
        with self._lock:
            return min((c["until"] - now for c in self._chips), default=None)

    def __bool__(self):
        with self._lock:
            return bool(self._chips)


CHIPS = ChipTray()


def reduce_motion(env=None):
    """GROK_STATION_REDUCE_MOTION=1: the reply window pages instead of scrolling. There is no admin switch yet."""
    return str((os.environ if env is None else env).get("GROK_STATION_REDUCE_MOTION", "0")).strip() in ("1", "true", "on")


class Teleprompter:
    """0.12.16: the 4-line reply window's motion (docs/display.md "Reply teleprompter"). Pure: every call takes now.

    Geometry comes from the painter (set_geometry). Progress is spoken audio time / total audio time, posted by the
    wake listener at 5 Hz or more ("speech" on POST /state). With no progress the window waits HOLD_S, then
    drifts at FALLBACK_PX (spec units, scaled by the layout) per second. The shown offset eases toward the target
    with a TAU_S time constant, so timing jitter never jerks. Reduced motion pages a whole window at a time with
    a FADE_S cross-fade."""

    WINDOW_LINES = 4
    HOLD_S = 1.5
    FALLBACK_PX = 45   # 60 px/s at 2560x1706 (s = 1.333)
    TAU_S = 0.25
    FADE_S = 0.2

    def __init__(self, now, reduced=False):
        self.started = float(now)
        self.reduced = bool(reduced)
        self.progress = None   # 0..1 once the wake listener reports it
        self.stopped = False   # barge-in or a tile: frozen in place
        self.settled = False   # speech ended: last window, no fades
        self.offset = 0.0
        self.line_h, self.text_h, self.scale = 1, 1, 1.0
        self._t = float(now)
        self._page, self._page_at, self._prev_page = 0, None, 0

    @property
    def win_h(self):
        return self.WINDOW_LINES * self.line_h

    @property
    def max_offset(self):
        return max(0, self.text_h - self.win_h)

    @property
    def scrolls(self):
        return self.text_h > self.win_h

    def set_geometry(self, n_lines, line_h, scale):
        self.line_h, self.text_h, self.scale = max(1, int(line_h)), max(1, int(n_lines) * max(1, int(line_h))), float(scale)
        self.offset = min(self.offset, self.max_offset)

    def feed(self, progress, now):
        if self.stopped or self.settled:
            return
        try:
            p = float(progress)
        except (TypeError, ValueError):
            return
        if p == p:  # not NaN
            self.progress = min(1.0, max(0.0, p))

    def stop(self, now):
        self.step(now)
        self.stopped = True

    def settle(self, now):
        if not self.stopped:
            self.settled = True
            self.offset = float(self.max_offset)
            self._page = self._prev_page = self._last_page()

    def _last_page(self):
        return max(0, -(-self.text_h // self.win_h) - 1) if self.scrolls else 0

    def target(self, now):
        if not self.scrolls:
            return 0.0
        if self.settled:
            return float(self.max_offset)
        return float(min(self.max_offset, max(0.0, self._spoken(now) - 1.5 * self.line_h)))

    def _spoken(self, now):
        """Pixels into the text of the line being spoken: progress, or the fallback pace after the hold."""
        if self.progress is not None:
            return self.progress * self.text_h
        drift = (now - self.started - self.HOLD_S) * self.FALLBACK_PX * self.scale
        return 1.5 * self.line_h + drift if drift > 0 else 0.0

    def step(self, now):
        """Advance to now. Returns the offset to draw (pixels into the text)."""
        dt = max(0.0, now - self._t)
        self._t = now
        if self.stopped or not self.scrolls:
            return self.offset if self.scrolls else 0.0
        if self.settled:
            self.offset = float(self.max_offset)
            return self.offset
        if self.reduced:
            page = min(self._last_page(), int(self._spoken(now) // self.win_h))
            if page != self._page:
                self._prev_page, self._page, self._page_at = self._page, page, now
            self.offset = float(min(self.max_offset, self._page * self.win_h))
            return self.offset
        goal = self.target(now)
        self.offset += (goal - self.offset) * (1.0 - math.exp(-dt / self.TAU_S)) if dt else 0.0
        return self.offset

    def crossfade(self, now):
        """Reduced motion only: (previous offset, alpha 0..1 of the new page) while a page change fades, else None."""
        if not self.reduced or self._page_at is None:
            return None
        a = (now - self._page_at) / self.FADE_S
        if a >= 1.0:
            self._page_at = None
            return None
        return float(min(self.max_offset, self._prev_page * self.win_h)), max(0.0, a)

    def fades(self):
        """(top, bottom) edge fades on? Off when the text fits, and both off once speech has ended."""
        if not self.scrolls or self.settled:
            return False, False
        return self.offset > 0.5, self.offset < self.max_offset - 0.5

    def moving(self, now):
        """True while the window still has to repaint on its own (scroll, ease, or a page fade)."""
        if not self.scrolls or self.stopped or self.settled:
            return False
        if self.reduced:
            return self._page_at is not None or self.progress is None or self._page < self._last_page()
        return abs(self.target(now) - self.offset) > 0.5 or self.target(now) < self.max_offset


def recipe_up(tile_card, current):
    """True while a recipe is the full-screen tile: the session's tile, or the deck's current card (a recipe in the
    deck is always current). A sports or calendar tile that arrives then becomes a chip."""
    if isinstance(tile_card, dict) and tile_card.get("type") == "recipe":
        return True
    return isinstance(current, dict) and (current.get("card") or {}).get("type") == "recipe"


def tele_next(tele, prev_state, state, done, now, reduced=False):
    """The reply window across a voice state change. A new reply starts a window; the finished-playback post
    (speaking, done) settles it on its last lines; any other state (barge-in back to listening, thinking,
    error) stops it in place and drops it."""
    if state == "speaking":
        if done:
            if tele is not None:
                tele.settle(now)
            return tele
        if tele is None or tele.stopped or tele.settled or prev_state != "speaking":
            return Teleprompter(now, reduced)
        return tele
    if tele is not None:
        tele.stop(now)
    return None


def zone_voice(msg):
    """A voice session message the bottom zone draws (listening, thinking, replying, error, notice).

    Plain /show text, enrollment, the T1 read-back, and a reply that carries an inline info card keep the
    full-screen session layout."""
    if not isinstance(msg, dict) or not msg.get("voice") or msg.get("card"):
        return False
    return (msg.get("state") or "speaking") in ZONE_STATES


def turn_over(msg):
    """The session is idle: the same screen, but no listening/thinking/speaking state for the corner badge."""
    if not isinstance(msg, dict):
        return msg
    out = dict(msg)
    out.pop("state", None)
    return out


def tile_end(shown_at, hold_s, speech_end):
    """A tile reply goes back to the main display when speech ends or hold_s after it showed, whichever is later."""
    return max(float(speech_end), float(shown_at) + float(hold_s))


def reply_visual(now, hold_until, card, had_prior):
    """"card", "text", or "hold" when a reply and a card can arrive apart.

    A card already in hand wins, so the reply text is never flashed.
    With no previous screen, or once the wait has elapsed, the text shows.
    Otherwise hold: the caller keeps the previous picture and stops the thinking
    ring. This does not delay spoken audio; that plays on its own path.
    """
    if card:
        return "card"
    if not had_prior:
        return "text"
    try:
        deadline = float(hold_until)
    except (TypeError, ValueError):
        return "text"
    if now >= deadline:
        return "text"
    return "hold"


def persona_match(who, bot):
    """True when a display name and a card slug are the same persona."""
    wk, bk = name_key(who or ""), name_key(bot or "")
    if not wk or not bk:
        return False
    names = persona_names()
    left, right = names.get(wk), names.get(bk)
    if left and right:
        return left[1] == right[1]
    return wk == bk


CARD_HOLD = 4  # a finished session that showed a card stays up this long; a notice uses VOICE_HOLDS["notice"]
VOICE_HOLDS = {"listening": LISTEN_HOLD, "thinking": 20, "speaking": 45, "error": 5, "notice": 10, "enroll": 30, "confirm": 30}  # safety timeouts per state
VOICE_TEXT = {"listening": "Go ahead, I’m listening.", "thinking": "Thinking…", "speaking": "…", "error": "Can’t reach Grok right now.", "notice": "Didn’t catch that.", "enroll": "Get ready…", "confirm": "…"}
WAKE_CONTROL = E("GROK_STATION_WAKE_CONTROL", "http://127.0.0.1:8789/enroll")  # wake listener's local enroll endpoint
_WAKE_TOKEN_WARNED = False


def _wake_control_headers(base=None):
    """X-Grok-Wake-Token from the file, read on each call. Unreadable: no header, one generic line, still try."""
    global _WAKE_TOKEN_WARNED
    headers = dict(base or {})
    path = E("WAKE_CONTROL_TOKEN_FILE", "/etc/grok-station/secrets/wake_control_token")
    tok = ""
    try:
        with open(path, "r", encoding="ascii") as f:
            tok = f.read().strip()
    except (OSError, UnicodeError):
        tok = ""
    if not tok:
        if not _WAKE_TOKEN_WARNED:
            _WAKE_TOKEN_WARNED = True
            print("wake control: token unreadable", flush=True)
    else:
        headers["X-Grok-Wake-Token"] = tok
    return headers


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
                    # Bot ids match admin SLUG_RE and relay handoff.BOT_SLUG: a letter, then letters, digits or dashes.
                    # No dot or slash, so find_portrait() joins slug + ext inside PORTRAIT_DIRS only.
                    if re.fullmatch(r"[a-z][a-z0-9-]{1,23}", slug) and name:
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
BG, SURFACE, SURFACE2, SURFACE3, RULE = hexc("0D0B09"), hexc("17130F"), hexc("211B16"), hexc("2A231C"), hexc("2E2620")
TEXT, MUTED, FAINT = hexc("F5EFE6"), hexc("A89B8C"), hexc("6F655A")
FIELD_EDGE = hexc("806F60")  # empty training-progress square, 3 px edge on SURFACE3
LISTENING, THINKING, SPEAKING, AMBER = hexc("3FD3B6"), hexc("A99CFF"), hexc("FFE6BF"), hexc("F5C04A")
RULE2, ALERT_FILL, AMBER_INK = hexc("3A3028"), hexc("241C0E"), hexc("1A1408")
RECIPE_FAINT = hexc("998C7D")  # "Serves", "No timers" on the recipe tile (SPEC faint, not the older FAINT)
RED = hexc("FF5A52")  # mic off, and a stock-down arrow. Problems use text on a quiet ring
GREEN = hexc("4ADE80")  # stock up only
TEAL_BG = hexc("12302A")  # the next calendar row
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
        if self.path == "/recipe":
            return self._recipe()
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
        except Exception:
            return self._reply(400, {"error": "bad json"})
        if isinstance(j, dict) and j.get("op") in ("put", "clear"):
            if not card_from_here(self.client_address[0]):
                return self._reply(403, {"error": "localhost only"})
            return self._card(j)
        try:
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

    def _card(self, j):
        """Localhost card put/clear. The log is the op, the bot, and the id. Never the text or a logo URL."""
        try:
            kind, bot, cid = apply_show(j, logo_cache(), deck())
        except cards.CardError:
            return self._reply(400, {"error": "bad card"})
        tile = None
        if kind == "put":
            page = deck().get(bot, cid)
            if page and page.get("type") in TILE_TYPES:
                tile = {"bot": bot, "id": cid, "card": page, "expires": deck().expiry(bot, cid)}
        pygame.event.post(pygame.event.Event(FEED, tile=tile))
        print(f"show card {kind} bot={bot} id={cid}", flush=True)
        self._reply(200, {"ok": True})

    def _enroll(self):
        """LAN POST /enroll {"phrase":"hey_finn","count":25} (show token): hand it to the wake listener, which owns the mic."""
        try:
            n = int(self.headers.get("Content-Length", 0))
            j = json.loads(self.rfile.read(min(n, 1024)) or b"{}")
            body = json.dumps({"phrase": str(j.get("phrase", ""))[:24], "count": int(j.get("count", 5)), "topup": bool(j.get("topup"))}).encode()
        except Exception:
            return self._reply(400, {"error": "bad json"})
        try:
            req = urllib.request.Request(WAKE_CONTROL, body, _wake_control_headers({"Content-Type": "application/json"}))
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
        if ev["speech"] is not None and not ev["state"] and not ev["has_card"] and not ev["has_timers"]:
            return self._reply(200, {"ok": True})  # 5 Hz speech progress: not logged
        bits = [ev["state"] or "-"]
        if ev["has_card"]:
            bits.append("card" if ev["card"] else "card-ignored")
        if ev["has_timers"]:
            bits.append(f"timers {len(ev['timers'])}")
        print(f"state {' '.join(bits)} who={ev['who']}", flush=True)
        self._reply(200, {"ok": True})

    def _recipe(self):
        """Localhost, no token: a short recipe-card command. The step text is not logged."""
        if not _localhost_client(self.client_address[0]):
            return self._reply(403, {"error": "localhost only"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 1024:
                return self._reply(413, {"error": "too large"})
            j = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._reply(400, {"error": "bad json"})
        cmd = j.get("cmd") if isinstance(j, dict) else None
        if cmd not in ("next", "prev", "repeat", "start", "close", "dismiss"):
            return self._reply(400, {"error": "bad command"})
        if cmd == "dismiss":
            # "dismiss" / "clear that" with a chip on the recipe: the newest chip goes. No chip: not ours.
            chip = dismiss_chip(time.time())
            print(f"recipe dismiss ok={bool(chip)}", flush=True)
            return self._reply(200, {"ok": bool(chip), "closed": False, "say": ""})
        result = cards.apply_recipe_command(deck(), cmd, time.time())
        if result.get("ok") and result.get("closed"):
            pygame.event.post(pygame.event.Event(FEED, tile=None, recipe_closed=True))
        elif result.get("ok"):
            page = deck().get(result["bot"], result["id"])
            tile = {"bot": result["bot"], "id": result["id"], "card": page, "step_cmd": True}
            pygame.event.post(pygame.event.Event(FEED, tile=tile, recipe_cmd=True))
        print(f"recipe {cmd} ok={bool(result.get('ok'))} closed={bool(result.get('closed'))}", flush=True)
        self._reply(200, result)

    def _status(self):
        """Localhost-only snapshot of the status icons. Enums only: no URL, host, address, or token."""
        if not _localhost_client(self.client_address[0]):
            return self._reply(403, {"error": "localhost only"})
        self._reply(200, status_snapshot())

    def log_message(self, *a):
        pass


def _localhost_client(addr):
    return addr.removeprefix("::ffff:") in ("127.0.0.1", "::1")


def card_from_here(addr):
    """Card puts ride /show, but only from localhost. The bearer was already checked."""
    return _localhost_client(addr)


DECK = None
_LOGO = None


def deck():
    """The station's card deck. Tests replace DECK; the process env sets rotation and the ttl cap."""
    global DECK
    if DECK is None:
        DECK = cards.Deck(rotate_s=cards.rotate_s(E("GROK_STATION_CARD_ROTATE", "")),
                          ttl_max=cards.ttl_cap(E("GROK_STATION_CARD_TTL_MAX", "")))
    return DECK


def logo_cache():
    """Logo files under the display cache. An empty allowlist means no fetch."""
    global _LOGO
    if _LOGO is None:
        root = E("GROK_STATION_LOGO_CACHE", "/var/cache/grok-station/logos")
        _LOGO = cards.LogoCache(root, cards.hosts_from_env(os.environ.get("GROK_STATION_LOGO_HOSTS")))
    return _LOGO


def logo_landed():
    """A team logo finished downloading after its score tile went up: repaint so it fills in."""
    try:
        pygame.event.post(pygame.event.Event(STATUS_EV))
    except Exception:
        pass


def apply_show(doc, cache, deck, now=None, on_logo=logo_landed):
    """Re-validate a card op, settle logos, and put or clear. Raises CardError."""
    if not isinstance(doc, dict):
        raise cards.CardError("schema")
    body = dict(doc)
    bot = body.pop("bot", None)
    if not cards.bot_slug_ok(bot):
        raise cards.CardError("schema")
    show = cards.validate_show(body, deck.ttl_max)
    now = time.time() if now is None else now
    if show["op"] == "clear":
        deck.clear(bot, show["id"])
        return "clear", bot, show["id"]
    card = cards.prepare_display_logos(show["card"], cache, on_ready=on_logo)
    deck.put(bot, show["id"], show["priority"], show["ttl_s"], card, now)
    return "put", bot, show["id"]


def feed_identity(now=None):
    """What is on screen from the deck. Content that changes the pixels is part of the key."""
    now = time.time() if now is None else now
    cur = deck().current(now)
    homes = deck().home_row(now)
    full = ""
    if cur and cur["card"]["type"] in ("recipe", "sports", "calendar"):
        c = cur["card"]
        if c["type"] == "recipe":
            mark = str(c["step"])
        elif c["type"] == "sports":
            mark = f"{c['home_score']}-{c['away_score']}-{c['status']}"
        else:
            mark = str(len(c["events"]))
        full = f"{cur['bot']}:{cur['id']}:{c['type']}:{mark}"
    home = ",".join(f"{h['bot']}:{h['id']}" for h in homes)
    return full + "|" + home


def feed_due(last_key, ident):
    """True when the idle or feed screen is showing a different card than the one just painted."""
    if not last_key or last_key[0] not in ("idle", "feed"):
        return False
    return last_key[-1] != ident


# ---------------------------------------------------------------- status (network, grok, mic)
# Polled off the UI thread. Every check takes its inputs as arguments so tests need no hardware.
# The Grok icon folds in the old relay icon: one fault, one icon. The mic stays the voice listener.
_STATUS_LOCK = threading.Lock()
STATUS = {
    "net": {"kind": "none", "up": False},
    "relay": {"ok": None, "detail": ""},
    "mic": {"ok": None, "detail": ""},
    "update": False,
    "grok": {"state": "checking", "cause": None},
    "probe": {},
    "at": 0,
}
_STATUS_ICON_CACHE = {}
GROK_STATES = ("online", "no-internet", "unreachable", "checking", "dim")
GROK_CAUSES = ("service", "relay", "both")


def public_grok(grok):
    """Enums only. Anything else is checking, never a URL, host, or error string."""
    if not isinstance(grok, dict):
        return {"state": "checking", "cause": None}
    state = grok.get("state")
    cause = grok.get("cause")
    if state not in GROK_STATES:
        return {"state": "checking", "cause": None}
    if state != "unreachable" or cause not in GROK_CAUSES:
        return {"state": state if state != "unreachable" else "checking", "cause": None}
    return {"state": "unreachable", "cause": cause}


def status_snapshot():
    """What /status returns. The probe record (and relay miss count) stay inside the process."""
    with _STATUS_LOCK:
        st = STATUS
        net = st.get("net") if isinstance(st.get("net"), dict) else {}
        relay = st.get("relay") if isinstance(st.get("relay"), dict) else {}
        mic = st.get("mic") if isinstance(st.get("mic"), dict) else {}
        detail = relay.get("detail")
        return {"net": {"kind": net.get("kind"), "up": net.get("up")},
                "relay": {"ok": relay.get("ok"), "detail": detail if isinstance(detail, str) else ""},
                "mic": {"ok": mic.get("ok"), "detail": mic.get("detail") if isinstance(mic.get("detail"), str) else ""},
                "update": bool(st.get("update")), "grok": public_grok(st.get("grok")), "at": st.get("at", 0)}


def _status_visible(st):
    net = st.get("net") if isinstance(st.get("net"), dict) else {}
    relay = st.get("relay") if isinstance(st.get("relay"), dict) else {}
    mic = st.get("mic") if isinstance(st.get("mic"), dict) else {}
    grok = public_grok(st.get("grok"))
    return (net.get("kind"), bool(net.get("up")), relay.get("ok"), relay.get("detail"),
            mic.get("ok"), mic.get("detail"), bool(st.get("update")), grok.get("state"), grok.get("cause"))


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


_warned = set()


def _warn_once(msg):
    """Log a message once per process. Never pass secrets."""
    if msg not in _warned:
        _warned.add(msg)
        print(msg, file=sys.stderr, flush=True)


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
    """Authorized /health JSON -> icon. No paired bot, or a body with no bots field, is unpaired (dim icon).

    ok True is green (paired, and the last request did not fail, including before any request).
    ok False is red (last request timed out or its webhook POST failed). Detail never names a host.
    """
    if not isinstance(body, dict):
        return {"ok": None, "detail": ""}
    bots = body.get("bots")
    if bots is None and "key" not in body:
        _warn_once("relay: /health returned no details (token unreadable or unauthorized); not a relay fault")
    if not isinstance(bots, dict) or bots.get("paired") is not True:
        return {"ok": None, "detail": ""}
    if bots.get("ok") is False:
        return {"ok": False, "detail": "down"}
    return {"ok": True, "detail": "ok"}  # paired always shows; green unless the last request failed


def _miss_count(previous):
    """Consecutive fetch misses. A bool is not a count (bool is a subclass of int)."""
    raw = previous.get("misses") if isinstance(previous, dict) else 0
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw if raw > 0 else 0


def check_relay(fetch, previous=None):
    """fetch() -> health JSON or None.

    One failed poll keeps the previous value. After 3 consecutive failures, a paired bot
    (previous ok is true or false) counts as a relay fault. Unpaired stays unpaired.
    A success drops the miss count. The token is never logged.
    """
    try:
        body = fetch()
    except Exception:
        body = None
    if isinstance(body, dict):
        return relay_icon(body)
    prev = previous if isinstance(previous, dict) else {}
    misses = _miss_count(prev) + 1
    if prev.get("ok") is not None and misses >= 3:
        return {"ok": False, "detail": "down", "misses": misses}
    if prev.get("ok") is not None:
        return {"ok": prev.get("ok"), "detail": prev.get("detail") or "", "misses": misses}
    return {"ok": None, "detail": "", "misses": misses}


def fetch_relay_health(timeout=2):
    """GET loopback /health with the local bearer. None on any failure. The token is not logged."""
    token = relay_token()
    if not token:
        _warn_once("relay: no local token readable by the display; not a relay fault")
        return None
    req = urllib.request.Request(relay_health_url(), headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read(8192))
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def read_relay():
    with _STATUS_LOCK:
        prev = dict(STATUS.get("relay") or {})
    return check_relay(fetch_relay_health, prev)


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


def _reset_hold(payload, now, window, grace):
    """True while a planned USB reset may hide the card or a stale capture.

    reset_until is a real positive number, not a bool. It must still be inside the same
    grace used for a reopen, and it must not sit further ahead than a reopen wait
    (window + grace). A negative, huge, or non-numeric deadline does not count.
    """
    if not isinstance(payload, dict):
        return False
    raw = payload.get("reset_until")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return False
    if raw != raw or raw in (float("inf"), float("-inf")) or raw <= 0:
        return False
    if now > raw + grace or raw - now > window + grace:
        return False
    return True


def check_mic(now, fetch, cards_text, stale_s=30, match="emeet", grace_s=10):
    """fetch() -> wake /status JSON or None. ok when the speakerphone is present and capture is alive.
    Uses audio_at (any frame read, any mode) when the listener sends it, else level_at (older listeners).
    A brief reopen (no capture device, audio within grace_s) stays ok, and so does a planned reopen wait:
    while the listener reports reopen_by, ok holds until reopen_by + grace_s. Older listeners omit it.
    A planned USB reset publishes reset_until. The card leaves /proc/asound/cards while it re-enumerates,
    so that deadline is read before a missing card is treated as an unplug. A real unplug has no deadline."""
    try:
        payload = fetch()
    except Exception:
        payload = None
    try:
        window = float(stale_s)
    except (TypeError, ValueError):
        window = 30.0
    try:
        grace = max(0.0, float(grace_s))
    except (TypeError, ValueError):
        grace = 10.0
    if _reset_hold(payload, now, window, grace):
        return {"ok": True, "detail": "ok"}
    if not speakerphone_present(cards_text, match):
        return {"ok": False, "detail": "speakerphone missing"}
    if not isinstance(payload, dict):
        return {"ok": False, "detail": "wake listener down"}
    raw = payload.get("audio_at") if payload.get("audio_at") else payload.get("level_at")
    try:
        alive_at = float(raw)
    except (TypeError, ValueError):
        alive_at = None
    if "capture" in payload and not payload.get("capture"):  # listener up but no input device open
        if payload.get("audio_at") and alive_at and now - alive_at <= grace:
            return {"ok": True, "detail": "ok"}
        try:
            reopen_by = float(payload.get("reopen_by") or 0)
        except (TypeError, ValueError):
            reopen_by = 0.0
        # Bound the deadline so a bad value cannot hold the icon ok forever (backoff never exceeds ~30 s).
        if reopen_by and now <= reopen_by + grace and reopen_by - now <= window + grace:
            return {"ok": True, "detail": "ok"}
        return {"ok": False, "detail": "no capture device"}
    if alive_at is None:
        return {"ok": False, "detail": "capture stalled"}
    if now - alive_at > window:
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
        req = urllib.request.Request(target, headers=_wake_control_headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
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


_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def semver_tuple(value):
    if not isinstance(value, str):
        return None
    match = _SEMVER.fullmatch(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def update_available(cache, installed):
    """True only when the cached check names a newer MAJOR.MINOR.PATCH and a 40-hex commit. No idle state."""
    if not isinstance(cache, dict) or cache.get("error"):
        return False
    sha = cache.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return False
    avail = semver_tuple(cache.get("available")) if isinstance(cache.get("available"), str) else None
    if avail is None:
        return False
    have = semver_tuple(installed) if isinstance(installed, str) else None
    if have is None:
        return True
    return avail > have


def read_update_available(cache_path=None, version_path=None):
    """The admin page's cached check. Unreadable or not-newer is absent, not a gray icon."""
    cache_path = cache_path or E("GROK_STATION_UPDATE_CHECK", "/var/lib/grok-station/admin/update-check.json")
    version_path = version_path or E("GROK_STATION_VERSION_FILE", "/opt/grok-station/VERSION")
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return False
    try:
        with open(version_path) as f:
            installed = f.read().strip()
    except OSError:
        installed = ""
    return update_available(cache, installed)


def grok_poll_s(raw=None):
    """Healthy probe interval. GROK_STATION_GROK_POLL, default 60 s, never under 30 s."""
    if raw is None:
        raw = E("GROK_STATION_GROK_POLL", "60")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 60.0
    return 30.0 if v < 30 else v


def grok_probe_every(shown, healthy=None, fast=None):
    """60 s (min 30) while online. Every status poll while checking or in a problem state."""
    healthy = grok_poll_s() if healthy is None else healthy
    fast = status_poll_s() if fast is None else fast
    return healthy if shown == "online" else fast


def _num(value):
    """A real number, or None. Bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_net_probe(text):
    """'1.1.1.1:443' -> (ip, port). An IP literal only, so the connect does no DNS."""
    raw = str(text or "").strip()
    if raw.count(":") != 1:
        return None
    host, port_s = raw.split(":", 1)
    if not port_s.isdigit():
        return None
    port = int(port_s)
    if not 1 <= port <= 65535:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host, port


def net_probe_targets(raw=None):
    """Primary from GROK_STATION_NET_PROBE (default 1.1.1.1:443), then 8.8.8.8:443."""
    if raw is None:
        raw = str(E("GROK_STATION_NET_PROBE", "") or "").strip()
    primary = parse_net_probe(raw) if raw else ("1.1.1.1", 443)
    if primary is None:
        primary = ("1.1.1.1", 443)
    fallback = ("8.8.8.8", 443)
    return [primary] if primary == fallback else [primary, fallback]


def probe_internet(connect, timeout=2.0, targets=None):
    """TCP connect only. No bytes, no TLS, no DNS. True if any target accepts."""
    if not callable(connect):
        raise TypeError("connect")
    last = None
    for host, port in (targets if targets is not None else net_probe_targets()):
        try:
            ipaddress.ip_address(str(host))
            port = int(port)
        except (TypeError, ValueError):
            continue
        try:
            sock = connect((str(host), port), timeout)
        except Exception as e:
            last = e
            continue
        close = getattr(sock, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return True
    if last is not None:
        _warn_once("internet probe failed: %s" % type(last).__name__)
    return False


def https_probe_url(raw):
    """An https URL with a host and no userinfo, or None. Nothing else is fetched."""
    text = str(raw or "").strip()
    parts = urllib.parse.urlsplit(text)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    return text


def grok_probe_url():
    raw = str(E("GROK_STATION_GROK_PROBE_URL", "") or "").strip()
    return https_probe_url(raw or "https://api.x.ai/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is already an HTTP response. Do not follow it, and do not read the Location."""

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, "redirect", headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def fetch_grok_https(url, timeout):
    """Unauthenticated GET. No Authorization header. Reads at most 1 KB. Returns the status."""
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, method="GET", headers={"Accept": "*/*", "User-Agent": "grok-station"})
    try:
        with opener.open(req, timeout=timeout) as r:
            r.read(1024)
            return int(getattr(r, "status", 200))
    except urllib.error.HTTPError as e:
        try:
            e.read(1024)
        except Exception:
            pass
        code = int(e.code)
        try:
            e.close()
        except Exception:
            pass
        return code


def probe_grok(fetch, timeout=3, url=None):
    """True when the service returns any HTTP status below 500. 5xx, DNS, TLS, and timeouts are down.

    fetch(url, timeout) -> status. A non-https URL is not fetched.
    """
    if not callable(fetch):
        raise TypeError("fetch")
    target = grok_probe_url() if url is None else https_probe_url(url)
    if not target:
        return False
    try:
        status = fetch(target, timeout)
    except Exception as e:
        _warn_once("grok probe failed: %s" % type(e).__name__)
        return False
    if isinstance(status, bool) or not isinstance(status, int):
        try:
            status = int(status)
        except (TypeError, ValueError):
            return False
    return 100 <= status < 500


def _probe_count(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("fails")
    if value < 0:
        raise ValueError("fails")
    return value


def _sample(probe, key):
    """up, down (2 failures), pending (1 failure), none, or bad."""
    val = probe.get(key)
    if val is True:
        return "up"
    if val is None:
        return "none"
    if val is not False:
        return "bad"
    fails = _probe_count(probe.get(key + "_fails", 0))
    if fails >= 2:
        return "down"
    if fails == 1:
        return "pending"
    return "bad"


def _relay_fault(relay):
    if relay is None:
        return False
    if not isinstance(relay, dict):
        raise ValueError("relay")
    ok = relay.get("ok")
    if ok is None:
        return False
    if isinstance(ok, bool):
        return not ok
    raise ValueError("relay")


def _probe_fresh(probe, now):
    """False when there is no result yet, or the result is older than 3x the probe interval."""
    if not isinstance(probe, dict):
        return False
    at = _num(probe.get("at"))
    if at is None:
        return False
    interval = probe.get("interval", 60)
    interval = _num(interval)
    if interval is None or interval <= 0:
        raise ValueError("interval")
    now_n = _num(now)
    if now_n is None:
        raise ValueError("now")
    return (now_n - at) <= 3 * interval


def grok_state(net, relay, probe, now):
    """Pure. net down -> dim. No fresh positive probe -> never online.

    A probe failure becomes no-internet or unreachable only after 2 consecutive failures.
    One success recovers at once. Unknown, unparsable, stale, or raised input is checking.
    """
    try:
        return _grok_state(net, relay, probe, now)
    except Exception:
        return {"state": "checking", "cause": None}


def _held_problem(probe):
    """The problem state last shown, kept through a single miss so the icon does not flicker to checking."""
    shown = probe.get("shown")
    if shown == "no-internet":
        return {"state": "no-internet", "cause": None}
    if shown == "unreachable" and probe.get("cause") in GROK_CAUSES:
        return {"state": "unreachable", "cause": probe.get("cause")}
    return None


def _grok_state(net, relay, probe, now):
    if not isinstance(net, dict) or not isinstance(net.get("up"), bool):
        raise ValueError("net")
    if net.get("up") is False:
        return {"state": "dim", "cause": None}
    if not _probe_fresh(probe, now):
        return {"state": "checking", "cause": None}
    inet = _sample(probe, "internet")
    svc = _sample(probe, "grok")
    if inet == "bad" or svc == "bad":
        return {"state": "checking", "cause": None}
    relay_bad = _relay_fault(relay)
    if inet == "down":
        return {"state": "no-internet", "cause": None}
    shown = probe.get("shown")
    shown_online = shown == "online"
    held = _held_problem(probe)
    if inet == "pending":
        if relay_bad:
            return {"state": "unreachable", "cause": "relay"}
        if shown_online:
            return {"state": "online", "cause": None}
        if held is not None:
            return held
        return {"state": "checking", "cause": None}
    if inet != "up":
        return {"state": "checking", "cause": None}
    if svc == "pending":
        if relay_bad:
            return {"state": "unreachable", "cause": "relay"}
        if shown_online:
            return {"state": "online", "cause": None}
        if held is not None and held["state"] == "unreachable":
            return held  # the internet is back up, so a held no-internet would be wrong
        return {"state": "checking", "cause": None}
    if svc == "none":
        if relay_bad:
            return {"state": "unreachable", "cause": "relay"}
        return {"state": "checking", "cause": None}
    svc_bad = svc == "down"
    if svc_bad or relay_bad:
        if svc_bad and relay_bad:
            cause = "both"
        elif svc_bad:
            cause = "service"
        else:
            cause = "relay"
        return {"state": "unreachable", "cause": cause}
    if svc == "up":
        return {"state": "online", "cause": None}
    return {"state": "checking", "cause": None}


def probe_due(probe, now, healthy=None, fast=None):
    if not isinstance(probe, dict):
        return True
    at = _num(probe.get("at"))
    if at is None:
        return True
    every = grok_probe_every(probe.get("shown"), healthy, fast)
    now_n = _num(now)
    if now_n is None:
        return True
    return now_n - at >= every


def sample_probe(prev, net, now, connect, fetch, timeout_net=2, timeout_grok=3):
    """One internet probe, then the Grok probe only when the network and the internet are up.

    A down network does not record a failure (the network icon already carries that).
    connect and fetch are injected; this function opens nothing itself.
    """
    prev = dict(prev) if isinstance(prev, dict) else {}
    prev["interval"] = grok_poll_s()
    if not (isinstance(net, dict) and net.get("up") is True):
        return prev
    try:
        inet_fails = _probe_count(prev.get("internet_fails", 0))
        grok_fails = _probe_count(prev.get("grok_fails", 0))
    except ValueError:
        inet_fails, grok_fails = 0, 0
    internet = bool(probe_internet(connect, timeout_net))
    if internet:
        inet_fails = 0
        grok = bool(probe_grok(fetch, timeout_grok))
        grok_fails = 0 if grok else grok_fails + 1
    else:
        inet_fails += 1
        grok = None
    prev.update({"at": now, "internet": internet, "grok": grok,
                 "internet_fails": inet_fails, "grok_fails": grok_fails})
    return prev


def tick_grok(prev, net, relay, now, connect, fetch):
    """Probe when due, then decide. `shown` is what the icon last committed to, for the debounce hold."""
    probe = dict(prev) if isinstance(prev, dict) else {}
    if probe_due(probe, now):
        probe = sample_probe(probe, net, now, connect, fetch)
    decision = grok_state(net, relay, probe, now)
    probe["shown"] = decision["state"]
    probe["cause"] = decision.get("cause")
    probe["interval"] = grok_poll_s()
    return probe, public_grok(decision)


def collect_status(now=None, connect=None, fetch_grok=None):
    now = time.time() if now is None else now
    with _STATUS_LOCK:
        prev_probe = dict(STATUS.get("probe") or {})
    net = read_net()
    relay = read_relay()
    connect = socket.create_connection if connect is None else connect
    fetch_grok = fetch_grok_https if fetch_grok is None else fetch_grok
    probe, grok = tick_grok(prev_probe, net, relay, now, connect, fetch_grok)
    return {"net": net, "relay": relay, "mic": read_mic(now), "update": read_update_available(),
            "grok": grok, "probe": probe, "at": now}


def status_loop():
    """Daemon poll. Posts STATUS_EV only when an icon would change. Never logs a URL or host."""
    while True:
        started = time.time()
        try:
            if store_status(collect_status(now=started)):
                pygame.event.post(pygame.event.Event(STATUS_EV))
        except Exception as e:
            _warn_once("status poll failed: %s" % type(e).__name__)
        delay = status_poll_s() - (time.time() - started)
        if delay > 0:
            time.sleep(delay)


def net_icon_name(net):
    up = bool(net.get("up"))
    if net.get("kind") == "ethernet":
        return "ethernet-connected" if up else "ethernet-disconnected"
    return "wifi-connected" if up else "wifi-disconnected"


_GROK_ICONS = {
    "online": "grok-online",
    "no-internet": "grok-no-internet",
    "unreachable": "grok-unreachable",
    "checking": "grok-checking",
    "dim": "grok-dim",
}


def grok_icon_name(grok):
    """Fail closed: an unknown state draws the checking icon, never the teal one."""
    state = grok.get("state") if isinstance(grok, dict) else None
    return _GROK_ICONS.get(state, "grok-checking")


def status_icon_names(st):
    """Right to left: network, Grok (the old relay slot), mic (hidden until the first reading), update (only while a newer version is cached)."""
    st = st or {}
    names = [net_icon_name(st.get("net") or {})]
    names.append(grok_icon_name(st.get("grok")))
    mic = st.get("mic") or {}
    if mic.get("ok") is not None:
        names.append("mic-ok" if mic.get("ok") else "mic-problem")
    if st.get("update"):
        names.append("update-available")
    return names


def status_icon_px(scale):
    """44 px at 1080p times the display scale, never under 28 px on screen."""
    return max(28, int(round(44 * scale)))


def _paint_grok_glyph(surf, color, px):
    """Speech-bubble outline and a wave, stroke only, so it weighs the same as the neighbouring icons."""
    sw = max(1, px // 10)
    pad = max(1, px // 8)
    body = pygame.Rect(pad, pad, max(2, int(px * 0.62)), max(2, int(px * 0.52)))
    rad = max(1, min(body.w, body.h) // 4)
    thick = max(1, min(sw, body.w // 2, body.h // 2))
    pygame.draw.rect(surf, color, body, thick, border_radius=rad)
    tail = [
        (body.left + body.w // 3, body.bottom - 1),
        (body.left + thick, min(px - 1, body.bottom + max(2, px // 7))),
        (body.left + body.w // 2, body.bottom - 1),
    ]
    if tail[1][1] > tail[0][1]:
        pygame.draw.lines(surf, color, False, tail, thick)
    x0 = body.left + thick * 2
    x1 = body.right - thick * 2
    if x1 > x0 + 2:
        pts = []
        steps = 8
        amp = max(1, body.h // 6)
        for i in range(steps + 1):
            t = i / steps
            pts.append((x0 + (x1 - x0) * t, body.centery + math.sin(t * math.pi * 2) * amp))
        pygame.draw.lines(surf, color, False, pts, thick)


def _fallback_status_icon(name, px):
    """Teal glyph when the state is good. Bad icons stay muted plus an amber slash.
    The Grok mark follows the same stroke. Dim is muted at 75% (#81776B). Unknown Grok names stay muted."""
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
    elif name == "relay-unpaired":
        r = max(2, px // 6)
        dim = tuple(int(c * 0.5) for c in MUTED[:3])
        pygame.draw.circle(surf, dim, (pad + r, px // 2), r, stroke(sw, r))
        pygame.draw.circle(surf, dim, (max(pad + r, px - pad - r), px // 2), r, stroke(sw, r))
        a, b = pad + 2 * r, max(pad + 2 * r, px - pad - 2 * r)
        gap = max(1, (b - a) // 3)
        pygame.draw.line(surf, dim, (a, px // 2), (a + gap, px // 2), sw)
        pygame.draw.line(surf, dim, (b - gap, px // 2), (b, px // 2), sw)
    elif name.startswith("relay"):
        r = max(2, px // 6)
        pygame.draw.circle(surf, color, (pad + r, px // 2), r, stroke(sw, r))
        pygame.draw.circle(surf, color, (max(pad + r, px - pad - r), px // 2), r, stroke(sw, r))
        pygame.draw.line(surf, color, (pad + 2 * r, px // 2), (max(pad + 2 * r, px - pad - 2 * r), px // 2), sw)
    elif name == "update-available":
        cx = px // 2
        top, bot = pad, max(pad + 2, px - pad)
        pygame.draw.line(surf, color, (cx, top), (cx, bot - sw), sw)
        pygame.draw.line(surf, color, (pad, px // 2), (cx, bot), sw)
        pygame.draw.line(surf, color, (px - pad, px // 2), (cx, bot), sw)
    elif name.startswith("grok"):
        if name == "grok-dim":
            color = hexc("81776B")
        elif name == "grok-online":
            color = LISTENING
        elif name in ("grok-no-internet", "grok-unreachable"):
            color = AMBER
        else:
            color = MUTED
        _paint_grok_glyph(surf, color, px)
        if name == "grok-no-internet" and px >= 4:
            a, b = (pad, pad), (px - pad - 1, px - pad - 1)
            pygame.draw.line(surf, hexc("0D0B09"), a, b, sw + 2)
            pygame.draw.line(surf, AMBER, a, b, sw)
        elif name == "grok-unreachable" and px >= 8:
            r = max(3, px // 6)
            cx, cy = px - pad - r + 1, px - pad - r + 1
            pygame.draw.circle(surf, AMBER, (cx, cy), r)
            pygame.draw.line(surf, BG, (cx, cy - r // 2), (cx, cy + max(1, r // 6)), max(1, sw // 2))
            pygame.draw.circle(surf, BG, (cx, cy + r // 2), max(1, sw // 4))
        elif name == "grok-checking" and px >= 8:
            r = max(3, px // 6)
            cx, cy = px - pad - r + 1, px - pad - r + 1
            rect = pygame.Rect(cx - r, cy - r, r * 2, r * 2)
            pygame.draw.arc(surf, MUTED, rect, math.radians(40), math.radians(320), max(1, sw // 2))
        return surf
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


_DIM = {"sig": None, "lux": None, "at": 0.0, "face": None, "mode": "off", "why": None}


def night_face(now):
    """Idle night clock. Off keeps the hour window. A dim mode follows the dim window, and only
    while the night-clock toggle is on (NIGHT is not empty). Partial sensor light stays on the day screen."""
    if dimming is None:
        return is_night(now)
    cfg = dimming.config_from_env()
    if cfg.get("mode") == "off":
        return is_night(now)
    if not NIGHT:
        return False
    return dimming.in_dim_period(cfg, now, lux=_DIM.get("lux"))


def refresh_dim(force=False):
    """Apply brightness at most every 30 seconds. Returns 'overlay', 'night', or None.

    'overlay' means the software dim changed (repaint even during a session).
    'night' means the idle night face changed (repaint only when nothing else is on screen).
    """
    if dimming is None:
        return None
    t = time.monotonic()
    if not force and t - _DIM["at"] < 30:
        return None
    _DIM["at"] = t
    cfg = dimming.config_from_env()
    _DIM["mode"] = cfg.get("mode") or "off"
    root = E("GROK_STATION_SYS_ROOT", "/")
    stamp = E("GROK_STATION_DIM_STAMP", "/var/cache/grok-station/dim-level")
    lux = dimming.current_lux(root) if cfg.get("mode") == "auto-sensor" else None
    _DIM["lux"] = lux
    level, why = dimming.select_level(cfg, time.localtime(), lux=lux)
    key = ("off",) if cfg.get("mode") == "off" else (level,)
    prev = dimming.overlay_level()
    if why != _DIM.get("why"):
        _DIM["why"] = why
        if why:  # no-sensor, no-times, no-location: no coordinates, no paths
            print(f"dim {why}", flush=True)
    if key != _DIM["sig"]:
        if cfg.get("mode") == "off":
            dimming.apply_brightness(None, root, stamp)
            print("dim off", flush=True)
        else:
            how = dimming.apply_brightness(level, root, stamp)
            if how == "backlight":
                print(f"dim backlight {level}", flush=True)
            else:
                print(f"dim overlay {level}", flush=True)
        _DIM["sig"] = key
    face = night_face(time.localtime())
    overlay_changed = dimming.overlay_level() != prev
    face_changed = face != _DIM["face"]
    _DIM["face"] = face
    if overlay_changed:
        return "overlay"
    if face_changed:
        return "night"
    return None


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


def ring_none(c, cx, cy, r, ring_w, glow, t):
    """No portrait ring. The training tile draws its own solid or dashed ring in the fixed box."""
    return


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
         "success": ring_success, "enroll_ready": ring_ready, "enroll_saved": ring_saved, "none": ring_none}

_photo_cache = {}  # (path, mtime, svg, svg_mtime, d, k, initial, photo_ok, kind) -> circle-cropped surface
_MARK_CACHE = {}
_MARK_PX = (64, 128, 256, 512, 1024)  # assets/brand/mark-<px>.png, rendered from mark.svg by tools/render_brand.py
LAST_PORTRAIT_RECT = LAST_PORTRAIT_ARGS = None  # last session portrait blit, so the ring can spin in place
LAST_ENROLL_RECT = LAST_ENROLL_AVATAR = None    # training tile and its fixed avatar box
LAST_ENROLL_BUTTONS = []                        # (id, rect) in screen px, for enroll_hit
LAST_ENROLL_HINT = None                         # muted "To stop, stay quiet." line; the station has no Cancel
LAST_ENROLL_PROGRESS = None                     # progress row the hint sits under
LAST_ENROLL_HEADLINE = None                     # headline box; height is font ascent plus descent
LAST_ENROLL_STAGE = None
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
    speech = speech_progress(j.get("speech"))
    if not has_state and card is None and timers is None and speech is None:
        raise ValueError("nothing to apply")
    heard = " ".join(str(j.get("heard") or "").split())[:MAX_HEARD]
    text = str(j.get("text") or "").strip()[:MAX_TEXT]
    who = persona_names().get(name_key(j.get("who") or VOICE_NAME), (VOICE_NAME, "grok"))[0]
    raw_hold = j.get("hold_s")
    # Only an exact 4 s hold is honored (the built-in Grok line, a failed enroll start). Anything else is a normal hold.
    hold_s = 4 if (not isinstance(raw_hold, bool) and isinstance(raw_hold, (int, float)) and raw_hold == 4) else None
    return {"state": raw_state if has_state else None, "heard": heard, "text": text, "done": bool(j.get("done")),
            "who": who, "has_card": has_card, "card": card, "has_timers": timers is not None, "timers": timers,
            "speech": speech, "hold_s": hold_s}


def speech_progress(raw):
    """{"p": 0..1} from the wake listener while a reply plays (spoken audio time / audio received), or None."""
    if not isinstance(raw, dict):
        return None
    p = raw.get("p")
    if type(p) not in (int, float) or p != p:
        return None
    return min(1.0, max(0.0, float(p)))


def dismiss_chip(now):
    """Drop the newest recipe chip and its deck card. Returns the chip, or None when there was none."""
    chip = CHIPS.dismiss(now)
    if chip:
        deck().clear(chip["bot"], chip["id"])
        pygame.event.post(pygame.event.Event(FEED, tile=None, chips_changed=True))
    return chip


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
    for t in items[:8]:
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
        rec = {"id": tid, "label": label, "ends_at_epoch": float(ends), "ringing": bool(t.get("ringing"))}
        dur = t.get("duration")
        if isinstance(dur, bool) or not isinstance(dur, (int, float)) or not math.isfinite(float(dur)):
            dur = None
        elif not 1 <= float(dur) <= 86400:
            dur = None
        if dur is not None:
            rec["duration"] = float(dur)
        out.append(rec)
        seen.add(tid)
    return out


def fmt_remaining(sec):
    """mm:ss with a leading zero, or h:mm:ss once an hour is left. Counts down; never negative."""
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_timer_clock(sec):
    """Ring digits: 7:42, not 07:42. Hours stay h:mm:ss. Never negative."""
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def timer_title(label):
    """Display name. Word characters only are already enforced; this only fixes caps."""
    parts = []
    for w in str(label or "timer").split():
        parts.append(w[:1].upper() + w[1:] if w else w)
    return " ".join(parts)[:24] or "Timer"


def _curly(text):
    inner = str(text or "").strip()
    if len(inner) >= 2 and inner[0] in "\u201c\"" and inner[-1] in "\u201d\"":
        inner = inner[1:-1]
    return "\u201c" + inner + "\u201d"


def hint_spans(segments):
    """(text, color) for one voice hint. phrase is TEXT in curly quotes; text is TEXT; anything else is MUTED."""
    spans = []
    for seg in segments or ():
        if not isinstance(seg, (tuple, list)) or len(seg) < 2:
            continue
        raw, role = seg[0], seg[1]
        text = "" if raw is None else str(raw)
        if role == "phrase":
            spans.append((_curly(text), TEXT))
        elif role == "text":
            spans.append((text, TEXT))
        else:
            spans.append((text, MUTED))
    return spans


def voice_hint_plain(segments):
    return "".join(text for text, _color in hint_spans(segments))


TIMER_HINT_SEGMENTS = (("Say ", "muted"), ("stop", "phrase"), (" to dismiss", "muted"))
TIMER_HINT = voice_hint_plain(TIMER_HINT_SEGMENTS)
RECIPE_HINT_SEGMENTS = (
    ("Say ", "muted"), ("next step", "phrase"), ("  ·  ", "muted"),
    ("previous step", "phrase"), ("  ·  ", "muted"), ("close recipe", "phrase"),
)
RING_SLOTS = 4  # s05: up to four rings; the rest are chips. A ringing timer is the only ring (s06).


def anchor_timer_chips(chips, wall=None, mono=None):
    """Stamp each chip with the remaining time and a monotonic anchor.

    Later ticks use the anchor, so a wall-clock step does not move the digits. `wall` and `mono`
    are injectable for tests.
    """
    wall = time.time() if wall is None else wall
    mono = time.monotonic() if mono is None else mono
    for t in chips:
        t["_left"] = float(t.get("ends_at_epoch", wall)) - wall
        t["_mono"] = mono
    return chips


def chip_left(t, epoch=None):
    """Seconds left. An explicit epoch or TIMER_EPOCH (render and tests) wins over the anchor."""
    if epoch is not None or TIMER_EPOCH is not None:
        base = epoch if epoch is not None else TIMER_EPOCH
        return float(t.get("ends_at_epoch", 0)) - base
    if isinstance(t.get("_left"), (int, float)) and isinstance(t.get("_mono"), (int, float)):
        return t["_left"] - (time.monotonic() - t["_mono"])
    return float(t.get("ends_at_epoch", 0)) - time.time()


def _pinned_epoch(epoch):
    """None means 'live': chip_left uses the monotonic anchor. A number is a frozen clock."""
    if epoch is not None:
        return epoch
    return TIMER_EPOCH


def _shown_ringing(t, left):
    """The ringing flag, or the countdown has already reached zero.

    The chime can wait until a conversation ends. The screen still shows the timer as done,
    and "stop" can dismiss it.
    """
    return bool(t.get("ringing")) or left <= 0


def any_timer_ringing():
    """True when a chip is flagged ringing or its anchored countdown has reached zero."""
    for t in TIMER_CHIPS:
        if _shown_ringing(t, chip_left(t, None)):
            return True
    return False


def timer_screen(timers, now=None):
    """Display payload for the timers screen.

    `now` is an epoch. None uses chip_left (monotonic anchor, or TIMER_EPOCH).
    Running: up to RING_SLOTS rings, soonest first, overflow as chips.
    Ringing: the first ringing timer is one amber ring; every other timer is a chip.
    A countdown that has reached zero is ringing even before the chime post arrives.
    """
    def left_of(t):
        if now is not None:
            return float(t.get("ends_at_epoch", 0)) - now
        return chip_left(t, None)

    ordered = sorted(timers or [], key=lambda t: (not _shown_ringing(t, left_of(t)), float(t.get("ends_at_epoch", 0)), str(t.get("label", ""))))

    def fraction(t, left, ringing):
        if ringing:
            return 1.0
        dur = t.get("duration")
        if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0:
            return max(0.0, min(1.0, left / float(dur)))
        return 1.0 if left > 0 else 0.0

    def ring_row(t, left, ringing):
        return {"id": t.get("id"), "label": t.get("label") or "timer", "title": timer_title(t.get("label")),
                "remaining": 0 if ringing else max(0, int(round(left))),
                "text": "0:00" if ringing else fmt_timer_clock(left),
                "fraction": round(fraction(t, left, ringing), 4), "ringing": ringing,
                "color": "amber" if ringing else "teal"}

    def chip_row(t, left):
        ringing = bool(t.get("ringing"))
        return {"id": t.get("id"), "label": t.get("label") or "timer", "title": timer_title(t.get("label")),
                "remaining": 0 if ringing else max(0, int(round(left))),
                "text": "0:00" if ringing else fmt_timer_clock(left), "ringing": ringing}

    if any(_shown_ringing(t, left_of(t)) for t in ordered):
        hero = next(t for t in ordered if _shown_ringing(t, left_of(t)))
        rest = [t for t in ordered if t is not hero]
        return {"mode": "ringing", "rings": [ring_row(hero, 0, True)],
                "chips": [chip_row(t, left_of(t)) for t in rest],
                "headline": timer_title(hero.get("label")) + "\nis done", "hint": TIMER_HINT}
    rings, chips = ordered[:RING_SLOTS], ordered[RING_SLOTS:]
    return {"mode": "rings",
            "rings": [ring_row(t, left_of(t), False) for t in rings],
            "chips": [chip_row(t, left_of(t)) for t in chips],
            "headline": None, "hint": None}


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
    ordered = sorted(timers, key=lambda t: (not _shown_ringing(t, chip_left(t, epoch)), t.get("ends_at_epoch", 0)))[:10]
    shown, extra = ordered[:4], max(0, len(ordered) - 4)

    def right_of(t):
        if _shown_ringing(t, chip_left(t, epoch)):
            return "Done"
        return fmt_remaining(chip_left(t, epoch))

    labels = [timer_title(t.get("label") or "timer") for t in shown]
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
    phase_at = time.time() if epoch is None else epoch
    phase = int(phase_at) % 2 == 0
    prev = surf.get_clip()
    surf.set_clip(rect)
    amber_bg = hexc("33270F")
    for i, (t, lab, tim) in enumerate(zip(shown, labels, times)):
        w = widths[i]
        box = pygame.Rect(x, rect.y, w, rect.h)
        rad = min(box.w, box.h) // 2
        if _shown_ringing(t, chip_left(t, epoch)):
            bg = AMBER if phase else amber_bg
            fg = BG if phase else AMBER
            lc = BG if phase else SPEAKING
        else:
            bg, fg, lc = SURFACE2, TEXT, MUTED
        pygame.draw.rect(surf, bg, box, border_radius=rad)
        if _shown_ringing(t, chip_left(t, epoch)):
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
    draw_timer_chips(surf, rect, TIMER_CHIPS, _pinned_epoch(epoch))
    LAST_TIMER_RECT = rect
    return rect


def repaint_timer_strip(disp, epoch=None):
    """Redraw only the strip and flush that rectangle. Called once a second while any timer is up."""
    rect = LAST_TIMER_RECT
    if not rect or not TIMER_CHIPS:
        return
    disp.screen.fill(BG, rect)
    draw_timer_chips(disp.screen, rect, TIMER_CHIPS, _pinned_epoch(epoch))
    disp.flush_rect(rect)


def _ring_pulse(epoch):
    """0 at the whole second, 1 halfway through. One cycle per second, no red."""
    src = time.time() if epoch is None else epoch
    return 0.5 - 0.5 * math.cos(2 * math.pi * (src % 1.0))


def _paint_countdown_ring(diameter, fraction, color, pulse):
    """Teal or amber arc on a rule track, clockwise from the top. pulse 0..1 grows an amber glow."""
    d = max(16, int(diameter))
    pulse = 0.0 if pulse < 0 else (1.0 if pulse > 1 else pulse)
    pad = max(4, int(d * (0.06 + 0.04 * pulse)))
    canvas = d + 2 * pad
    k = 2
    surf = pygame.Surface((canvas * k, canvas * k), pygame.SRCALPHA)
    cx = cy = surf.get_width() // 2
    width = max(k * 3, int(d * 0.055) * k)
    r = (d * k) // 2 - width
    pygame.draw.circle(surf, RULE, (cx, cy), r, width)
    frac = max(0.0, min(1.0, float(fraction)))
    if frac >= 0.999:
        pygame.draw.circle(surf, color, (cx, cy), r, width)
    elif frac > 0:
        steps = max(12, int(360 * frac))
        start = -math.pi / 2
        rad = max(1, int(width / 2))
        for i in range(steps + 1):
            a = start + 2 * math.pi * frac * (i / steps)
            pygame.draw.circle(surf, color, (int(cx + r * math.cos(a)), int(cy + r * math.sin(a))), rad)
    if pulse > 0.04:
        glow = (*AMBER[:3], int(90 * pulse))
        pygame.draw.circle(surf, glow, (cx, cy), int(r + width * (0.6 + pulse)), max(2, int(width * 0.45)))
    out = pygame.transform.smoothscale(surf, (canvas, canvas))
    return out, pad


def _blit_ring_block(surf, ring, cx, cy, row):
    """Center a countdown ring on (cx, cy). Returns the ring's rect."""
    img, _pad = ring
    rect = img.get_rect(center=(cx, cy))
    surf.blit(img, rect.topleft)
    time_px = max(14, int(rect.w * 0.20))
    name_px = max(14, int(rect.w * 0.08))
    color = AMBER if row.get("color") == "amber" else TEXT
    name_color = AMBER if row.get("color") == "amber" else MUTED
    tr = text_at(surf, "display", time_px, row["text"], color, rect.center, "center")
    nf = font("display", name_px)
    title = row["title"] if nf.size(row["title"])[0] <= rect.w else ellipsize(nf, row["title"], rect.w)
    text_at(surf, "display", name_px, title, name_color, (rect.centerx, tr.bottom + max(4, rect.w // 40)), "midtop")
    return rect


def draw_timers(surf, L, now, epoch=None):
    """Idle timers (s05) or one ringing timer (s06). No red: ringing is amber.

    `now` is a struct_time for the corner clock. `epoch` pins the countdown in tests.
    """
    global LAST_TOPBAR_RECT, LAST_CARD_RECT, LAST_PORTRAIT_RECT, LAST_PORTRAIT_ARGS
    global LAST_WEATHER_RECT, LAST_OFFLINE_RECT, LAST_DATE_RECT, LAST_CLOCK_RECT, LAST_TIMER_RECT
    LAST_CARD_RECT = LAST_PORTRAIT_RECT = LAST_PORTRAIT_ARGS = None
    LAST_WEATHER_RECT = LAST_OFFLINE_RECT = LAST_DATE_RECT = LAST_CLOCK_RECT = None
    surf.fill(BG)
    pinned = _pinned_epoch(epoch)
    payload = timer_screen(TIMER_CHIPS, pinned)
    side, top, bot = safe_insets(L.w, L.h)
    se = min(L.w, L.h)
    # Same top-bar center line as the other station screens (mockup y = 34 + 72/2).
    bar_y = L(34) + L(72) // 2
    lab = text_at(surf, "body", max(14, L(40)), "Timers", MUTED, (side, bar_y), "midleft")
    hhmm, ampm = clock_parts(now)
    clk = text_at(surf, "display", max(14, L(52)), f"{hhmm} {ampm}", TEXT, (L.w - side, bar_y), "midright")
    LAST_CLOCK_RECT = clk
    icons = draw_status_icons(surf, L, right=clk.left - max(8, L(16)), cy=bar_y)
    LAST_TOPBAR_RECT = lab.union(clk).union(icons)
    chip_h = timer_strip_height(se) if payload["chips"] else 0
    gap = max(8, L(24))
    content_top = max(LAST_TOPBAR_RECT.bottom + gap, top)
    content_bot = L.h - bot - (chip_h + gap if chip_h else 0)
    avail_w = max(1, L.w - 2 * side)
    avail_h = max(1, content_bot - content_top)
    pulse = _ring_pulse(time.time() if pinned is None else pinned)
    ring_rects = []
    if payload["mode"] == "ringing" and payload["rings"]:
        row = payload["rings"][0]
        text_min = max(L(280), int(avail_w * 0.34))
        d = min(L(640), avail_h, int(avail_w * 0.48))
        d = max(48, d)
        stack = d + gap + text_min > avail_w or avail_w < L(900)
        if stack:
            d = max(48, min(d, avail_w, int(avail_h * 0.55)))
            cx, cy = L.w // 2, content_top + d // 2
        else:
            cx = side + d // 2
            cy = content_top + avail_h // 2
        ring_rects.append(_blit_ring_block(surf, _paint_countdown_ring(d, 1.0, AMBER, pulse), cx, cy, row))
        if stack:
            tx, ty, tw = side, ring_rects[0].bottom + gap, avail_w
        else:
            tx = ring_rects[0].right + gap
            tw = max(1, L.w - side - tx)
            ty = cy - L(160)
        head = payload["headline"] or ""
        f, lines, step = fit_text("display", head, tw, max(L(80), avail_h // 3), max(14, L(120)), 14, max_lines=2)
        ty = draw_lines(surf, f, lines, step, AMBER, tx, max(content_top, ty), align="left", width=tw)
        draw_voice_hint(surf, TIMER_HINT_SEGMENTS, tx, ty + gap, px=L(36), max_w=tw)
    else:
        n = len(payload["rings"])
        if n:
            gap_r = L(80) if n <= 2 else L(28)
            d = min(L(620), avail_h, int((avail_w - gap_r * (n - 1)) / n))
            d = max(48, d)
            row_w = n * d + gap_r * (n - 1)
            x = side + max(0, (avail_w - row_w) // 2) + d // 2
            cy = content_top + avail_h // 2
            for row in payload["rings"]:
                color = AMBER if row["color"] == "amber" else LISTENING
                ring_rects.append(_blit_ring_block(surf, _paint_countdown_ring(d, row["fraction"], color, 0.0), x, cy, row))
                x += d + gap_r
    if payload["chips"]:
        wanted = {c["id"] for c in payload["chips"]}
        chip_timers = [t for t in TIMER_CHIPS if t.get("id") in wanted]
        rect = pygame.Rect(side, L.h - bot - chip_h, max(1, L.w - 2 * side), chip_h)
        draw_timer_chips(surf, rect, chip_timers, pinned)
        LAST_TIMER_RECT = rect
    else:
        LAST_TIMER_RECT = ring_rects[0].unionall(ring_rects[1:]) if len(ring_rects) > 1 else (ring_rects[0] if ring_rects else None)
    return payload


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


def draw_idle(surf, L, now, wx, colon_on=True, home_band=True):
    """The idle clock, date and weather. Returns the lowest y they use, which is where the bottom zone starts.
    home_band=False leaves out the home cards: the bottom zone is in use."""
    global LAST_TOPBAR_RECT, LAST_CARD_RECT, LAST_PORTRAIT_RECT, LAST_PORTRAIT_ARGS
    global LAST_WEATHER_RECT, LAST_OFFLINE_RECT, LAST_DATE_RECT, LAST_CLOCK_RECT
    global LAST_ENROLL_RECT, LAST_ENROLL_AVATAR, LAST_ENROLL_BUTTONS, LAST_ENROLL_STAGE
    global LAST_ENROLL_HINT, LAST_ENROLL_PROGRESS, LAST_ENROLL_HEADLINE
    LAST_TOPBAR_RECT = LAST_CARD_RECT = LAST_PORTRAIT_RECT = LAST_PORTRAIT_ARGS = None
    LAST_WEATHER_RECT = LAST_OFFLINE_RECT = LAST_DATE_RECT = LAST_CLOCK_RECT = None
    LAST_ENROLL_RECT = LAST_ENROLL_AVATAR = LAST_ENROLL_STAGE = None
    LAST_ENROLL_BUTTONS = []
    LAST_ENROLL_HINT = LAST_ENROLL_PROGRESS = LAST_ENROLL_HEADLINE = None
    surf.fill(BG)
    hhmm, ampm = clock_parts(now)
    top = L(70) + int(L.extra_y * 0.3)
    clock_px = L(300)  # design.md §13 refresh: idle clock 300 px @1080
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
    if home_band:
        draw_next_up(surf, L, now)
    draw_timer_strip(surf, L)
    draw_status_icons(surf, L)
    return max(r.bottom for r in (LAST_DATE_RECT, LAST_CLOCK_RECT, LAST_WEATHER_RECT, LAST_OFFLINE_RECT) if r)


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


def draw_voice_hint(surf, segments, x, y, px=36, max_w=None):
    """One station voice-hint style. Body font at px (callers pass L(36)).

    segments are (text, role): muted stays MUTED, phrase is TEXT inside curly quotes,
    text is TEXT with no quotes. Returns the row rect. A state change is not animated.
    """
    px = max(8, int(px))
    f = font("body", px)
    spans = [(t, c) for t, c in hint_spans(segments) if t]
    if not spans:
        return pygame.Rect(int(x), int(y), 0, f.get_height())
    if max_w is not None:
        widths = [f.size(t)[0] for t, _c in spans]
        total = sum(widths)
        limit = int(max_w)
        if total > limit and spans:
            idx = len(spans) - 1
            for i in range(len(spans) - 1, -1, -1):
                if spans[i][1] == TEXT:
                    idx = i
                    break
            room = max(1, widths[idx] - (total - limit))
            spans[idx] = (ellipsize(f, spans[idx][0], room), spans[idx][1])
    cx, top = int(x), int(y)
    rects = []
    for text, color in spans:
        img = f.render(text, True, color)
        surf.blit(img, (cx, top))
        rects.append(pygame.Rect(cx, top, img.get_width(), img.get_height()))
        cx += img.get_width()
    return rects[0].unionall(rects[1:]) if len(rects) > 1 else rects[0]


def _line(surf, kind, px, text, color, width):
    f = font(kind, px)
    shown = text if f.size(text)[0] <= width else ellipsize(f, text, width)
    return f.render(shown, True, color)


def _fit_px(kind, text, max_px, width, floor=16):
    """Largest size at or under max_px whose text fits width. Never zero."""
    px = max(floor, int(max_px))
    floor = max(8, int(floor))
    while px > floor and font(kind, px).size(str(text))[0] > width:
        px = max(floor, int(px * 0.88))
    return px


def monogram_shield(abbr, px):
    """A colored shield with the abbreviation. Never red: red is mic-off and stock-down.

    Drawn in the square the score layout reserved. A cached logo replaces this;
    the station never ships a real team mark.
    """
    colors = (LISTENING, THINKING, SPEAKING, AMBER)
    color = colors[hashlib.sha256(str(abbr).encode()).digest()[0] % 4]
    box = pygame.Surface((px, px), pygame.SRCALPHA)
    w = h = max(8, int(px))
    tip = [(int(x), int(y)) for x, y in (
        (w * 0.50, h * 0.04), (w * 0.92, h * 0.16), (w * 0.92, h * 0.46),
        (w * 0.50, h * 0.96), (w * 0.08, h * 0.46), (w * 0.08, h * 0.16))]
    pygame.draw.polygon(box, color, tip)
    band = [(int(x), int(y)) for x, y in (
        (w * 0.08, h * 0.40), (w * 0.50, h * 0.56), (w * 0.92, h * 0.40),
        (w * 0.92, h * 0.50), (w * 0.50, h * 0.66), (w * 0.08, h * 0.50))]
    pygame.draw.polygon(box, BG, band)
    label = "".join(ch for ch in str(abbr) if ch.isalnum())[:3] or "?"
    img = font("display", max(12, int(px * 0.26))).render(label, True, BG)
    box.blit(img, ((px - img.get_width()) // 2, int(px * 0.18)))
    return box


def _loaded_logo(team, px):
    """A cached logo scaled to px, or None when there is nothing to draw.

    A missing file, or a file pygame cannot read, is None. This does not fetch
    and does not leave a broken image.
    """
    lid = team.get("logo_id") if isinstance(team, dict) else None
    path = logo_cache().path_for(lid) if isinstance(lid, str) else None
    if not path or not os.path.isfile(path):
        return None
    try:
        img = pygame.image.load(path)
        try:
            img = img.convert_alpha()
        except pygame.error:
            pass
        return pygame.transform.smoothscale(img, (px, px))
    except Exception:
        return None


def team_mark(team, px):
    """A cached logo, or a monogram. This does not fetch.

    A logo id with no file yet (the download is still in the background) is a monogram.
    The score card uses team_face instead, so a missing logo does not reserve a gap.

    convert_alpha needs a display mode. Demo renders and tests often have none,
    and a decoded logo should still show rather than falling back to a monogram.
    """
    img = _loaded_logo(team, px)
    if img is not None:
        return img
    abbr = team.get("abbr") if isinstance(team, dict) else "?"
    return monogram_shield(abbr or "?", px)


def team_face(team, px):
    """What sits above the team name: a logo, an abbreviation badge, or nothing.

    No logo_url (no logo id and no badge) is nothing, so the name is not pushed down.
    A file that fails to load is nothing too, unless the station marked logo_badge
    because the fetch was not ready. Never a broken image and never an empty gap.
    """
    img = _loaded_logo(team, px)
    if img is not None:
        return img
    if isinstance(team, dict) and team.get("logo_badge") is True:
        return monogram_shield(team.get("abbr") or "?", px)
    return None


def sports_stack(top, bottom, face_h, name_h, score_h, gap):
    """Y positions for one team column, centered in [top, bottom].

    face_h 0 means no logo and no reserved gap. Returns (face_y or None, name_y, score_y).
    """
    face_h = int(face_h or 0)
    gap = int(gap)
    block = int(name_h) + gap + int(score_h)
    if face_h > 0:
        block += face_h + gap
    span = max(0, int(bottom) - int(top))
    y = int(top) + max(0, (span - block) // 2)
    face_y = None
    if face_h > 0:
        face_y = y
        y += face_h + gap
    return face_y, y, y + int(name_h) + gap


def _trend(surf, x, y, size, trend):
    if trend == "up":
        pygame.draw.polygon(surf, GREEN, [(x, y + size), (x + size // 2, y), (x + size, y + size)])
        return GREEN
    if trend == "down":
        pygame.draw.polygon(surf, RED, [(x, y), (x + size // 2, y + size), (x + size, y)])
        return RED
    return None


def draw_home_band(surf, L, rows):
    """Up to three home cards under the date and weather, above the timer strip. No-op if there is no room."""
    if not rows:
        return
    floor_y = content_limit(L)
    date_bottom = LAST_DATE_RECT.bottom if LAST_DATE_RECT else 0
    weather_bottom = LAST_WEATHER_RECT.bottom if LAST_WEATHER_RECT else 0
    top = max(date_bottom, weather_bottom, int(L.h * 0.58)) + L(16)
    if floor_y - top < L(80):
        top = max(date_bottom, weather_bottom) + L(16)
    if floor_y - top < L(80):
        return
    gap = L(16)
    n = min(3, len(rows))
    width = (L.w - 2 * L.m - gap * (n - 1)) // n
    height = min(L(280), floor_y - top)
    for i, row in enumerate(rows[:n]):
        rect = pygame.Rect(L.m + i * (width + gap), top, width, height)
        pygame.draw.rect(surf, SURFACE, rect, border_radius=L(20))
        card = row["card"]
        y = rect.top + L(16)
        title = _line(surf, "display", L(34), card["title"].upper(), MUTED, rect.w - L(48))
        surf.blit(title, (rect.left + L(28), y))
        y += title.get_height() + L(16)
        for item in card["items"]:
            trend = item.get("trend")
            color = GREEN if trend == "up" else RED if trend == "down" else TEXT
            arrow = L(22)
            label = _line(surf, "body", L(36), item["label"], MUTED, max(40, rect.w // 2 - L(16)))
            value = _line(surf, "display", L(48), item["value"], color, max(40, rect.w // 2 - arrow - L(24)))
            row_h = max(label.get_height(), value.get_height())
            if y + row_h > rect.bottom - L(8):
                break
            surf.blit(label, (rect.left + L(28), y + max(0, (value.get_height() - label.get_height()) // 2)))
            vx = rect.right - L(28) - value.get_width()
            if trend in ("up", "down"):
                _trend(surf, vx - arrow - L(8), y + max(0, (value.get_height() - arrow) // 2), arrow, trend)
            surf.blit(value, (vx, y))
            y += row_h + L(12)


def draw_next_up(surf, L, now):
    """Home cards in the lower band. Empty deck: nothing, so the idle clock stays where it is."""
    draw_home_band(surf, L, deck().home_row(time.time()))


def recipe_scale(L):
    """The recipe tile is measured at 2560×1706. Other screens scale that panel to fit."""
    return min(L.w / 2560.0, L.h / 1706.0)


def recipe_px(L, px):
    return max(1, int(round(float(px) * recipe_scale(L))))


def recipe_rect(L, x, y, w, h):
    s = recipe_scale(L)
    return pygame.Rect(int(round(x * s)), int(round(y * s)), max(1, int(round(w * s))), max(1, int(round(h * s))))


def recipe_frame(L):
    """Reserved boxes from the recipe-tile SPEC. Exact at 2560×1706."""
    return {
        "title": recipe_rect(L, 128, 56, 620, 104),
        "banner": recipe_rect(L, 780, 56, 1130, 104),
        "clock": recipe_rect(L, 1930, 52, 410, 112),
        "status": recipe_rect(L, 2440, 20, 64, 64),
        "ingredients": recipe_rect(L, 128, 216, 560, 1246),
        "step": recipe_rect(L, 712, 216, 1168, 1246),
        "rail": recipe_rect(L, 1904, 216, 528, 1246),
        "slots": [recipe_rect(L, 1904, y, 528, 400) for y in (216, 639, 1062)],
        "hint": recipe_rect(L, 128, 1494, 2304, 84),
        "step_text": recipe_rect(L, 776, 520, 1040, 828),
    }


def _bezier_ease(u):
    """One sample of cubic-bezier(.2, 0, 0, 1). Used only to fill the lookup table."""
    def bez(t, a, b):
        omt = 1.0 - t
        return 3 * omt * omt * t * a + 3 * omt * t * t * b + t * t * t

    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2.0
        if bez(mid, 0.2, 0.0) < u:
            lo = mid
        else:
            hi = mid
    return bez((lo + hi) / 2.0, 0.0, 1.0)


_EASE = tuple(_bezier_ease(i / 32.0) for i in range(33))


def ease_out(u):
    """cubic-bezier(.2, 0, 0, 1), from the table above. 0 at the start, 1 at the end."""
    if u <= 0:
        return 0.0
    if u >= 1:
        return 1.0
    x = float(u) * 32.0
    i = int(x)
    return _EASE[i] + (_EASE[i + 1] - _EASE[i]) * (x - i)


def rail_translate(before, after, elapsed_ms, reduced, slot_ys):
    """Pixels to add to each timer's new slot y. One change is one slide, clipped by the caller to the rail.

    before/after map id -> slot index. Reduced motion, or 240 ms and later, is already at the new slot.
    """
    ids = list(after or {})
    if reduced or elapsed_ms >= 240:
        return {i: 0 for i in ids}
    remain = 1.0 - ease_out(max(0.0, min(1.0, float(elapsed_ms) / 240.0)))
    out = {}
    for i, slot in (after or {}).items():
        if i in (before or {}) and before[i] != slot:
            out[i] = (slot_ys[before[i]] - slot_ys[slot]) * remain
        else:
            out[i] = 0
    return out


def recipe_name(label):
    """First letter only, so a dish stays "Garlic bread" rather than a title-cased "Garlic Bread"."""
    text = " ".join(str(label or "timer").split())[:24]
    return (text[:1].upper() + text[1:]) if text else "Timer"


def slot_title(timer):
    """Rail name. A plain "Timer" with a whole-minute length is "Timer · 5 min"; "Timer 2" stays "Timer 2"."""
    label = str((timer or {}).get("label") or "timer")
    titled = recipe_name(label)
    dur = (timer or {}).get("duration")
    if label.strip().lower() == "timer" and isinstance(dur, (int, float)) and not isinstance(dur, bool):
        sec = int(dur)
        if 60 <= sec <= 86400 and sec % 60 == 0:
            return f"Timer · {sec // 60} min"
    return titled


def timer_aria(title, seconds):
    """Spoken length for the slot, e.g. "Orzo, 7 minutes 42 seconds left"."""
    sec = max(0, int(round(float(seconds))))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour" + ("" if h == 1 else "s"))
    if m or h:
        parts.append(f"{m} minute" + ("" if m == 1 else "s"))
    parts.append(f"{s} second" + ("" if s == 1 else "s"))
    return f"{title}, {' '.join(parts)} left"


def banner_copy(names):
    """Ringing banner. One name, two names, or the first two plus "+ N more". Names are already capped."""
    names = [str(n) for n in (names or []) if str(n).strip()]
    if not names:
        return ""
    if len(names) == 1:
        return f"{names[0]} timer is done"
    if len(names) == 2:
        return f"{names[0]} and {names[1]} are done"
    return f"{names[0]}, {names[1]} + {len(names) - 2} more"


def banner_parts(names, font, limit=350):
    """Cap each name at `limit` px. The "+ N more" tail is never ellipsized."""
    shown = []
    for name in names or []:
        text = str(name)
        shown.append(text if font.size(text)[0] <= limit else ellipsize(font, text, limit))
    suffix = f"+ {len(shown) - 2} more" if len(shown) >= 3 else ""
    return {"names": shown, "text": banner_copy(shown), "suffix": suffix}


def _timer_fraction(timer, left):
    dur = (timer or {}).get("duration")
    if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0:
        return max(0.0, min(1.0, float(left) / float(dur)))
    return 1.0 if left > 0 else 0.0


def recipe_rail(timers, epoch, order=None):
    """Three fixed slots. Oldest first. Past three, slot 3 is "+N more" (soonest first, 3 rows).

    A listed timer that is ringing takes slot 3 with the ringing treatment. Banner names are every
    ringing timer in end order (earliest deadline first).
    """
    rows = [t for t in (timers or []) if isinstance(t, dict) and t.get("id")]
    by_id = {}
    for t in rows:
        by_id[t["id"]] = t
    ids = []
    for i in order or []:
        if i in by_id and i not in ids:
            ids.append(i)
    for t in rows:
        if t["id"] not in ids:
            ids.append(t["id"])

    def left_of(t):
        return float(t.get("ends_at_epoch", 0)) - float(epoch)

    def is_ringing(t):
        return _shown_ringing(t, left_of(t))

    def end_key(tid):
        return (float(by_id[tid].get("ends_at_epoch", 0)), ids.index(tid))

    def ring_slot(tid):
        t = by_id[tid]
        ringing = is_ringing(t)
        title = slot_title(t)
        left = 0.0 if ringing else max(0.0, left_of(t))
        return {
            "kind": "ringing" if ringing else "ring",
            "id": tid,
            "title": title,
            "text": "0:00" if ringing else fmt_timer_clock(left),
            # TODO: a recipe timer linked to its step should say "From step 3" here.
            # Skipped in 0.13.11 with the spoken offer and step-text time detection.
            "source": "Set by voice",
            "done": f"{title} is done",
            "fraction": 1.0 if ringing else round(_timer_fraction(t, left), 4),
            "aria": timer_aria(title, 0 if ringing else left),
        }

    slots = [{"kind": "empty"}, {"kind": "empty"}, {"kind": "empty"}]
    if len(ids) <= 3:
        for i, tid in enumerate(ids):
            slots[i] = ring_slot(tid)
    else:
        slots[0] = ring_slot(ids[0])
        slots[1] = ring_slot(ids[1])
        rest = ids[2:]
        ringing_rest = [tid for tid in rest if is_ringing(by_id[tid])]
        if ringing_rest:
            slots[2] = ring_slot(min(ringing_rest, key=end_key))
        else:
            soonest = sorted(rest, key=end_key)[:3]
            slots[2] = {
                "kind": "more",
                "n": len(rest),
                "label": f"+{len(rest)} more",
                "rows": [{"id": tid, "title": slot_title(by_id[tid]), "text": fmt_timer_clock(max(0.0, left_of(by_id[tid])))}
                         for tid in soonest],
            }
    ringing_ids = sorted((tid for tid in ids if is_ringing(by_id[tid])), key=end_key)
    positions = {}
    for i, slot in enumerate(slots):
        if slot.get("kind") in ("ring", "ringing") and slot.get("id"):
            positions[slot["id"]] = i
    if len(ids) > 3:
        for tid in ids[2:]:
            positions.setdefault(tid, 2)
    return {"order": ids, "slots": slots, "banner_names": [recipe_name(by_id[tid].get("label")) for tid in ringing_ids],
            "positions": positions}


RECIPE_RAIL_ORDER = []
RECIPE_FEED_OPEN = False
_RAIL_SLIDE = {"sig": None, "t0": 0.0, "before": {}, "positions": None}
# Recipe paint results. Same LAST_* pattern as the other screens, kept in one block.
LAST_RECIPE_FRAME = None
LAST_RECIPE_RAIL = None
LAST_RECIPE_COPY = []
LAST_BANNER_TEXT = ""
SLOT_STOP_SEGMENTS = (("Say ", "muted"), ("stop", "phrase"))


def arm_recipe_screen(showing):
    """Drop rail order and the re-pack slide when a recipe opens or closes.

    Both are process globals. Left alone, the next recipe's first frame slides in from the previous one.
    """
    global RECIPE_FEED_OPEN, RECIPE_RAIL_ORDER, _RAIL_SLIDE
    showing = bool(showing)
    if showing == RECIPE_FEED_OPEN:
        return
    RECIPE_RAIL_ORDER = []
    _RAIL_SLIDE = {"sig": None, "t0": 0.0, "before": {}, "positions": None}
    RECIPE_FEED_OPEN = showing


def rail_slide_running(now=None):
    """True during the 240 ms re-pack, and only when something actually changed slot."""
    before = _RAIL_SLIDE.get("before") or {}
    positions = _RAIL_SLIDE.get("positions")
    if positions is None or before == positions or reduce_motion():
        return False
    t0 = _RAIL_SLIDE.get("t0") or 0.0
    elapsed = ((time.monotonic() if now is None else now) - t0) * 1000.0
    return elapsed < 240.0


def feed_frame_gap(sliding, ringing, reduced):
    """Seconds between repaints. A slide or a ringing pulse needs a frame inside the 240 ms move."""
    if reduced:
        return 0.9
    if sliding or ringing:
        return 0.12
    return 0.9


def sync_recipe_order(timers):
    """Keep rail order across list posts. New ids append; dismissed ids drop and the rest slide up."""
    global RECIPE_RAIL_ORDER
    by = {t.get("id"): t for t in timers or [] if isinstance(t, dict) and t.get("id")}
    have, seen = [], set()
    for i in RECIPE_RAIL_ORDER:
        if i in by and i not in seen:
            have.append(i)
            seen.add(i)
    for t in timers or []:
        i = t.get("id") if isinstance(t, dict) else None
        if i and i not in seen:
            have.append(i)
            seen.add(i)
    RECIPE_RAIL_ORDER = have
    return have


def _slide_offsets(positions, reduced, slot_ys):
    """Start one 240 ms slide when the slot map changes. Reduced motion skips it."""
    global _RAIL_SLIDE
    sig = tuple(sorted((positions or {}).items()))
    if _RAIL_SLIDE.get("sig") != sig:
        before = _RAIL_SLIDE.get("positions") if _RAIL_SLIDE.get("positions") is not None else positions
        _RAIL_SLIDE = {"sig": sig, "t0": time.monotonic(), "before": before or {}, "positions": dict(positions or {})}
    elapsed = (time.monotonic() - _RAIL_SLIDE["t0"]) * 1000.0
    return rail_translate(_RAIL_SLIDE["before"], positions or {}, elapsed, reduced, slot_ys)


def _dashed_rect(surf, color, rect, width, dash, gap):
    x0, y0, w, h = rect.x, rect.y, rect.w, rect.h
    spans = (
        (x0, y0, x0 + w, y0, 1, 0),
        (x0 + w, y0, x0 + w, y0 + h, 0, 1),
        (x0 + w, y0 + h, x0, y0 + h, -1, 0),
        (x0, y0 + h, x0, y0, 0, -1),
    )
    for x, y, x1, y1, dx, dy in spans:
        left = abs(x1 - x) + abs(y1 - y)
        pos = 0
        while pos < left:
            run = min(dash, left - pos)
            a = (int(x + dx * pos), int(y + dy * pos))
            b = (int(x + dx * (pos + run)), int(y + dy * (pos + run)))
            pygame.draw.line(surf, color, a, b, width)
            pos += dash + gap


def _clock_icon(px, color):
    box = pygame.Surface((px, px), pygame.SRCALPHA)
    c = px // 2
    sw = max(2, px // 10)
    pygame.draw.circle(box, color, (c, c), max(2, c - 1), sw)
    pygame.draw.line(box, color, (c, c), (c, int(px * 0.28)), sw)
    pygame.draw.line(box, color, (c, c), (int(px * 0.72), c), sw)
    return box


def _bell_icon(px, color):
    box = pygame.Surface((px, px), pygame.SRCALPHA)
    pygame.draw.ellipse(box, color, (int(px * 0.22), int(px * 0.16), int(px * 0.56), int(px * 0.50)))
    pygame.draw.rect(box, color, (int(px * 0.16), int(px * 0.48), int(px * 0.68), max(2, int(px * 0.12))))
    pygame.draw.circle(box, color, (px // 2, int(px * 0.74)), max(2, int(px * 0.08)))
    return box


def _static_outline(surf, rect, color, offset, thick):
    """6 px outline sitting `offset` px outside rect. No animation."""
    outer = rect.inflate(2 * (offset + thick), 2 * (offset + thick))
    inner = rect.inflate(2 * offset, 2 * offset)
    bars = (
        pygame.Rect(outer.left, outer.top, outer.w, thick),
        pygame.Rect(outer.left, inner.bottom, outer.w, thick),
        pygame.Rect(outer.left, inner.top, thick, max(1, inner.h)),
        pygame.Rect(inner.right, inner.top, thick, max(1, inner.h)),
    )
    for bar in bars:
        if bar.w > 0 and bar.h > 0:
            surf.fill(color, bar)


def _pulse_glow(surf, rect, pulse):
    """1 Hz amber glow, 0→18 px at 33% opacity, outside the slot. pulse 0 draws nothing."""
    if pulse <= 0.02:
        return
    spread = max(1, int(round(18 * pulse)))
    shade = pygame.Surface((rect.w + 2 * spread, rect.h + 2 * spread), pygame.SRCALPHA)
    alpha = int(round(255 * 0.33 * pulse))
    rad = min(rect.w, rect.h) // 2
    pygame.draw.rect(shade, (AMBER.r, AMBER.g, AMBER.b, alpha), shade.get_rect(), border_radius=rad)
    surf.blit(shade, (rect.left - spread, rect.top - spread))


def _ringing_done_line(title, width, face):
    """'<Name> is done', with the name ellipsized so the whole line fits the slot. ' is done' stays whole."""
    suffix = " is done"
    if face.size(title + suffix)[0] <= width:
        return title + suffix
    return ellipsize(face, title, max(1, width - face.size(suffix)[0])) + suffix


def fitted_step_lines(lines, face, width, height, gap):
    """Lines that fit in the step box, whole lines only. A cut-off step ellipsizes the last one shown."""
    line_h = face.get_height()
    if line_h <= 0 or height < line_h or not lines:
        return []
    n = 0
    while n < len(lines) and (n + 1) * line_h + n * gap <= height:
        n += 1
    shown = list(lines[:n])
    if n < len(lines) and shown:
        shown[-1] = ellipsize(face, shown[-1], width)
    return shown


def _paint_recipe_slot(surf, L, rect, slot, index, epoch, reduced):
    """One rail slot. Empty slots stay dashed; only slot 1 says "No timers"."""
    global LAST_RECIPE_COPY
    rad = max(4, recipe_px(L, 37))
    if slot.get("kind") == "empty":
        _dashed_rect(surf, RULE2, rect, max(1, recipe_px(L, 3)), recipe_px(L, 14), recipe_px(L, 10))
        if index == 0:
            slot["label"] = "No timers"
            LAST_RECIPE_COPY.append("No timers")
            icon = _clock_icon(recipe_px(L, 64), RECIPE_FAINT)
            label = font("body", recipe_px(L, 40)).render("No timers", True, RECIPE_FAINT)
            block = icon.get_height() + recipe_px(L, 12) + label.get_height()
            y = rect.top + max(0, (rect.h - block) // 2)
            surf.blit(icon, (rect.centerx - icon.get_width() // 2, y))
            surf.blit(label, (rect.centerx - label.get_width() // 2, y + icon.get_height() + recipe_px(L, 12)))
        return
    if slot.get("kind") == "more":
        pygame.draw.rect(surf, SURFACE, rect, border_radius=rad)
        pygame.draw.rect(surf, RULE, rect, max(1, recipe_px(L, 3)), border_radius=rad)
        y = rect.top + recipe_px(L, 34)
        head = font("display", recipe_px(L, 52)).render(slot.get("label") or "", True, TEXT)
        LAST_RECIPE_COPY.append(slot.get("label") or "")
        surf.blit(head, (rect.left + recipe_px(L, 28), y))
        y += head.get_height() + recipe_px(L, 16)
        name_f = font("body", recipe_px(L, 36))
        for row in slot.get("rows") or []:
            line = name_f.render(f"{row['title']}  {row['text']}", True, MUTED)
            if y + line.get_height() > rect.bottom - recipe_px(L, 16):
                break
            surf.blit(line, (rect.left + recipe_px(L, 28), y))
            y += line.get_height() + recipe_px(L, 8)
        return
    ringing = slot.get("kind") == "ringing"
    pulse = 0.0 if reduced else _ring_pulse(epoch if epoch is not None else time.time())
    if ringing and not reduced:
        _pulse_glow(surf, rect, pulse)
    if ringing:
        pygame.draw.rect(surf, ALERT_FILL, rect, border_radius=rad)
        pygame.draw.rect(surf, AMBER, rect, max(1, recipe_px(L, 8)), border_radius=rad)
        if reduced:
            _static_outline(surf, rect, AMBER, recipe_px(L, 10), recipe_px(L, 6))
        d = recipe_px(L, 200)
        ring, _pad = _paint_countdown_ring(d, 1.0, AMBER, 0.0 if reduced else pulse)
        ring_at = ring.get_rect(midtop=(rect.centerx, rect.top + recipe_px(L, 16)))
        surf.blit(ring, ring_at)
        bell = _bell_icon(recipe_px(L, 72), AMBER)
        surf.blit(bell, bell.get_rect(center=ring_at.center))
        done_px = recipe_px(L, 46)
        done_font = font("display", done_px)
        done_text = _ringing_done_line(slot.get("title") or "", rect.w - recipe_px(L, 48), done_font)
        done = done_font.render(done_text, True, AMBER)
        LAST_RECIPE_COPY.append(done_text)
        surf.blit(done, (rect.centerx - done.get_width() // 2, ring_at.bottom + recipe_px(L, 8)))
        hint_px = recipe_px(L, 48)
        draw_voice_hint(surf, SLOT_STOP_SEGMENTS, rect.left + recipe_px(L, 28),
                        rect.bottom - recipe_px(L, 28) - font("body", hint_px).get_height(),
                        px=hint_px, max_w=rect.w - recipe_px(L, 56))
        return
    pygame.draw.rect(surf, SURFACE, rect, border_radius=rad)
    pygame.draw.rect(surf, RULE, rect, max(1, recipe_px(L, 3)), border_radius=rad)
    d = recipe_px(L, 212)
    ring, _pad = _paint_countdown_ring(d, slot.get("fraction") or 0, LISTENING, 0.0)
    ring_at = ring.get_rect(midtop=(rect.centerx, rect.top + recipe_px(L, 28)))
    prev = surf.get_clip()
    surf.set_clip(rect.clip(prev if prev else rect))
    surf.blit(ring, ring_at)
    title = font("display", recipe_px(L, 40)).render(slot.get("title") or "", True, TEXT)
    digits = font("display", recipe_px(L, 64)).render(slot.get("text") or "", True, TEXT)
    source = font("body", recipe_px(L, 32)).render(slot.get("source") or "", True, MUTED)
    LAST_RECIPE_COPY.append(slot.get("source") or "")
    y = ring_at.bottom + recipe_px(L, 8)
    for img in (title, digits, source):
        if y + img.get_height() > rect.bottom - recipe_px(L, 12):
            break
        surf.blit(img, (rect.centerx - img.get_width() // 2, y))
        y += img.get_height() + recipe_px(L, 4)
    surf.set_clip(prev)


def _paint_recipe_rail(surf, L, frame, epoch=None):
    """Draw the three slots. A re-pack slides inside the rail only."""
    global LAST_RECIPE_RAIL
    pinned = _pinned_epoch(epoch)
    now = pinned if pinned is not None else time.time()
    order = sync_recipe_order(TIMER_CHIPS)
    rail = recipe_rail(TIMER_CHIPS, now, order)
    LAST_RECIPE_RAIL = rail
    reduced = reduce_motion()
    ys = [frame["slots"][i].y for i in range(3)]
    offsets = _slide_offsets(rail["positions"], reduced, ys)
    prev = surf.get_clip()
    surf.set_clip(frame["rail"])
    for i, slot in enumerate(rail["slots"]):
        dy = 0
        sid = slot.get("id")
        if sid and sid in offsets:
            dy = int(round(offsets[sid]))
        body = frame["slots"][i].move(0, dy)
        # The outline and glow sit outside the slot, so they are drawn unclipped below.
        if slot.get("kind") != "ringing":
            _paint_recipe_slot(surf, L, body, slot, i, now, reduced)
    surf.set_clip(prev)
    for i, slot in enumerate(rail["slots"]):
        if slot.get("kind") != "ringing":
            continue
        dy = 0
        sid = slot.get("id")
        if sid and sid in offsets:
            dy = int(round(offsets[sid]))
        _paint_recipe_slot(surf, L, frame["slots"][i].move(0, dy), slot, i, now, reduced)


def _paint_recipe_banner(surf, L, rect, parts):
    """Amber pill in the banner slot. The bell is 64 px; names were already capped."""
    if not parts or not parts.get("text"):
        return
    rad = rect.h // 2
    pygame.draw.rect(surf, AMBER, rect, border_radius=rad)
    bell = _bell_icon(recipe_px(L, 64), AMBER_INK)
    surf.blit(bell, (rect.left + recipe_px(L, 40), rect.centery - bell.get_height() // 2))
    f = font("display", max(14, recipe_px(L, 54)))
    text = parts["text"]
    room = rect.w - recipe_px(L, 64) - recipe_px(L, 24) - recipe_px(L, 80)
    # The tail "+ N more" stays whole. Only a too-long single line would be cut, and names are pre-capped.
    img = f.render(text, True, AMBER_INK)
    if img.get_width() > room and parts.get("suffix") and text.endswith(parts["suffix"]):
        head = text[: -len(parts["suffix"])].rstrip()
        head_img = f.render(ellipsize(f, head, max(1, room - f.size(" " + parts["suffix"])[0])), True, AMBER_INK)
        tail_img = f.render(" " + parts["suffix"], True, AMBER_INK)
        x = rect.left + recipe_px(L, 40) + bell.get_width() + recipe_px(L, 24)
        y = rect.centery - head_img.get_height() // 2
        surf.blit(head_img, (x, y))
        surf.blit(tail_img, (x + head_img.get_width(), y))
        return
    x = rect.left + recipe_px(L, 40) + bell.get_width() + recipe_px(L, 24)
    surf.blit(img, (x, rect.centery - img.get_height() // 2))


def draw_recipe(surf, L, card):
    """Ingredients, the current step, the voice-hint row, and the timer rail.

    Timer chips used to shrink this card through content_limit. The rail is reserved, so the step card
    stays the same height with or without timers."""
    global LAST_RECIPE_FRAME, LAST_RECIPE_COPY
    frame = recipe_frame(L)
    LAST_RECIPE_FRAME = frame
    LAST_RECIPE_COPY = []
    rad = max(4, recipe_px(L, 37))
    border = max(1, recipe_px(L, 3))
    ing, stp = frame["ingredients"], frame["step"]
    for rect in (ing, stp):
        pygame.draw.rect(surf, SURFACE, rect, border_radius=rad)
        pygame.draw.rect(surf, RULE, rect, border, border_radius=rad)
    pad = recipe_px(L, 40)
    x, y = ing.left + pad, ing.top + pad
    cap = _line(surf, "display", recipe_px(L, 28), "INGREDIENTS", MUTED, ing.w - 2 * pad)
    surf.blit(cap, (x, y))
    y += cap.get_height() + recipe_px(L, 8)
    meta = _line(surf, "body", recipe_px(L, 28), f"Serves {card['servings']}", RECIPE_FAINT, ing.w - 2 * pad)
    surf.blit(meta, (x, y))
    y += meta.get_height() + recipe_px(L, 16)
    prev = surf.get_clip()
    surf.set_clip(ing)
    for item in card["ingredients"]:
        if y > ing.bottom - recipe_px(L, 28):
            break
        line = _line(surf, "body", recipe_px(L, 42), item, TEXT, ing.w - 2 * pad)
        surf.blit(line, (x, y))
        y += line.get_height() + recipe_px(L, 10)
    surf.set_clip(prev)
    sx = stp.left + recipe_px(L, 64)
    sy = stp.top + recipe_px(L, 56)
    step_n, total = card["step"], len(card["steps"])
    col = stp.w - recipe_px(L, 128)
    label = _line(surf, "display", recipe_px(L, 52), f"Step {step_n} of {total}", LISTENING, col)
    surf.blit(label, (sx, sy))
    sy += label.get_height() + recipe_px(L, 18)
    track = pygame.Rect(sx, sy, col, max(6, recipe_px(L, 12)))
    pygame.draw.rect(surf, SURFACE2, track, border_radius=recipe_px(L, 8))
    fill = max(track.h, int(track.w * step_n / max(1, total)))
    pygame.draw.rect(surf, LISTENING, (track.left, track.top, min(fill, track.w), track.h), border_radius=recipe_px(L, 8))
    text_box = pygame.Rect(sx, track.bottom + recipe_px(L, 36), col, recipe_px(L, 828))
    text_box = text_box.clip(stp)
    frame["step_text"] = text_box
    body_px = recipe_px(L, 112)
    f = font("display", body_px)
    gap = recipe_px(L, 8)
    lines = fitted_step_lines(wrap(f, card["steps"][step_n - 1], col), f, col, text_box.h, gap)
    surf.set_clip(text_box)
    ty = text_box.top
    for line in lines:
        img = f.render(line, True, TEXT)
        if ty + img.get_height() > text_box.bottom:
            break
        surf.blit(img, (sx, ty))
        ty += img.get_height() + gap
    surf.set_clip(prev)
    hint = frame["hint"]
    hint_px = recipe_px(L, 48)
    hy = hint.top + max(0, (hint.h - font("body", hint_px).get_height()) // 2)
    draw_voice_hint(surf, RECIPE_HINT_SEGMENTS, hint.left, hy, px=hint_px, max_w=hint.w)
    _paint_recipe_rail(surf, L, frame)


def _card_chrome(surf, L, rect):
    pygame.draw.rect(surf, SURFACE, rect, border_radius=L(28))
    pygame.draw.rect(surf, RULE, rect, max(2, L(2)), border_radius=L(28))


def draw_sports(surf, L, card, rect):
    """Score tile in the bottom zone: header row, the two teams, and the next game as a band inside the card.
    Shields or cached logos only; no fetch while drawing."""
    _card_chrome(surf, L, rect)
    games = card["schedule"][:4]
    pad = L(48)
    strip_h = L(112) if games and rect.h >= L(300) else 0
    head = rect.top + L(30)
    live = card["status"] == "live"
    label = "LIVE" if live else card["status"].upper()
    st = _line(surf, "display", L(44), label, LISTENING if live else MUTED, max(40, rect.w // 3))
    if live:
        pygame.draw.circle(surf, LISTENING, (rect.left + pad + L(8), head + st.get_height() // 2), max(4, L(10)))
        surf.blit(st, (rect.left + pad + L(28), head))
    else:
        surf.blit(st, (rect.left + pad, head))
    period = _line(surf, "display", L(44), card["period"], MUTED, max(40, rect.w // 3))
    surf.blit(period, (rect.right - pad - period.get_width(), head))
    # MUTED, not FAINT: FAINT on SURFACE is 3.24:1 and fails AA for small text (Noor's contrast table).
    league = _line(surf, "body", L(33), card["league"], MUTED, max(40, rect.w // 4))
    surf.blit(league, (rect.centerx - league.get_width() // 2, head + max(0, (st.get_height() - league.get_height()) // 2)))
    strip_top = rect.bottom - strip_h
    col_top = head + st.get_height() + L(12)
    col_bot = (strip_top if strip_h else rect.bottom) - L(16)
    col_w = max(40, rect.w // 2 - L(72))
    avail = max(L(48), col_bot - col_top)
    mark = min(L(110), max(L(40), avail // 3))
    name_px = min(L(48), max(L(22), avail // 6))
    score_px = min(
        _fit_px("display", card["home_score"], L(180), col_w, L(36)),
        _fit_px("display", card["away_score"], L(180), col_w, L(36)),
        max(L(36), avail * 2 // 3))
    gap = L(8)
    score_mids = []

    def paint_side(team, score, cx):
        face = team_face(team, mark)
        name = _line(surf, "display", name_px, team["name"], TEXT, col_w)
        sc = font("display", score_px).render(str(score), True, TEXT)
        face_h = face.get_height() if face is not None else 0
        face_y, name_y, score_y = sports_stack(col_top, col_bot, face_h, name.get_height(), sc.get_height(), gap)
        if face is not None and face_y is not None:
            surf.blit(face, (cx - face.get_width() // 2, face_y))
        surf.blit(name, (cx - name.get_width() // 2, name_y))
        surf.blit(sc, (cx - sc.get_width() // 2, score_y))
        score_mids.append(score_y + sc.get_height() // 2)

    paint_side(card["home"], card["home_score"], rect.left + rect.w // 4)
    paint_side(card["away"], card["away_score"], rect.right - rect.w // 4)
    dash = font("display", min(L(60), max(16, score_px // 3))).render("–", True, MUTED)
    mid = sum(score_mids) // len(score_mids) if score_mids else (col_top + col_bot) // 2
    surf.blit(dash, (rect.centerx - dash.get_width() // 2, mid - dash.get_height() // 2))
    if strip_h:
        pygame.draw.line(surf, RULE, (rect.left + max(2, L(2)), strip_top), (rect.right - max(2, L(2)) - 1, strip_top), max(2, L(2)))
        cap = _line(surf, "display", L(28), "NEXT GAME", MUTED, L(240))
        game = games[0]
        when = _line(surf, "display", L(44), game["when"], LISTENING, max(40, rect.w // 4))
        label_w = max(40, rect.w - cap.get_width() - when.get_width() - L(180))
        name = _line(surf, "display", L(44), game["label"], TEXT, label_w)
        row_y = strip_top + (strip_h - max(cap.get_height(), name.get_height(), when.get_height())) // 2
        surf.blit(cap, (rect.left + pad, row_y + (name.get_height() - cap.get_height()) // 2))
        surf.blit(name, (rect.left + pad + cap.get_width() + L(32), row_y))
        surf.blit(when, (rect.right - pad - when.get_width(), row_y))
    elif games:
        line = _line(surf, "body", L(28), f"{games[0]['when']}  {games[0]['label']}", MUTED, rect.w - 2 * pad)
        surf.blit(line, (rect.left + pad, rect.bottom - L(16) - line.get_height()))


def friendly_date(iso):
    """YYYY-MM-DD as a weekday and month day. An impossible day stays the raw date."""
    try:
        parsed = time.strptime(iso, "%Y-%m-%d")
    except (TypeError, ValueError):
        return str(iso or "")
    return time.strftime("%A, %B %-d", parsed)


def draw_calendar(surf, L, card, rect):
    """Calendar tile in the bottom zone: up to five rows, as many as fit. The first event is next (teal).
    A summary line names it."""
    _card_chrome(surf, L, rect)
    foot_h = L(64)
    y = rect.top + L(28)
    room = max(L(48), rect.bottom - y - foot_h)
    shown = card["events"][:max(1, min(5, room // L(52)))]
    extra = len(card["events"]) - len(shown)
    row_h = min(L(118), max(L(52), room // max(1, len(shown) or 1)))
    time_col = L(260)
    for i, ev in enumerate(shown):
        row = pygame.Rect(rect.left + L(24), y, rect.w - L(48), max(L(40), row_h - L(12)))
        if i == 0:
            pygame.draw.rect(surf, TEAL_BG, row, border_radius=L(16))
        px = min(L(54), max(L(22), row.h - L(20)))
        when = _line(surf, "display", px, ev["time"], LISTENING if i == 0 else MUTED, max(40, time_col - L(24)))
        bar = pygame.Rect(row.left + time_col, 0, max(4, L(8)), max(4, min(L(70), row.h - L(16))))
        bar.centery = row.centery
        pygame.draw.rect(surf, LISTENING if i == 0 else RULE, bar, border_radius=L(4))
        name = _line(surf, "display", px, ev["title"], TEXT, max(40, row.right - bar.right - L(36)))
        surf.blit(when, (row.left + time_col - L(20) - when.get_width(), row.centery - when.get_height() // 2))
        surf.blit(name, (bar.right + L(28), row.centery - name.get_height() // 2))
        y += row_h
    if shown:
        segments = [("Next:", "muted"), (" %s · %s" % (shown[0]["title"], shown[0]["time"]), "text")]
        if extra > 0:
            segments.append((" · and %d more" % extra, "muted"))
    else:
        segments = [("Nothing on this day", "muted")]
    hint_px = L(36)
    draw_voice_hint(surf, segments, rect.left + L(48), rect.bottom - L(24) - font("body", hint_px).get_height(),
                    px=hint_px, max_w=max(1, rect.w - L(72)))


RING_COLOR = {"listening": LISTENING, "thinking": THINKING, "speaking": SPEAKING}


def draw_state_glyph(surf, state, x, cy, h, color):
    """The shape that goes with each state word, so state never rests on colour alone: bars (listening),
    three dots (thinking), a speaker with waves (replying). Drawn from x, centred on cy. Returns its rect."""
    if state == "listening":
        bw, gap = max(2, h // 6), max(2, h // 9)
        for i, k in enumerate((0.45, 0.8, 1.0, 0.65, 0.35)):
            bh = max(2, int(h * k))
            pygame.draw.rect(surf, color, (x + i * (bw + gap), cy - bh // 2, bw, bh), border_radius=max(1, bw // 2))
        return pygame.Rect(x, cy - h // 2, 5 * bw + 4 * gap, h)
    if state == "thinking":
        r = max(2, h // 7)
        for i in range(3):
            pygame.draw.circle(surf, color, (x + r + i * 3 * r, cy), r)
        return pygame.Rect(x, cy - r, 8 * r, 2 * r)
    if state == "speaking":
        u = max(1, h // 10)
        pygame.draw.rect(surf, color, (x, cy - 2 * u, 3 * u, 4 * u))
        pygame.draw.polygon(surf, color, [(x + 3 * u, cy - 2 * u), (x + 6 * u, cy - 5 * u), (x + 6 * u, cy + 5 * u), (x + 3 * u, cy + 2 * u)])
        lw = max(2, u)
        for k in (1, 2):
            arc = pygame.Rect(0, 0, 2 * (k * 2 + 1) * u, 2 * (k * 2 + 1) * u)
            arc.center = (x + 6 * u, cy)
            pygame.draw.arc(surf, color, arc, -0.9, 0.9, lw)
        return pygame.Rect(x, cy - h // 2, 12 * u, h)
    return pygame.Rect(x, cy, 0, 0)


def draw_state_label(surf, L, state, px, x, cy, align="left"):
    """Icon and word ("Listening", "Thinking", "Replying") in the ring colour. Returns the rect, or None."""
    word = STATE_WORDS.get(state)
    if not word:
        return None
    color = RING_COLOR[state]
    img = font("display", px).render(word, True, color)
    gh = int(px * 0.7)
    gap = L(14)
    probe = draw_state_glyph(pygame.Surface((1, 1)), state, 0, 0, gh, color)
    total = probe.w + gap + img.get_width()
    left = x if align == "left" else x - total
    g = draw_state_glyph(surf, state, left, cy, gh, color)
    tr = img.get_rect(midleft=(g.right + gap, cy))
    surf.blit(img, tr)
    return g.union(tr)


def draw_tile_badge(surf, L, msg, state, t=1.0):
    """Upper-left status while a tile is up in a session: the avatar and ring scaled down (52 px, 5 px ring at
    1920x1080), the bot's name, then the state icon and word. Returns the x where the badge ends."""
    global LAST_PORTRAIT_RECT, LAST_PORTRAIT_ARGS, LAST_PORTRAIT_BG, LAST_BADGE_RECT
    who = msg.get("from") or VOICE_NAME
    d, ring_w = L(52), max(3, L(5))
    cy = L(34) + L(72) // 2
    photo_ok = bool(msg.get("voice"))
    img, pad = portrait(who, d, ring_w, state, t, photo_ok=photo_ok)
    origin = (L.m - pad, cy - d // 2 - pad)
    surf.blit(img, origin)
    LAST_PORTRAIT_RECT = pygame.Rect(origin, img.get_size())
    LAST_PORTRAIT_ARGS = (who, d, ring_w, photo_ok)
    LAST_PORTRAIT_BG = BG
    name = _line(surf, "body", L(40), who, TEXT, max(40, L.w // 3))
    x = L.m + d + L(24)
    nr = surf.blit(name, (x, cy - name.get_height() // 2))
    LAST_BADGE_RECT = LAST_PORTRAIT_RECT.union(nr)
    lr = draw_state_label(surf, L, state, L(40), nr.right + L(24), cy)
    if lr:
        LAST_BADGE_RECT = LAST_BADGE_RECT.union(lr)
    return LAST_BADGE_RECT.right


def zone_rect(L, top_of):
    """The bottom zone: under the clock, date and weather (top_of is their lowest y) and above the safe area,
    or the timer strip when it shows (content_limit). Full width inside the side margins."""
    top = int(top_of) + L(80)
    bottom = content_limit(L)
    return pygame.Rect(L.m, top, L.w - 2 * L.m, max(L(120), bottom - top))


def ZONE_PAD(L):
    return L(26)  # inner top and bottom padding of the voice card


def zone_reply_metrics(L, rect, has_heard):
    """(name px, heard px or 0, reply px, line step) for the voice card. The window is always 4 lines; on a
    short zone the heard line goes first, then the reply size steps down (never under 40 spec px)."""
    pad = ZONE_PAD(L)
    name_px, heard_px, reply_px = L(96), L(54), L(64)
    floor = L(40)
    while True:
        step = max(1, int(reply_px * 1.18))  # Inter 500 at line height 1.18
        need = font("display", name_px).get_height() + L(12) + Teleprompter.WINDOW_LINES * step
        if has_heard and heard_px:
            need += font("italic", heard_px).get_height() + L(12)
        if need <= rect.h - 2 * pad:
            return name_px, heard_px if has_heard else 0, reply_px, step
        if has_heard and heard_px:
            heard_px = 0
            continue
        if reply_px <= floor:
            return name_px, 0, floor, step
        reply_px = max(floor, int(reply_px * 0.92))
        name_px = max(L(56), int(name_px * 0.92))


LAST_ZONE_RECT = LAST_BADGE_RECT = LAST_TELE = None
LAST_PORTRAIT_BG = None
LAST_CHIP_RECTS = []


def draw_zone_voice(surf, L, rect, msg, state, t=1.0, tele=None, now_s=None):
    """A bot state in the bottom zone: avatar on the left, name, heard line and the 4-line reply window on the
    right, and the state icon and word at the top right."""
    global LAST_PORTRAIT_RECT, LAST_PORTRAIT_ARGS, LAST_PORTRAIT_BG, LAST_TELE
    LAST_TELE = None
    _card_chrome(surf, L, rect)
    who, heard, text = msg.get("from") or VOICE_NAME, msg.get("heard", ""), msg.get("text") or ""
    if state == "thinking":
        text = thinking_body(text)
    pad = ZONE_PAD(L)
    ring_w = max(4, L(10))
    d = max(L(80), min(L(390), rect.h - 2 * pad - 2 * ring_w))
    photo_ok = bool(msg.get("voice"))
    img, ppad = portrait(who, d, ring_w, state, t, photo_ok=photo_ok)
    origin = (rect.left + L(54) - ppad, rect.centery - d // 2 - ppad)
    surf.blit(img, origin)
    LAST_PORTRAIT_RECT = pygame.Rect(origin, img.get_size())
    LAST_PORTRAIT_ARGS = (who, d, ring_w, photo_ok)
    LAST_PORTRAIT_BG = SURFACE
    # 0.13.0: no state word in the card's top-right corner; the ring around the avatar is the state.
    x = rect.left + L(54) + d + L(96)
    width = max(L(120), rect.right - L(60) - x)
    name_px, heard_px, reply_px, step = zone_reply_metrics(L, rect, bool(heard))
    y = rect.top + pad
    name_w = width
    nm = _line(surf, "display", name_px, who, TEXT, max(40, name_w))
    surf.blit(nm, (x, y))
    y += nm.get_height() + L(12)
    if heard_px:
        hl = _line(surf, "italic", heard_px, f"“{heard}”", MUTED, width)
        surf.blit(hl, (x, y))
        y += hl.get_height() + L(12)
    color = AMBER if state == "error" else TEXT
    f = font("body", reply_px)
    lines = wrap(f, text, width) if text else []
    win = pygame.Rect(x, y, width, Teleprompter.WINDOW_LINES * step)
    if tele is not None:
        tele.set_geometry(len(lines), step, L.s)
    if not lines:
        return
    full = pygame.Surface((width, max(1, len(lines)) * step), pygame.SRCALPHA)
    for i, ln in enumerate(lines):
        full.blit(f.render(ln if f.size(ln)[0] <= width else ellipsize(f, ln, width), True, color), (0, i * step))
    LAST_TELE = {"rect": win, "text": full, "fade": L(45), "tele": tele}
    paint_tele(surf, now_s if now_s is not None else time.time())


def _window_at(full, offset, w, h):
    """The text surface cropped to the window at a pixel offset (SRCALPHA)."""
    out = pygame.Surface((w, h), pygame.SRCALPHA)
    out.blit(full, (0, -int(round(offset))))
    return out


def _fade_edges(win, fade, top, bottom):
    """Multiply a linear alpha ramp onto the top and/or bottom fade rows (Noor's 60 px mask)."""
    w, h = win.get_size()
    fade = max(1, min(fade, h // 2))
    for i in range(fade):
        a = int(255 * (i + 0.5) / fade)
        if top:
            win.fill((255, 255, 255, a), (0, i, w, 1), special_flags=pygame.BLEND_RGBA_MULT)
        if bottom:
            win.fill((255, 255, 255, a), (0, h - 1 - i, w, 1), special_flags=pygame.BLEND_RGBA_MULT)


def paint_tele(surf, now_s):
    """Repaint only the reply window (the main loop calls this at 30 fps while it scrolls). Returns its rect."""
    st = LAST_TELE
    if not st:
        return None
    rect, full, tele = st["rect"], st["text"], st["tele"]
    surf.fill(SURFACE, rect)
    if tele is None or not tele.scrolls:
        surf.blit(full, rect.topleft, pygame.Rect(0, 0, rect.w, rect.h))
        return rect
    offset = tele.step(now_s)
    win = _window_at(full, offset, rect.w, rect.h)
    fade = tele.crossfade(now_s)
    if fade is not None:
        old = _window_at(full, fade[0], rect.w, rect.h)
        old.fill((255, 255, 255, int(255 * (1 - fade[1]))), special_flags=pygame.BLEND_RGBA_MULT)
        win.fill((255, 255, 255, int(255 * fade[1])), special_flags=pygame.BLEND_RGBA_MULT)
        surf.blit(old, rect.topleft)
    top, bottom = tele.fades()
    if not tele.reduced:
        _fade_edges(win, st["fade"], top, bottom)
    surf.blit(win, rect.topleft)
    return rect


def zone_view(msg, tile_card, feed_card=None):
    """What the bottom zone shows. A tile wins the zone; a bot state then moves to the upper-left badge and its
    reply text is hidden (no caption). With no tile, the bot state fills the zone."""
    tile = tile_card if isinstance(tile_card, dict) and tile_card.get("type") in ZONE_TILES else None
    if tile is None and not msg and isinstance(feed_card, dict) and feed_card.get("type") in ZONE_TILES:
        tile = feed_card
    voice = msg if zone_voice(msg) else None
    state = (voice.get("state") or "speaking") if voice else None
    if tile is not None:
        badge = state if isinstance(msg, dict) and msg.get("voice") and msg.get("state") in BADGE_STATES else None
        return {"tile": tile, "voice": None, "badge": badge, "msg": msg}
    return {"tile": None, "voice": voice, "badge": None, "msg": msg, "state": state}


def draw_zone(surf, L, now, wx, colon_on=True, zone=None, t=1.0, tele=None, now_s=None):
    """The idle clock, date and weather, unchanged, with the bottom zone under them (docs/display.md)."""
    global LAST_ZONE_RECT, LAST_BADGE_RECT, LAST_TELE
    zone = zone or {}
    busy = bool(zone.get("tile") or zone.get("voice"))
    top_of = draw_idle(surf, L, now, wx, colon_on, home_band=not busy)
    LAST_ZONE_RECT = LAST_BADGE_RECT = LAST_TELE = None
    if not busy:
        return
    rect = zone_rect(L, top_of)
    LAST_ZONE_RECT = rect
    if zone.get("tile"):
        card = zone["tile"]
        if card["type"] == "sports":
            draw_sports(surf, L, card, rect)
        else:
            draw_calendar(surf, L, card, rect)
        if zone.get("badge"):
            draw_tile_badge(surf, L, zone["msg"], zone["badge"], t)
        return
    draw_zone_voice(surf, L, rect, zone["voice"], zone["state"], t, tele, now_s)


def chip_text(card):
    """(status word, live?, league, main) for a recipe chip. Calendar: "Next 6:30 · Practice"."""
    if card.get("type") == "sports":
        live = card.get("status") == "live"
        main = f"{card['home'].get('abbr') or card['home']['name']} {card['home_score']} – {card['away_score']} {card['away'].get('abbr') or card['away']['name']}"
        return ("LIVE" if live else str(card.get("status", "")).upper()), live, card.get("league", ""), main
    if card.get("type") == "recipe":
        return "", False, "", card.get("title") or "Recipe"
    ev = (card.get("events") or [{}])[0]
    main = f"Next {ev.get('time', '')} · {ev.get('title', '')}" if ev else "Nothing on this day"
    return "", False, "", main


def draw_tile_chip(surf, L, card, right_x, cy):
    """One recipe chip: an 82 px pill (at 2560x1706) in the top bar, right edge at right_x. Returns its rect.
    Drops the league first when the pill would pass its 760 px cap."""
    h, pad, gap = L(62), L(26), L(14)
    status, live, league, main = chip_text(card)
    parts = []
    if status:
        parts.append(("status", font("display", L(33)).render(status, True, LISTENING if live else MUTED)))
    if league:
        parts.append(("league", font("body", L(33)).render(league, True, MUTED)))
    parts.append(("main", font("display", L(36)).render(main, True, TEXT)))
    dot = L(8) if live else 0
    cap = L(570)

    def width(ps):
        return 2 * pad + (2 * dot + gap if dot else 0) + sum(p.get_width() for _, p in ps) + gap * (len(ps) - 1)

    if width(parts) > cap:
        parts = [p for p in parts if p[0] != "league"]
    if width(parts) > cap:
        f = font("display", L(36))
        parts[-1] = ("main", f.render(ellipsize(f, main, cap - width(parts[:-1]) - gap), True, TEXT))
    w = min(cap, width(parts))
    rect = pygame.Rect(right_x - w, cy - h // 2, w, h)
    pygame.draw.rect(surf, SURFACE, rect, border_radius=h // 2)
    pygame.draw.rect(surf, RULE, rect, max(2, L(2)), border_radius=h // 2)
    x = rect.left + pad
    if dot:
        pygame.draw.circle(surf, LISTENING, (x + dot, cy), dot)
        x += 2 * dot + gap
    for _, img in parts:
        surf.blit(img, (x, cy - img.get_height() // 2))
        x += img.get_width() + gap
    return rect


def draw_feed(surf, L, now, card, colon_on=True, msg=None, state=None, t=1.0, chips=()):
    """Full-screen recipe. Sports and calendar tiles that arrive meanwhile are chips in the banner slot,
    unless a timer is ringing: then the amber banner replaces them. Timers live in the rail, not a bottom strip."""
    global LAST_PORTRAIT_RECT, LAST_PORTRAIT_ARGS, LAST_CHIP_RECTS, LAST_BADGE_RECT
    global LAST_BANNER_TEXT, LAST_TIMER_RECT, LAST_CLOCK_RECT, LAST_TOPBAR_RECT
    surf.fill(BG)
    LAST_PORTRAIT_RECT = LAST_PORTRAIT_ARGS = LAST_BADGE_RECT = None
    LAST_CHIP_RECTS = []
    LAST_BANNER_TEXT = ""
    LAST_TIMER_RECT = None
    frame = recipe_frame(L)
    st = (state or (msg or {}).get("state")) if isinstance(msg, dict) and msg.get("voice") else None
    draw_status_icons(surf, L, right=frame["status"].right, cy=frame["status"].centery)
    if isinstance(now, time.struct_time):
        hhmm, ampm = clock_parts(now)
        LAST_CLOCK_RECT = text_at(surf, "display", max(14, recipe_px(L, 56)), f"{hhmm} {ampm}", TEXT,
                                  (frame["clock"].right, frame["clock"].centery), "midright")
        LAST_TOPBAR_RECT = LAST_CLOCK_RECT
    if st in BADGE_STATES:
        draw_tile_badge(surf, L, msg, st, t)
    else:
        caption = card.get("title", "") if isinstance(card, dict) else ""
        if caption:
            title = _line(surf, "body", max(14, recipe_px(L, 60)), caption, MUTED, frame["title"].w)
            y = frame["title"].top + max(0, (frame["title"].h - title.get_height()) // 2)
            surf.blit(title, (frame["title"].left, y))
    pinned = _pinned_epoch(None)
    now_e = pinned if pinned is not None else time.time()
    ringing = bool(TIMER_CHIPS) and any(_shown_ringing(t, chip_left(t, pinned)) for t in TIMER_CHIPS)
    if ringing:
        names = recipe_rail(TIMER_CHIPS, now_e, sync_recipe_order(TIMER_CHIPS))["banner_names"]
        face = font("display", max(14, recipe_px(L, 54)))
        parts = banner_parts(names, face, limit=max(40, recipe_px(L, 350)))
        LAST_BANNER_TEXT = parts["text"]
        _paint_recipe_banner(surf, L, frame["banner"], parts)
    elif chips:
        right = frame["banner"].right
        cy = frame["banner"].centery
        gap = max(4, recipe_px(L, 12))
        for chip in reversed(list(chips)[-ChipTray.MAX:]):
            r = draw_tile_chip(surf, L, chip["card"], right, cy)
            LAST_CHIP_RECTS.insert(0, r)
            right = r.left - gap
    draw_recipe(surf, L, card)


def chip_at(x, y):
    """Index into the visible chips for a tap at (x, y), or None. The station has no touch input yet; this is
    the hit test a pointer would use."""
    for i, r in enumerate(LAST_CHIP_RECTS):
        if r.collidepoint(x, y):
            return i
    return None


def draw_night(surf, L, now, colon_on=True):
    global LAST_TIMER_RECT
    LAST_TIMER_RECT = None  # the dim clock doesn't carry the timer strip
    surf.fill(BLACK)
    hhmm, ampm = clock_parts(now)
    f, fm = font("night", L(440)), font("night", L(90))  # design.md §12: night clock 440 px @1080
    wc, wm = f.size(hhmm)[0], fm.size(ampm)[0]
    shift = (now.tm_min // 10) % 4  # burn-in: drift up to 16px (at 1080p) every 10 minutes
    dx, dy = L((-8, 8, 8, -8)[shift]), L((-8, -8, 8, 8)[shift])
    x = (L.w - (wc + L(20) + wm)) // 2 + dx
    base = L.h // 2 + int(f.get_ascent() * 0.36) + dy
    draw_clock(surf, "night", L(440), hhmm, NIGHT_TEXT, x, base, colon_on)
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
    "count": ("idle", "Get ready", SPEAKING, None),
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
    if stage == "count":
        digit = extra if extra in ("1", "2", "3") else "3"
        return digit, "Recording in 3…", MUTED, f"{i} of {n}" if n else None, bar
    if stage == "recording":
        return f"Listening… {i} of {n}", "Speak after the chime.", LISTENING, f"{i} of {n}" if n else None, bar
    if stage == "loud":
        return "Too loud", f"Step back a little or speak softer. Clip {i} will retry.", MUTED, clip, bar
    if stage == "quiet":
        return "Try again", "I didn't hear it. Again after the chime.", MUTED, f"{i} of {n}" if n else clip, bar
    if stage == "saved":
        if n and i >= n and extra != "next":
            return "Saved. Checking the recordings.", "", MUTED, None, 1.0  # the deck: this line only, 4 s
        sub = "Next" if extra == "next" and i < n else ""
        return f"{i} of {n} saved", sub, MUTED, f"{i} of {n}" if n else None, i / n if n else None
    if stage == "uploaded":
        return f"All {n} clips saved", "Uploaded for training. You can use the station as usual.", MUTED, "Uploaded ✓", 1.0
    if stage == "queued":
        k = int(extra) if extra.isdigit() else 1
        when = "training now, about 10 min" if k <= 1 else f"{ordinal(k)} in line, about {10 * k} min"
        return "Training queued", f"{q} is {when}. We’ll show the result here.", MUTED, None, None
    if stage == "passed":
        shown = n or 5
        return f"{q} is ready", "Try it now.", MUTED, f"All {shown} clips saved ✓", 1.0
    if stage == "failed":
        ratio = re.fullmatch(r"(\d+)/(\d+)", extra or "")
        more = int(extra) if extra.isdigit() else 5
        sub = f"A short top-up will make {q} more reliable. It stays off until then."
        status = f"{ratio.group(1)} of {ratio.group(2)} test clips woke it" if ratio else None
        return f"Needs {more} more clips", sub, MUTED, status, None
    if stage == "error":
        return "Enrollment stopped", "Nothing was lost. Try again later.", MUTED, None, None
    return f"Say {q}" if phrase else "Wake word setup", extra, MUTED, clip if n else None, bar


def state_hold(state, done, heard="", hold_s=None):
    """Seconds a voice state stays up.

    A finished reply (done) keeps REPLY_HOLD. The finished save (N/N saved, not a between-clip Next)
    holds 4 s, then the tile closes. Passed, uploaded and error enrollment screens hold 10 s.
    Every other enrollment stage keeps the 30 s cap in VOICE_HOLDS.
    hold_s 4 is the built-in Grok wake-word line or a failed enroll start: it does not use the notice or reply holds.
    """
    if hold_s == 4:
        return 4
    if done:
        return REPLY_HOLD
    if state == "enroll":
        i, n, stage, extra = enroll_parse({"heard": heard})
        if stage == "saved" and n and i >= n and extra != "next":
            return 4
        return 10 if stage in ("passed", "uploaded", "error") else VOICE_HOLDS["enroll"]
    return VOICE_HOLDS[state]


def park_tile_for_enroll(tile, now):
    """A tile already on screen when training starts becomes a top-bar chip. Training keeps the bottom zone."""
    card = tile.get("card") if isinstance(tile, dict) else None
    if not isinstance(card, dict):
        return False
    CHIPS.add({"bot": tile.get("bot"), "id": tile.get("id"), "card": card,
               "expires": tile.get("expires") or (float(now) + SCORE_HOLD)}, float(now))
    tile.clear()
    return True


def zpx(L, design_px):
    """Pixels at the 2560×1706 design, scaled the same way Layout scales 1920×1080 spec units."""
    return int(round(float(design_px) * L.s / (2560 / 1920)))


class _EnrollWeather:
    """Clock and date with no weather fetch. paint() passes the live Weather when it has one."""

    data = None

    def offline(self):
        return False


_ENROLL_WX = _EnrollWeather()

# Portrait ring while training. "none" lets the tile draw a solid or dashed ring in the fixed 548 px box.
ENROLL_RING = {
    "prompt": "none", "count": "none", "loud": "none", "quiet": "none", "failed": "none", "error": "none", "custom": "none",
    "ready": "enroll_ready", "recording": "listening", "saved": "enroll_saved",
    "uploaded": "success", "passed": "success", "queued": "thinking",
}
_DASH_RING = {"quiet": MUTED, "loud": MUTED, "failed": AMBER}
_SOLID_RING = {"prompt": MUTED, "error": MUTED, "custom": MUTED}
_ENROLL_SQUARES = 5


def clock_date_bottom(L):
    """Bottom of the idle clock and date. Same y as draw_idle when there is no weather."""
    top = L(70) + int(L.extra_y * 0.3)
    cf = font("display", L(300))
    base = top + int(cf.get_ascent() * 0.98)
    clock_bottom = base - cf.get_ascent() + cf.get_height()
    af = font("display", L(84))
    ampm_bottom = base - af.get_ascent() + af.get_height()
    date_bottom = base + L(40) + font("body", L(60)).get_height()
    return max(clock_bottom, ampm_bottom, date_bottom)


def enroll_tile_rect(L):
    """Bottom-zone training tile. It stays under the clock and date and shrinks if that gap is short.

    Inner offsets stay tile-relative when timers push the cap up.
    """
    x, y, w, h = zpx(L, 128), zpx(L, 800), zpx(L, 2304), zpx(L, 794)
    if x + w > L.w - zpx(L, 16):
        x, w = L.m, max(1, L.w - 2 * L.m)
    cap = L.h - L(40)
    if TIMER_CHIPS:
        cap = min(cap, content_limit(L))
    floor = clock_date_bottom(L)
    # Design y when it is already clear. Otherwise sit as low as the date allows, then shrink.
    y = max(int(y), int(floor))
    if y + h > cap:
        y = max(int(floor), cap - h)
    if y + h > cap:
        h = max(1, cap - y)
    return pygame.Rect(int(x), int(y), int(w), int(h))


def _fit_px_step(kind, text, width, start, floor, step):
    """Largest size from start down to floor, in `step` px, whose single line fits width."""
    px = max(int(floor), int(start))
    step = max(1, int(step))
    floor = max(8, int(floor))
    while px > floor and font(kind, px).size(text)[0] > width:
        px = max(floor, px - step)
    return px


def _ellipsize_quoted(f, text, width):
    """Shrink the quoted wake phrase, then the whole line, so a headline stays one line."""
    if f.size(text)[0] <= width:
        return text
    m = re.search(r"([“\"'])(.+?)([”\"'])", text)
    if not m:
        return ellipsize(f, text, width)
    pre, phrase, post = text[:m.start(2)], m.group(2), text[m.end(2):]
    if f.size(pre + "…" + post)[0] > width:
        return ellipsize(f, text, width)
    lo, hi, best = 0, len(phrase), pre + "…" + post
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = pre + phrase[:mid].rstrip() + "…" + post
        if f.size(cand)[0] <= width:
            best, lo = cand, mid + 1
        else:
            hi = mid - 1
    return best


def _fit_wrapped(kind, text, width, height, start, floor, step, max_lines, lh):
    """Top-anchored lines. Shrink by `step` px, then clamp to max_lines with an ellipsis."""
    px = max(int(floor), int(start))
    step = max(1, int(step))
    floor = max(8, int(floor))
    while True:
        f = font(kind, px)
        lines = wrap(f, text, width) if text else []
        line_h = max(1, int(round(px * lh)))
        fits = len(lines) <= max_lines and all(f.size(ln)[0] <= width for ln in lines)
        if fits or px <= floor:
            if not fits:
                lines = (lines or [""])[:max_lines]
                lines[-1] = ellipsize(f, lines[-1], width)
            return f, lines, line_h
        px = max(floor, px - step)


def _dashed_ring(surf, cx, cy, outer, width, color, dash, gap):
    """Butt-capped dashes along the ring centerline: `dash` on, `gap` off, stroke `width`.

    Round dots would swallow a 6 px gap under a 14 px stroke, so each dash is a short
    radial rectangle (SVG stroke-linecap butt).
    """
    radius = outer - width / 2.0
    if radius < 2 or width < 1 or dash < 1:
        return
    circ = 2 * math.pi * radius
    period = max(1.0, float(dash) + float(gap))
    dist = 0.0
    hw = width / 2.0
    while dist < circ - 0.25:
        dash_len = min(float(dash), circ - dist)
        mid = dist + dash_len / 2.0
        ang = -math.pi / 2 + mid / radius
        rx, ry = math.cos(ang), math.sin(ang)
        tx, ty = -ry, rx
        px, py = cx + radius * rx, cy + radius * ry
        hl = dash_len / 2.0
        pts = [(px + s * hl * tx + rs * hw * rx, py + s * hl * ty + rs * hw * ry)
               for s, rs in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        pygame.draw.polygon(surf, color, pts)
        dist += period


# The station has no touch input. These stages used to draw Cancel; the hint replaces it.
QUIET_HINT = "To stop, stay quiet."
_QUIET_HINT_STAGES = ("ready", "count", "recording", "quiet", "loud", "saved")


def _enroll_actions(stage):
    """(id, label, primary, text_only) for the action row, and whether a voice hint is drawn (never, 0.13.7).

    No Cancel button: staying quiet is how a session on the station stops.
    """
    if stage == "prompt":
        return [("start", "Start", True, False), ("dismiss", "Not now", False, True)], False
    if stage in _QUIET_HINT_STAGES:
        return [], False
    if stage == "passed":
        return [("done", "Done", True, False)], False
    if stage == "failed":
        return [("more", "Record 5 more", True, False), ("dismiss", "Not now", False, True)], False
    return [("close", "Close", False, False)], False


def _square_kinds(stage, i):
    """Five squares. The heard count can still say "of 10"; the row is always five."""
    kinds = ["empty"] * _ENROLL_SQUARES
    if stage in ("queued", "prompt", "error", "custom"):
        return kinds
    if stage in ("passed", "uploaded"):
        return ["kept"] * _ENROLL_SQUARES
    if stage == "saved":
        for k in range(min(_ENROLL_SQUARES, max(0, int(i)))):
            kinds[k] = "kept"
        return kinds
    cur = max(1, min(_ENROLL_SQUARES, int(i) or 1))
    for k in range(cur - 1):
        kinds[k] = "kept"
    if stage in ("quiet", "loud", "failed"):
        kinds[cur - 1] = "bad"
    elif stage == "recording":
        kinds[cur - 1] = "now"
    return kinds


def _progress_mark(surf, rect, kind):
    cx, cy = rect.center
    if kind == "kept":
        u = rect.w * 0.22
        pts = [(cx - u, cy), (cx - u * 0.15, cy + u * 0.7), (cx + u * 1.05, cy - u * 0.85)]
        pygame.draw.lines(surf, BG, False, pts, max(2, rect.w // 10))
    elif kind == "now":
        pygame.draw.circle(surf, BG, (cx, cy), max(2, rect.w // 8))
    elif kind == "bad":
        pygame.draw.line(surf, BG, (cx, int(rect.top + rect.h * 0.22)), (cx, int(cy + rect.h * 0.05)), max(2, rect.w // 10))
        pygame.draw.circle(surf, BG, (cx, int(rect.bottom - rect.h * 0.28)), max(2, rect.w // 12))


def _draw_progress(surf, L, slot, col_w, stage, i, status):
    if stage == "queued":
        return  # the slot stays reserved and empty
    sq = max(8, zpx(L, 56))
    gap = max(2, zpx(L, 16))
    kinds = _square_kinds(stage, i)
    sq_w = len(kinds) * sq + (len(kinds) - 1) * gap
    room = max(zpx(L, 80), col_w - sq_w - zpx(L, 24))
    x = slot.x
    if status:
        px = zpx(L, 64)
        f = font("display", px)
        check = status.endswith(" ✓")
        stext = status[:-2] if check else status
        if f.size(stext)[0] > room:
            stext = ellipsize(f, stext, room)
        img = f.render(stext, True, TEXT)
        y = slot.y + max(0, (slot.h - img.get_height()) // 2)
        surf.blit(img, (slot.x, y))
        x = slot.x + img.get_width() + zpx(L, 16)
        if check:
            u = max(6, zpx(L, 28))
            cy = y + img.get_height() // 2
            pts = [(x, cy), (x + int(u * 0.38), cy + int(u * 0.38)), (x + u, cy - int(u * 0.5))]
            pygame.draw.lines(surf, LISTENING, False, pts, max(2, zpx(L, 6)))
            x += u + zpx(L, 16)
    rad = min(sq // 2, max(2, zpx(L, 14)))
    edge = max(1, zpx(L, 3))
    top = slot.y + max(0, (slot.h - sq) // 2)
    fills = {"kept": LISTENING, "now": SPEAKING, "bad": AMBER}
    for kind in kinds:
        rect = pygame.Rect(x, top, sq, sq)
        if kind == "empty":
            pygame.draw.rect(surf, SURFACE3, rect, border_radius=rad)
            pygame.draw.rect(surf, FIELD_EDGE, rect, edge, border_radius=rad)
        else:
            pygame.draw.rect(surf, fills[kind], rect, border_radius=rad)
            _progress_mark(surf, rect, kind)
        x += sq + gap


def _draw_quiet_hint(surf, L, slot, col_w):
    """Muted "To stop, stay quiet." under the progress. Not a button."""
    global LAST_ENROLL_HINT
    px = max(14, zpx(L, 44))
    f = font("body", px)
    text = QUIET_HINT if f.size(QUIET_HINT)[0] <= col_w else ellipsize(f, QUIET_HINT, col_w)
    img = f.render(text, True, MUTED)
    y = slot.y + max(0, (slot.h - img.get_height()) // 2)
    surf.blit(img, (slot.x, y))
    LAST_ENROLL_HINT = pygame.Rect(slot.x, y, img.get_width(), img.get_height())


def _draw_actions(surf, L, tile, slot, col_w, stage):
    """Buttons in the action slot. Returns nothing; hit rects land in LAST_ENROLL_BUTTONS."""
    global LAST_ENROLL_BUTTONS
    actions, _hint = _enroll_actions(stage)
    px = max(12, zpx(L, 52))
    f = font("display", px)
    bh = min(slot.h, max(44, zpx(L, 104)))
    by = slot.y + max(0, (slot.h - bh) // 2)
    pad = zpx(L, 52)
    gap = zpx(L, 20)
    rad = min(bh // 2, max(4, zpx(L, 24)))
    x = slot.x
    buttons = []
    for bid, label, primary, text_only in actions:
        tw = f.size(label)[0]
        bw = tw + (zpx(L, 24) if text_only else 2 * pad)
        if x + bw > slot.x + col_w and buttons:
            bw = max(zpx(L, 48), slot.x + col_w - x)
        rect = pygame.Rect(x, by, max(1, bw), bh)
        if stage == "prompt" and bid == "start":
            fr = rect.inflate(zpx(L, 24), zpx(L, 24))
            pygame.draw.rect(surf, LISTENING, fr, max(1, zpx(L, 6)), border_radius=rad + zpx(L, 6))
        if primary:
            pygame.draw.rect(surf, SPEAKING, rect, border_radius=rad)
            color = BG
        elif text_only:
            color = TEXT
        else:
            pygame.draw.rect(surf, SURFACE2, rect, border_radius=rad)
            pygame.draw.rect(surf, RULE, rect, max(1, zpx(L, 3)), border_radius=rad)
            color = TEXT
        img = f.render(label, True, color)
        surf.blit(img, (rect.x + (rect.w - img.get_width()) // 2, rect.y + (rect.h - img.get_height()) // 2))
        buttons.append((bid, pygame.Rect(rect)))
        x = rect.right + gap
    LAST_ENROLL_BUTTONS = buttons


def _draw_enroll_chips(surf, L):
    """Tiles that arrived during training, as the rev2 top-bar chips. Newest nearest the right edge."""
    global LAST_CHIP_RECTS
    chips = CHIPS.visible(time.time())
    LAST_CHIP_RECTS = []
    if not chips:
        return
    right = zpx(L, 2432)
    if right > L.w - zpx(L, 16):
        right = L.w - L.m
    h = L(62)
    cy = zpx(L, 120) + h // 2
    for chip in reversed(list(chips)[-ChipTray.MAX:]):
        card = chip.get("card")
        if not isinstance(card, dict):
            continue
        r = draw_tile_chip(surf, L, card, right, cy)
        LAST_CHIP_RECTS.insert(0, r)
        right = r.left - L(12)


def enroll_hit(x, y):
    """Tap target. On the prompt stage the whole tile starts, except Not now. None outside the tile."""
    for bid, rect in LAST_ENROLL_BUTTONS:
        if rect.collidepoint(x, y):
            return bid
    if LAST_ENROLL_STAGE == "prompt" and LAST_ENROLL_RECT and LAST_ENROLL_RECT.collidepoint(x, y):
        return "start"
    return None


def draw_meter(surf, L, cx, cy, kind):
    """7 rounded bars under the state label: live (teal, varied), full (text color: too loud), flat (faint: nothing)."""
    bw, gap, hmax = max(2, L(16)), max(2, L(12)), L(90)
    hs = {"live": (0.35, 0.6, 0.85, 1.0, 0.75, 0.5, 0.3), "full": (1.0,) * 7, "flat": (0.08,) * 7}[kind]
    color = {"live": LISTENING, "full": TEXT, "flat": FAINT}[kind]
    x0 = cx - (7 * bw + 6 * gap) // 2
    for k, hh in enumerate(hs):
        bh = max(bw // 2, int(hmax * hh))
        pygame.draw.rect(surf, color, (x0 + k * (bw + gap), cy - bh // 2, bw, bh), border_radius=bw // 2)


def draw_enroll(surf, L, now, msg, t=1.0, wx=None):
    """Training tile in the bottom zone. The avatar box does not move between stages.

    The ring is the only state cue: solid, or dashed (8 on / 6 off) for a quiet/loud retry and for failed.
    No top-right state word. Persona portrait, or the Echo mark on surface-2 when there is no persona.
    """
    global LAST_PORTRAIT_RECT, LAST_PORTRAIT_ARGS, LAST_PORTRAIT_BG
    global LAST_ENROLL_RECT, LAST_ENROLL_AVATAR, LAST_ENROLL_STAGE
    global LAST_ENROLL_HINT, LAST_ENROLL_PROGRESS, LAST_ENROLL_HEADLINE
    i, n, stage, extra = enroll_parse(msg)
    prompt = msg.get("text") or ""
    m = re.search(r"[“\"](.+?)[”\"]", prompt)
    phrase = m.group(1) if m else prompt
    who = msg.get("from") or VOICE_NAME
    hit = persona_names().get(name_key(who))
    label = hit[0] if hit else who
    if label == VOICE_NAME:
        label = phrase or label
    draw_idle(surf, L, now, wx or _ENROLL_WX, home_band=False)
    tile = enroll_tile_rect(L)
    LAST_ENROLL_RECT = tile
    LAST_ENROLL_STAGE = stage
    border = max(1, zpx(L, 3))
    rad = max(4, zpx(L, 37))
    pygame.draw.rect(surf, SURFACE, tile, border_radius=rad)
    pygame.draw.rect(surf, RULE, tile, border, border_radius=rad)
    prev = surf.get_clip()
    surf.set_clip(tile)
    ring_w = max(2, zpx(L, 14))
    av = pygame.Rect(tile.x + border + zpx(L, 72), tile.y + border + zpx(L, 123), zpx(L, 548), zpx(L, 548))
    if av.w > tile.w - 2 * border:
        av.w = av.h = max(16, tile.w - 2 * border)
    LAST_ENROLL_AVATAR = pygame.Rect(av)
    d = max(8, av.w - 2 * ring_w)
    img, pad = portrait(who, d, ring_w, ENROLL_RING.get(stage, "none"), t, photo_ok=True)
    origin = (av.x + (av.w - d) // 2 - pad, av.y + (av.h - d) // 2 - pad)
    surf.blit(img, origin)
    LAST_PORTRAIT_RECT = pygame.Rect(origin, img.get_size())
    LAST_PORTRAIT_ARGS = (who, d, ring_w, True)
    LAST_PORTRAIT_BG = SURFACE
    if stage in _DASH_RING:
        _dashed_ring(surf, av.centerx, av.centery, av.w / 2.0, ring_w, _DASH_RING[stage], zpx(L, 8), zpx(L, 6))
    elif stage in _SOLID_RING:
        pygame.draw.circle(surf, _SOLID_RING[stage], av.center, int(av.w / 2), ring_w)
    col_x = tile.x + zpx(L, 720)
    col_w = max(40, tile.right - zpx(L, 80) - col_x)
    eye = f"Wake word setup · {label}"
    eye_slot = pygame.Rect(col_x, tile.y + zpx(L, 61), col_w, max(1, zpx(L, 60)))
    surf.set_clip(eye_slot.clip(tile))
    ef = font("body", zpx(L, 44))
    surf.blit(ef.render(eye if ef.size(eye)[0] <= col_w else ellipsize(ef, eye, col_w), True, MUTED), eye_slot.topleft)
    head, sub, scolor, status, _bar = enroll_copy(phrase, i, n, stage, extra)
    hf = font("display", _fit_px_step("display", head, col_w, zpx(L, 128), zpx(L, 96), zpx(L, 8)))
    # Descent is negative. Ascent minus descent is the line box, and it does not depend on the string.
    head_h = max(1, hf.get_ascent() - hf.get_descent())
    head_slot = pygame.Rect(col_x, tile.y + zpx(L, 170), col_w, head_h)
    LAST_ENROLL_HEADLINE = pygame.Rect(head_slot)
    surf.set_clip(head_slot.clip(tile))
    shown = _ellipsize_quoted(hf, head, col_w)
    surf.blit(hf.render(shown, True, TEXT), head_slot.topleft)
    # Lines under the headline start at its bottom, so a descender is not covered.
    shift = max(0, head_slot.bottom - (tile.y + zpx(L, 321)))
    sub_slot = pygame.Rect(col_x, tile.y + zpx(L, 321) + shift, col_w, max(1, zpx(L, 170)))
    surf.set_clip(sub_slot.clip(tile))
    if sub:
        sf, lines, step = _fit_wrapped("body", sub, col_w, sub_slot.h, zpx(L, 72), zpx(L, 60), zpx(L, 4), 2, 1.18)
        y = sub_slot.y
        for ln in lines:
            if y >= sub_slot.bottom:
                break
            surf.blit(sf.render(ln, True, scolor), (col_x, y))
            y += step
    prog = pygame.Rect(col_x, tile.y + zpx(L, 521) + shift, col_w, max(1, zpx(L, 88)))
    LAST_ENROLL_PROGRESS = pygame.Rect(prog)
    surf.set_clip(prog.clip(tile))
    # The success tile (last clip saved) shows only its line: no progress, no quiet hint (rev1.3 copy deck).
    success = stage == "saved" and n and i >= n and extra != "next"
    if not success:
        _draw_progress(surf, L, prog, col_w, stage, i, status)
    act = pygame.Rect(col_x, tile.y + zpx(L, 641) + shift, col_w, max(1, zpx(L, 104)))
    # Focus ring sits 6 px outside the button, so this clip is the tile, not the slot.
    surf.set_clip(tile)
    _draw_actions(surf, L, tile, act, col_w, stage)
    if stage in _QUIET_HINT_STAGES and not success:
        _draw_quiet_hint(surf, L, act, col_w)
    surf.set_clip(prev)
    _draw_enroll_chips(surf, L)
    draw_timer_strip(surf, L)


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


def screen_for(setup_doc, busy, msg, night, feed=None, timers=False, tile=None):
    """Which screen paint() shows. Setup (or hotspot) until setup is done. After that:
    - "feed": a recipe, full screen (the one full-screen tile), during a session or from the deck;
    - "zone": the idle clock, date and weather with the bottom zone under them, holding a sports or calendar
      tile, or a bot state (listening, thinking, replying, error) when no tile is up;
    - "session": the full-screen layouts that stay as they were (plain /show text, enrollment, the T1
      read-back, a reply with an inline info card);
    - the night clock, the timer rings when any timer is up, or the idle clock.
    Night hides timers. Night and a voice session win over cards. A tile keeps the timer strip instead of the
    rings. Nothing else (no certificate or pairing screen) can take over."""
    if setup_doc:
        return "hotspot" if setup_doc.get("mode") == "hotspot" else "setup"
    if msg and isinstance(tile, dict) and tile.get("type") in TILE_TYPES:
        return "feed" if tile["type"] in FULL_TILES else "zone"  # a tile reply; speech still plays
    if busy or msg:
        return "zone" if zone_voice(msg) else "session"
    if night:
        return "night"
    if isinstance(feed, dict) and feed.get("type") in TILE_TYPES:
        return "feed" if feed["type"] in FULL_TILES else "zone"
    if timers:
        return "timers"
    return "idle"


SCREENS = {"idle": draw_idle, "night": draw_night, "session": draw_session, "setup": draw_setup, "hotspot": draw_hotspot,
           "timers": draw_timers, "feed": draw_feed, "zone": draw_zone}


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

    def _presented(self):
        """Bytes to write, and a surface that must stay alive while those bytes are used.

        The working surface stays bright. A 16bpp frame is scaled (RGB565) so a black overlay
        matches CSS opacity (100-level)/100. Other depths get a black SRCALPHA blit on a copy.
        """
        raw = self.full.get_buffer().raw
        level = None if dimming is None else dimming.overlay_level()
        if level is None or level >= 100:
            return raw, None
        if self.full.get_bytesize() == 2:
            buf = bytearray(raw)
            dimming.shade_rgb565(buf, level)
            return buf, None
        copy = self.full.copy()
        layer = pygame.Surface(copy.get_size(), pygame.SRCALPHA)
        layer.fill((0, 0, 0, dimming.overlay_alpha(level)))
        copy.blit(layer, (0, 0))
        return copy.get_buffer().raw, copy

    def _snap_surface(self):
        level = None if dimming is None else dimming.overlay_level()
        if level is None or level >= 100:
            return self.full
        if self.full.get_bytesize() != 2:
            return self._presented()[1] or self.full
        raw, _hold = self._presented()
        w, h = self.full.get_size()
        pitch, bpp = self.full.get_pitch(), 2
        try:
            if pitch == w * bpp:
                return pygame.image.frombytes(bytes(raw), (w, h), "RGB565")
            tight = b"".join(bytes(raw[y * pitch:y * pitch + w * bpp]) for y in range(h))
            return pygame.image.frombytes(tight, (w, h), "RGB565")
        except (pygame.error, ValueError, TypeError):
            return self.full

    def flush(self):
        if self.fb:
            raw, hold = self._presented()
            with open(self.fb, "wb") as f:  # 16bpp RGB565 surface matches the vc4 fbdev layout
                f.write(raw)
            del hold

    def flush_rect(self, rect):
        """Write only rect's rows/columns. fb=None (demo, tests) is a no-op."""
        if not self.fb or not rect:
            return
        rect = pygame.Rect(rect).clip(self.screen.get_rect()).move(self.view.topleft)  # canvas -> framebuffer coords
        if rect.w <= 0 or rect.h <= 0:
            return
        bpp, pitch = self.full.get_bytesize(), self.full.get_pitch()
        raw, hold = self._presented()
        x, y, w, h = rect
        with open(self.fb, "r+b") as f:
            for row in range(y, y + h):
                off = row * pitch + x * bpp
                f.seek(off)
                f.write(raw[off:off + w * bpp])
        del hold

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
                pygame.image.save(self._snap_surface(), SNAP)
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
    text_hold = None    # {"until", "pending", "who"} while a reply waits to see if a card follows
    card_hold = False   # the card hold after a session that showed a card has started
    strip_at = 0.0      # last time the timer strip was painted; ticks are at least 0.9 s apart
    tele = None         # the reply window's motion while a reply is up in the bottom zone (Teleprompter)

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
        global LAST_TIMER_RECT, LAST_PORTRAIT_BG, LAST_ZONE_RECT, LAST_BADGE_RECT, LAST_TELE
        LAST_PORTRAIT_BG = LAST_ZONE_RECT = LAST_BADGE_RECT = LAST_TELE = None  # set again by what this paint draws
        now = time.localtime()
        on = colon_blink_on()
        sd = setup["doc"]
        if sd:  # until setup is done this is the idle screen and the screen on wake (voice is off)
            LAST_TIMER_RECT = None
            arm_recipe_screen(False)
            disp.show(draw_hotspot if sd["mode"] == "hotspot" else draw_setup, now, sd, fade=fade, snapshot=snapshot)
            last_key = ("setup", now.tm_min, json.dumps(sd, sort_keys=True))
            return
        night = night_face(now)
        t_now = time.time()
        view_card = None
        if not thinking() and not msg and not night:
            page = deck().current(t_now)
            if page and page["card"]["type"] in TILE_TYPES:
                view_card = page["card"]
        tile_card = TILE.get("card") if msg else None
        enroll_up = isinstance(msg, dict) and msg.get("state") == "enroll"
        if CHIPS and not enroll_up and not ((tile_card or view_card or {}).get("type") == "recipe"):
            # The recipe ended (closed, expired, or the session moved on): the newest chip with TTL left
            # takes the bottom zone. With no session the deck already holds that card.
            # Training keeps the zone, so chips stay chips until it ends.
            chip = CHIPS.handoff(t_now)
            if chip and msg and not TILE and deck().get(chip["bot"], chip["id"]):
                TILE.update({"bot": chip["bot"], "id": chip["id"], "card": chip["card"]}, shown=t_now)
                tile_card = TILE["card"]
        if tile_card:
            view_card = tile_card
        view = screen_for(sd, thinking(), msg, night, view_card, bool(TIMER_CHIPS), tile_card)
        arm_recipe_screen(view == "feed")
        ph = phase_now() if thinking() else 1.0
        if view == "session" and isinstance(msg, dict) and msg.get("state") == "enroll":
            disp.show(draw_enroll, now, msg, ph, wx, fade=fade, snapshot=snapshot)
            key = ("session", now.tm_min)
        elif view == "session":
            if thinking():
                disp.show(draw_session, now, msg, msg["state"], ph, fade=fade, snapshot=snapshot)
            else:
                disp.show(draw_session, now, msg, fade=fade, snapshot=snapshot)
            key = ("session", now.tm_min)
        elif view == "zone":
            zone = zone_view(msg, tile_card, view_card)
            if tele is not None and zone.get("voice") is None:
                tele.stop(t_now)  # a tile took the zone: the reply window freezes where it is
            run = tele if zone.get("state") == "speaking" else None
            disp.show(draw_zone, now, wx, on, zone, ph, run, t_now, fade=fade, snapshot=snapshot)
            key = ("zone", now.tm_min, wx.offline(), json.dumps(wx.data), feed_identity())
        elif view == "night":
            disp.show(draw_night, now, on, fade=fade, snapshot=snapshot)
            key = ("night", now.tm_min)
        elif view == "timers":
            disp.show(draw_timers, now, fade=fade, snapshot=snapshot)
            key = ("timers", now.tm_min, len(TIMER_CHIPS))
        elif view == "feed":
            chips = CHIPS.visible(t_now)
            if msg and msg.get("voice"):
                ph = phase_now() if anim_started and anim_of(msg) else 1.0
                disp.show(draw_feed, now, view_card, on, msg, msg.get("state"), ph, chips, fade=fade, snapshot=snapshot)
            else:
                disp.show(draw_feed, now, view_card, on, None, None, 1.0, chips, fade=fade, snapshot=snapshot)
            key = ("feed", now.tm_min, feed_identity())
        else:
            disp.show(draw_idle, now, wx, on, fade=fade, snapshot=snapshot)
            key = ("idle", now.tm_min, wx.offline(), json.dumps(wx.data), feed_identity())
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
        bg = tuple(LAST_PORTRAIT_BG or BG)
        if any(k[0] != (args, bg) for k in spin_frames):
            spin_frames.clear()
        key = ((args, bg), state, i)
        frame = spin_frames.get(key)
        if frame is None:
            who, d, ring_w, photo_ok = args
            frame = pygame.Surface(rect.size, depth=disp.screen.get_bitsize())
            frame.fill(bg)  # the zone card's SURFACE, or the screen behind the badge
            frame.blit(portrait(who, d, ring_w, state, i / n, photo_ok=photo_ok)[0], (0, 0))
            spin_frames[key] = frame
        disp.screen.blit(frame, rect.topleft)
        disp.flush_rect(rect)

    def tele_live():
        """The reply window on screen still moves on its own."""
        return bool(tele is not None and LAST_TELE and LAST_TELE.get("tele") is tele and last_key
                    and last_key[0] == "zone" and tele.moving(time.time()))

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
        """Countdown tick. The timers screen repaints in full; a session only repaints the chip strip."""
        nonlocal strip_at
        if not TIMER_CHIPS or setup["doc"] or (last_key and last_key[0] == "night"):
            return
        now = time.time()
        on_timers = last_key and last_key[0] == "timers"
        on_feed = last_key and last_key[0] == "feed"
        # A re-pack is 240 ms. Reduced motion stays on the slow tick; a slide or a ringing pulse does not.
        gap = feed_frame_gap(bool(on_feed and rail_slide_running()),
                             bool((on_feed or on_timers) and any_timer_ringing()), reduce_motion())
        if now - strip_at < gap:
            return
        strip_at = now
        if on_timers:
            disp.screen.fill(BG)
            draw_timers(disp.screen, disp.L, time.localtime())
            disp.flush()
            return
        if not LAST_TIMER_RECT:
            paint(snapshot=False)
        else:
            repaint_timer_strip(disp)

    def apply_chips(ev):
        if getattr(ev, "has_timers", False) and ev.timers is not None:
            TIMER_CHIPS[:] = anchor_timer_chips([dict(t) for t in ev.timers])
            return True
        return False

    refresh_dim(force=True)
    paint()
    threading.Thread(target=wx.loop, daemon=True).start()
    threading.Thread(target=status_loop, name="status", daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"listening on :{PORT}", flush=True)
    while True:
        why = refresh_dim()
        if why and not setup["doc"] and (why == "overlay" or not msg):
            paint(snapshot=False)  # a session stays a session; only the overlay changes under it
        if card_hold and TILE:
            deadline = deck().idle_deadline(TILE.get("bot"), TILE.get("id"))
            if deadline is not None:
                until = deadline  # a step command can push a recipe's idle clock out
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
                setup["doc"], msg, anim_started, card_hold, text_hold = sd, None, None, False, None
                paint(fade=True)
                print(f"setup screen: {sd['mode'] if sd else 'off'}", flush=True)
        if setup["doc"]:
            wait = min(wait, 2.0)
        if TIMER_CHIPS and not setup["doc"] and not (last_key and last_key[0] == "night"):
            on_timers = last_key and last_key[0] == "timers"
            on_recipe = last_key and last_key[0] == "feed"
            gap = feed_frame_gap(bool(on_recipe and rail_slide_running()),
                                 bool((on_recipe or on_timers) and any_timer_ringing()), reduce_motion())
            if gap < 0.5:
                wait = min(wait, gap)  # slide or amber pulse: a few frames inside 240 ms
            else:
                delay = 1.0 - (t % 1.0)  # next whole second, so mm:ss steps together
                if delay < 0.05:
                    delay += 1.0
                wait = min(wait, delay)
        if _DIM.get("mode") not in (None, "off"):
            wait = min(wait, 30.0)  # wake to re-read the sensor or the sun
        if not setup["doc"] and not msg and not night_face(time.localtime()):
            left = deck().seconds_until_change(time.time())
            if left is not None:
                wait = min(wait, max(0.05, left))
        if text_hold:
            wait = min(wait, max(0.05, text_hold["until"] - time.time()))
        left = CHIPS.next_change(time.time())
        if left is not None:
            wait = min(wait, max(0.05, left))
        if tele_live():
            wait = min(wait, 1.0 / 30)
        ev = pygame.event.wait(max(1, int(wait * 1000)))
        if setup["doc"] and ev.type in (STATE, SHOW, FEED):
            continue  # nothing else is shown until setup is done; the deck still kept the card
        if ev.type == STATE:
            if getattr(ev, "speech", None) is not None and tele is not None:
                tele.feed(ev.speech, time.time())  # the window's own 30 fps tick draws it
            chips_changed = apply_chips(ev)
            if ev.state in VOICE_HOLDS:
                hold = state_hold(ev.state, bool(ev.done), getattr(ev, "heard", "") or "", getattr(ev, "hold_s", None))
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
                prev_state = msg.get("state") if msg else None
                now_s = time.time()
                if ev.state == "enroll" and TILE:
                    park_tile_for_enroll(TILE, now_s)  # training takes the zone; the tile becomes a chip
                elif TILE and tile_step(TILE, prev_state, ev.state, now_s, SCORE_HOLD) == "clear":
                    deck().clear(TILE.get("bot"), TILE.get("id"))
                    TILE.clear()  # back to the full-screen status as the response starts
                had = msg is not None
                live_card = bool(TILE)  # a card already up wins; a stale one was cleared just above
                if ev.state == "speaking" and not ev.done:
                    decision = reply_visual(now_s, now_s + TEXT_CARD_WAIT, live_card, had)
                else:
                    decision = "text"
                if decision == "hold":
                    # Stay off the reply text. Speaking (not thinking) so the ring stops; audio is already going.
                    text_hold = {"until": now_s + TEXT_CARD_WAIT, "pending": new, "who": ev.who}
                    shown = dict(new)
                    shown["text"] = "…"
                    msg = shown
                else:
                    text_hold = None
                    msg = new
                tele = tele_next(tele, prev_state, ev.state, bool(ev.done), now_s, reduce_motion())
                until = now_s + hold
                paint()
            elif ev.state == "idle":
                # session end: a conversation card returns on its idle clock (ttl is only the cap)
                text_hold = None
                if tele is not None:
                    tele.settle(time.time())
                if msg and TILE:
                    now_s = time.time()
                    msg = turn_over(msg)  # the corner badge stops showing listening/thinking
                    anim_started = None
                    deck().arm_idle(TILE.get("bot"), TILE.get("id"), now_s,
                                    sports_idle=(cards.sports_idle_span(TILE.get("shown", now_s), now_s)
                                                 if (TILE.get("card") or {}).get("type") == "sports" else cards.CONVO_IDLE_S))
                    deadline = deck().idle_deadline(TILE.get("bot"), TILE.get("id"))
                    until = deadline if deadline is not None else now_s + cards.CONVO_IDLE_S
                    card_hold = True
                    paint()
                elif msg and msg.get("card") and not card_hold:
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
        if ev.type == FEED:
            if getattr(ev, "recipe_closed", False):
                TILE.clear()
                if msg and msg.get("voice"):
                    paint()
                elif not night_face(time.localtime()):
                    paint(snapshot=False)
                continue
            if getattr(ev, "chips_changed", False):
                if (msg and msg.get("voice")) or not night_face(time.localtime()):
                    paint(snapshot=not msg)
                continue
            tile = getattr(ev, "tile", None)
            on_recipe = recipe_up((TILE.get("card") if msg else None), deck().current(time.time()))
            enroll_up = isinstance(msg, dict) and msg.get("state") == "enroll"
            if tile and enroll_up and (tile.get("card") or {}).get("type") in TILE_TYPES:
                CHIPS.add(tile, time.time())  # training keeps the zone; the arrival is a chip
                paint(snapshot=False)
                continue
            if tile and (tile.get("card") or {}).get("type") in ZONE_TILES and on_recipe:
                CHIPS.add(tile, time.time())  # the recipe stays; the tile is a chip until its TTL or "dismiss"
                if (msg and msg.get("voice")) or not night_face(time.localtime()):
                    paint(snapshot=not msg)
                continue
            if tile and msg and msg.get("voice"):
                if text_hold and persona_match(text_hold.get("who"), tile.get("bot")):
                    msg = text_hold["pending"]  # badge only; the reply text is not the screen
                text_hold = None
                TILE.clear()
                TILE.update(tile, shown=time.time())
                if tele is not None:
                    tele.stop(time.time())  # the reply text hides; the window stops where it is
                paint()
            elif not msg and not night_face(time.localtime()):
                if tile:
                    deck().arm_idle(tile.get("bot"), tile.get("id"), time.time())
                paint(snapshot=False)
        elif ev.type == SHOW:
            msg, until, anim_started, card_hold, text_hold = ev.msg, time.time() + HOLD, None, False, None
            paint()
            print("rendered", flush=True)
        elif ev.type == WEATHER:
            if not msg and not night_face(time.localtime()):
                paint(snapshot=False)
        elif ev.type == STATUS_EV:
            paint(snapshot=False)
        elif ev.type == pygame.QUIT:
            break
        elif ev.type == pygame.NOEVENT:
            if text_hold and time.time() >= text_hold["until"]:
                pending = text_hold["pending"]
                text_hold = None
                if not TILE:
                    msg = pending  # no card came; show the reply as before
                    paint()
            elif msg and time.time() >= until:
                if TILE:
                    deck().clear(TILE.get("bot"), TILE.get("id"))  # do not leave the tile up on the idle feed
                    TILE.clear()
                    msg, anim_started, card_hold = None, None, False
                    paint(fade=True)
                    print("back to idle", flush=True)
                elif msg.get("card") and not card_hold:
                    card_hold = True
                    until = time.time() + CARD_HOLD
                else:
                    msg, anim_started, card_hold = None, None, False
                    paint(fade=True)
                    print("back to idle", flush=True)
            elif thinking():  # full-screen ring, or the tile's corner badge
                spin_portrait()  # no full-screen repaint, no snapshot
                maybe_strip()
            elif CHIPS.prune(time.time()):
                paint(snapshot=False)  # a chip reached its tile's TTL
            elif tele_live():
                disp.flush_rect(paint_tele(disp.screen, time.time()))  # only the reply window
                maybe_strip()
            elif last_key is None or last_key[1] != time.localtime().tm_min:
                paint(snapshot=False)
            elif not msg and feed_due(last_key, feed_identity()):
                paint(snapshot=False)
            elif not msg and E("GROK_STATION_BLINK", "0") != "0":
                blink_colon()
                maybe_strip()
            else:
                maybe_strip()


def demo(out, size):
    """Render every screen to PNG with fixed sample data (no framebuffer, no network)."""
    global WX_CACHE, TIMER_EPOCH
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
                                               ("uploaded", 5, "", 1), ("queued", 0, "2", 0.3), ("passed", 0, "", 1),
                                               ("failed", 0, "5", 1)), 1):
        em = {"from": "Finn", "voice": True, "state": "enroll", "text": "“Hey Finn”", "heard": f"{i}/5|{stage}|{extra}"}
        shots.append((f"enroll-{k:02d}-{stage}", draw_session, (now, em, "enroll", t)))
    shots.append(("enroll-no-persona", draw_session, (now, {"from": "Grok", "voice": True, "state": "enroll",
                                                             "text": "“Hey Nova”", "heard": "1/5|prompt|"}, "enroll", 1)))
    sd = {"mode": "setup", "code": "K7P4QX", "host": "grok-station.local", "url": "https://grok-station.local"}
    shots.append(("setup-needed", draw_setup, (now, sd)))
    shots.append(("setup-reset", draw_setup, (now, dict(sd, mode="reset", code="R4M9TW"))))
    demo_qr = [("".join("1" if ((r * 7 + c * 3) % 5 < 2 or (r < 7 and c < 7)) else "0" for c in range(29))) for r in range(29)]
    shots.append(("setup-hotspot", draw_hotspot, (now, dict(sd, mode="hotspot", ssid="grok-station-7F3A", password="hearth-4821",
                                                            url="https://10.42.0.1", qr_join=demo_qr, qr_open=demo_qr))))
    shots.append(("t1-confirm", draw_session, (now, {"from": "Finn", "heard": "add|grocery|0", "text": "oat milk and two lemons", "state": "confirm"})))
    recipe = {"type": "recipe", "v": 1, "title": "Weeknight soup", "servings": 4,
              "ingredients": ["onion", "carrots", "stock"], "steps": ["Chop the vegetables.", "Simmer twenty minutes.", "Season and serve."], "step": 2}
    sports = {"type": "sports", "v": 1, "league": "City league",
              "home": {"name": "Harbor City", "abbr": "HC"}, "away": {"name": "Ridgeview", "abbr": "RV"},
              "home_score": 3, "away_score": 2, "status": "live", "period": "2nd",
              "schedule": [{"label": "Harbor City at Ridgeview", "when": "Sat 7pm"}]}
    calendar = {"type": "calendar", "v": 1, "date": "2026-10-22",
                "events": [{"time": "9:00", "title": "Market"}, {"time": "18:30", "title": "Practice"}]}
    shots.append(("card-recipe", draw_feed, (now, recipe)))
    shots.append(("card-sports", draw_zone, (now, wx, True, zone_view(None, None, sports))))
    shots.append(("card-calendar", draw_zone, (now, wx, True, zone_view(None, None, calendar))))
    voice = {"from": "Grok", "voice": True, "heard": "what's the weather like tomorrow", "state": "speaking",
             "text": "Tomorrow looks mild: partly sunny, a high of 71 and a low of 52, with a light breeze in the "
                     "afternoon. No rain expected, so it is a good day for a walk by the harbor."}
    shots.append(("zone-score-badge", draw_zone, (now, wx, True, zone_view(voice, sports))))
    shots.append(("recipe-chips", draw_feed, (now, recipe, True, voice, "speaking", 1.0,
                                              [{"card": sports}, {"card": calendar}])))
    home = {"type": "home", "v": 1, "title": "Stocks", "items": [
        {"label": "Northwind", "value": "12.40", "trend": "up"},
        {"label": "Harbor", "value": "8.10", "trend": "down"}]}

    def draw_home_demo(surf, L, now, wx):
        global DECK
        old = DECK
        sample = cards.Deck(rotate_s=20, ttl_max=86400)
        sample.put("jules", "stocks", 10, 60, home, time.time())
        DECK = sample
        try:
            draw_idle(surf, L, now, wx)
        finally:
            DECK = old

    shots.append(("card-home", draw_home_demo, (now, wx)))
    for name, painter, args in shots:
        disp.screen.fill(BG)
        painter(disp.screen, disp.L, *args)
        pygame.image.save(disp.full, os.path.join(out, f"{name}.png"))
        print(os.path.join(out, f"{name}.png"))
    epoch = 1_700_000_000.0
    TIMER_EPOCH = epoch
    TIMER_CHIPS[:] = [
        {"id": "aaaa01", "label": "pasta", "ends_at_epoch": epoch + 462, "duration": 600, "ringing": False},
        {"id": "aaaa02", "label": "oven", "ends_at_epoch": epoch + 810, "duration": 1800, "ringing": False},
    ]
    disp.screen.fill(BG)
    draw_timers(disp.screen, disp.L, now, epoch)
    pygame.image.save(disp.full, os.path.join(out, "timers.png"))
    print(os.path.join(out, "timers.png"))
    TIMER_CHIPS[0]["ringing"] = True
    TIMER_CHIPS[0]["ends_at_epoch"] = epoch - 1
    disp.screen.fill(BG)
    draw_timers(disp.screen, disp.L, now, epoch)
    pygame.image.save(disp.full, os.path.join(out, "timer-ringing.png"))
    print(os.path.join(out, "timer-ringing.png"))
    TIMER_CHIPS[:] = []
    TIMER_EPOCH = None


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--demo":
        size = tuple(map(int, (sys.argv[3] if len(sys.argv) > 3 else "2560x1706").split("x")))
        demo(sys.argv[2], size)
    else:
        main()
