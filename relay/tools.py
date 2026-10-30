"""Station tools for every voice persona (T0-equivalent): read-only lookups plus timers that live on the station.
Nothing here sends, posts, buys, books or changes anything outside the station, and no tool needs a credential.
  get_time       local date/time (TZ_NAME), or the time in another IANA zone
  get_weather    Open-Meteo (no key) for the configured location WEATHER_LAT/WEATHER_LON (WEATHER_PLACE names it)
  convert_units  length, weight, volume (incl. cooking), temperature, speed, area
  set_timer / list_timers / cancel_timer / extend_timer
      the station rings; the relay keeps a copy so a relay restart can restore them

run() returns (tool output, message for the Pi). A successful time, weather or unit lookup also sends a small
display card ({"type":"card",...}); timers still send timer_set / timer_cancel. A failed tool sends nothing.
The weather card has no place name and no coordinates — those stay in the tool output for the spoken answer only.
"""
import datetime, json, os, re, secrets, sys, time, urllib.parse
from zoneinfo import ZoneInfo, available_timezones

E = os.environ.get
TZ = E("TZ_NAME", "America/Chicago")
LAT, LON, PLACE = E("WEATHER_LAT", ""), E("WEATHER_LON", ""), E("WEATHER_PLACE", "")
UNITS = E("WEATHER_UNITS", "fahrenheit")
# Single device: the admin page saves the location in the station env file (GROK_STATION_LAT/LON/UNITS).
# Read it on every weather call (cached by mtime), so a save takes effect without a relay restart.
STATION_ENV = E("STATION_ENV_FILE", "/etc/grok-station/env")
_station_env = {"mtime": None, "vals": {}, "warned": False}


def _env_vals(path):
    vals = {}
    with open(path) as f:
        for line in f:
            m = re.match(r"^\s*(GROK_STATION_(?:LAT|LON|UNITS))\s*=\s*(.*?)\s*$", line)
            if m:
                vals[m.group(1)] = m.group(2).strip("\"'")
    return vals


def location():
    """(lat, lon, units). WEATHER_* env wins when set; else the station env file the admin page writes."""
    try:
        mt = os.stat(STATION_ENV).st_mtime_ns
        if mt != _station_env["mtime"]:
            _station_env.update(mtime=mt, vals=_env_vals(STATION_ENV))
    except PermissionError:
        if not _station_env["warned"]:
            print(f"warning: can't read {STATION_ENV} (permission denied); weather has no location. "
                  "Expected root:grok 0640.", file=sys.stderr, flush=True)
            _station_env["warned"] = True
        _station_env.update(mtime=None, vals={})
    except OSError:
        _station_env.update(mtime=None, vals={})
    v = _station_env["vals"]
    lat = LAT or v.get("GROK_STATION_LAT", "")
    lon = LON or v.get("GROK_STATION_LON", "")
    units = E("WEATHER_UNITS") or v.get("GROK_STATION_UNITS") or "fahrenheit"
    if not (re.fullmatch(r"-?\d{1,2}(\.\d+)?", lat or "") and re.fullmatch(r"-?\d{1,3}(\.\d+)?", lon or "")):
        lat = lon = ""
    return lat, lon, units if units in ("fahrenheit", "celsius") else "fahrenheit"
MAX_TIMERS, MAX_TIMER_S = 8, 24 * 3600

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "foggy", 48: "foggy", 51: "light drizzle",
       53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
       65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
       77: "snow grains", 80: "rain showers", 81: "rain showers", 82: "heavy rain showers", 85: "snow showers",
       86: "heavy snow showers", 95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}
# Display icon for a WMO code. One of sun|partly|cloud|fog|rain|snow|storm; unknown codes fall back to cloud.
_ICON = {0: "sun", 1: "partly", 2: "partly", 3: "cloud", 45: "fog", 48: "fog",
         51: "rain", 53: "rain", 55: "rain", 56: "rain", 57: "rain", 61: "rain", 63: "rain", 65: "rain", 66: "rain", 67: "rain",
         80: "rain", 81: "rain", 82: "rain", 71: "snow", 73: "snow", 75: "snow", 77: "snow", 85: "snow", 86: "snow",
         95: "storm", 96: "storm", 99: "storm"}
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def wmo_icon(code):
    try:
        return _ICON.get(int(code), "cloud")
    except (TypeError, ValueError):
        return "cloud"


def _fn(name, desc, props=None, req=()):
    return {"type": "function", "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props or {}, "required": list(req)}}


SPECS = [
    _fn("get_time", "Current local date and time at the station, or in another place by IANA time zone.",
        {"timezone": {"type": "string", "description": "Optional IANA zone such as Europe/London. Omit for local time."}}),
    _fn("get_weather", "Weather at the station's configured location: now, today and tomorrow.",
        {"day": {"type": "string", "enum": ["now", "today", "tomorrow"]}}),
    _fn("convert_units", "Convert a quantity between units (length, weight, volume incl. cups/tbsp/tsp, temperature, speed, area).",
        {"value": {"type": "number"}, "from_unit": {"type": "string"}, "to_unit": {"type": "string"}},
        ("value", "from_unit", "to_unit")),
    _fn("set_timer", "Start a timer on the station; it rings when done, even after this conversation ends.",
        {"seconds": {"type": "integer", "description": "Length in seconds (1 to 86400)."},
         "label": {"type": "string", "description": "Optional short name, e.g. pasta. 'pasta timer' is stored as pasta."}},
        ("seconds",)),
    _fn("list_timers", "List running and ringing timers. Index 1 is the one ringing, or the one finishing soonest."),
    _fn("cancel_timer", "Cancel a running timer or dismiss one that is ringing. Match by name "
        "(pasta, the pasta timer) or by index (1, second). stop, dismiss timer, and stop the timer "
        "dismiss every ringing timer at once. With no name otherwise, dismiss the ringing timer, or the only one.",
        {"label": {"type": "string", "description": "Name, index, or stop. Omit to dismiss the ringing timer or the only one."},
         "index": {"type": "integer", "description": "1-based index from list_timers. Optional."}}),
    _fn("extend_timer", "Add time to a timer by name or index. A ringing timer starts running again.",
        {"seconds": {"type": "integer", "description": "Seconds to add (1 to 86400)."},
         "label": {"type": "string", "description": "Name or index. Omit when only one timer exists."},
         "index": {"type": "integer", "description": "1-based index from list_timers. Optional."}},
        ("seconds",)),
]
NAMES = {s["name"] for s in SPECS}

# ---------------------------------------------------------------- time
def _zone_label(zone):
    """City part of an IANA name: Europe/London -> London, America/New_York -> New York."""
    return (zone.split("/")[-1].replace("_", " ") or zone)[:24]


def _hhmm(t):
    h = t.hour % 12 or 12
    return f"{h}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _offset_vs_local(t):
    """How far `t`'s zone is from the station zone, e.g. '+6 h' or '+5:30 h'."""
    try:
        local_off = t.astimezone(ZoneInfo(TZ)).utcoffset()
    except Exception:
        local_off = t.utcoffset()
    delta = (t.utcoffset() or datetime.timedelta(0)) - (local_off or datetime.timedelta(0))
    mins = int(round(delta.total_seconds() / 60.0))
    sign = "+" if mins >= 0 else "-"
    h, m = divmod(abs(mins), 60)
    return f"{sign}{h} h" if m == 0 else f"{sign}{h}:{m:02d} h"


def time_result(args, now=None):
    """(tool output, time card or None). The card is display fields only."""
    tz = str(args.get("timezone") or "").strip()
    zone = TZ
    if tz:
        if tz not in available_timezones():
            return {"ok": False, "error": "Unknown time zone; use an IANA name like Europe/Paris."}, None
        zone = tz
    t = now.astimezone(ZoneInfo(zone)) if now else datetime.datetime.now(ZoneInfo(zone))
    out = {"ok": True, "timezone": zone, "date": t.strftime("%A, %B %-d, %Y"), "time": t.strftime("%-I:%M %p"),
           "iso": t.isoformat(timespec="minutes")}
    card = {"type": "card", "kind": "time", "zone_label": _zone_label(zone), "hhmm": _hhmm(t),
            "day": _DAYS[t.weekday()], "offset": _offset_vs_local(t)}
    return out, card


def get_time(args, now=None):
    return time_result(args, now)[0]


# ---------------------------------------------------------------- weather
def weather_url():
    global UNITS
    lat, lon, UNITS = location()
    if not (lat and lon):
        return None
    return "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": TZ, "forecast_days": 2, "temperature_unit": UNITS,
        "wind_speed_unit": "mph" if UNITS == "fahrenheit" else "kmh",
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})


def weather_summary(j, day="now"):
    u = "°F" if UNITS == "fahrenheit" else "°C"
    c, d = j.get("current") or {}, j.get("daily") or {}
    out = {"ok": True, "place": PLACE or "the station's location", "units": u}
    if day in ("now", "", None):
        out["now"] = {"temperature": round(c.get("temperature_2m", 0)), "feels_like": round(c.get("apparent_temperature", 0)),
                      "conditions": WMO.get(c.get("weather_code"), "unknown"), "wind": round(c.get("wind_speed_10m", 0)),
                      "humidity_pct": c.get("relative_humidity_2m")}
    for i, name in enumerate(("today", "tomorrow")):
        if day in ("now", "", None, name) and len(d.get("time", [])) > i:
            out[name] = {"high": round(d["temperature_2m_max"][i]), "low": round(d["temperature_2m_min"][i]),
                         "conditions": WMO.get(d["weather_code"][i], "unknown"),
                         "rain_chance_pct": (d.get("precipitation_probability_max") or [None, None])[i]}
    return out


def _round_temp(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(round(v))


def weather_card(j):
    """Display card for a forecast. Numbers and short strings only — no place name and no coordinates."""
    if not isinstance(j, dict):
        return None
    c, d = j.get("current") or {}, j.get("daily") or {}
    if not isinstance(c, dict):
        c = {}
    if not isinstance(d, dict):
        d = {}
    code = c.get("weather_code")
    card = {"type": "card", "kind": "weather", "icon": wmo_icon(code), "units": "°F" if UNITS == "fahrenheit" else "°C"}
    now = _round_temp(c.get("temperature_2m"))
    if now is not None:
        card["now_temp"] = now
    cond = WMO.get(code)
    if cond:
        card["conditions"] = cond[:32]
    times = d.get("time") if isinstance(d.get("time"), list) else []
    highs = d.get("temperature_2m_max") if isinstance(d.get("temperature_2m_max"), list) else []
    lows = d.get("temperature_2m_min") if isinstance(d.get("temperature_2m_min"), list) else []
    for i, name in enumerate(("today", "tomorrow")):
        if i >= len(times):
            continue
        hi, lo = _round_temp(highs[i] if i < len(highs) else None), _round_temp(lows[i] if i < len(lows) else None)
        if hi is not None and lo is not None:
            card[name] = {"high": hi, "low": lo}
    if "now_temp" not in card and "today" not in card:
        return None
    return card


async def _weather(args, http):
    """(tool output, weather card or None). One fetch feeds both; the card is not filtered by `day`."""
    url = weather_url()
    if not url:
        return {"ok": False, "error": "No weather location is configured for the station. Say so briefly."}, None
    try:
        async with http.get(url, timeout=_timeout(6)) as r:
            if r.status != 200:
                raise ValueError(r.status)
            j = json.loads(await r.read())
    except Exception:
        return {"ok": False, "error": "The weather service isn't answering right now."}, None
    summary = weather_summary(j, str(args.get("day") or "now"))
    return summary, (weather_card(j) if summary.get("ok") else None)


async def get_weather(args, http):
    return (await _weather(args, http))[0]


def _timeout(s):
    from aiohttp import ClientTimeout
    return ClientTimeout(total=s)


# ---------------------------------------------------------------- units
_U = {}   # alias -> (dimension, factor to SI base)
_DISP = {}  # alias -> short label drawn on the card
def _add(dim, factor, disp, *names):
    for n in names:
        _U[n] = (dim, factor)
        _DISP[n] = disp

_add("length", 1, "m", "m", "meter", "meters", "metre", "metres")
_add("length", 0.001, "mm", "mm", "millimeter", "millimeters")
_add("length", 0.01, "cm", "cm", "centimeter", "centimeters")
_add("length", 1000, "km", "km", "kilometer", "kilometers", "kilometre", "kilometres")
_add("length", 0.0254, "in", "in", "inch", "inches")
_add("length", 0.3048, "ft", "ft", "foot", "feet")
_add("length", 0.9144, "yd", "yd", "yard", "yards")
_add("length", 1609.344, "mi", "mi", "mile", "miles")
_add("mass", 1, "kg", "kg", "kilogram", "kilograms", "kilo", "kilos")
_add("mass", 0.001, "g", "g", "gram", "grams")
_add("mass", 1e-6, "mg", "mg", "milligram", "milligrams")
_add("mass", 0.45359237, "lb", "lb", "lbs", "pound", "pounds")
_add("mass", 0.028349523125, "oz", "oz", "ounce", "ounces")
_add("mass", 6.35029318, "st", "st", "stone", "stones")
_add("volume", 0.001, "L", "l", "liter", "liters", "litre", "litres")
_add("volume", 1e-6, "mL", "ml", "milliliter", "milliliters", "millilitre", "millilitres")
_add("volume", 0.0002365882365, "cup", "cup", "cups")
_add("volume", 0.00001478676478125, "tbsp", "tbsp", "tablespoon", "tablespoons")
_add("volume", 0.00000492892159375, "tsp", "tsp", "teaspoon", "teaspoons")
_add("volume", 0.0000295735295625, "fl oz", "floz", "fl oz", "fluid ounce", "fluid ounces")
_add("volume", 0.000473176473, "pt", "pint", "pints", "pt")
_add("volume", 0.000946352946, "qt", "quart", "quarts", "qt")
_add("volume", 0.003785411784, "gal", "gal", "gallon", "gallons")
_add("speed", 1, "m/s", "m/s", "meters per second")
_add("speed", 1 / 3.6, "km/h", "km/h", "kph", "kmh", "kilometers per hour")
_add("speed", 0.44704, "mph", "mph", "miles per hour")
_add("speed", 0.514444, "kn", "knot", "knots", "kn")
_add("area", 1, "m²", "m2", "sq m", "square meter", "square meters")
_add("area", 0.09290304, "ft²", "ft2", "sq ft", "square foot", "square feet")
_add("area", 4046.8564224, "acre", "acre", "acres")
_add("area", 10000, "ha", "ha", "hectare", "hectares")
_add("area", 1e6, "km²", "km2", "sq km", "square kilometer", "square kilometers")
_add("area", 2589988.110336, "mi²", "mi2", "sq mi", "square mile", "square miles")
TEMP = {"c": "c", "celsius": "c", "°c": "c", "degc": "c", "f": "f", "fahrenheit": "f", "°f": "f", "degf": "f",
        "k": "k", "kelvin": "k"}
_TEMP_DISP = {"c": "°C", "f": "°F", "k": "K"}


def _norm(u):
    u = re.sub(r"\s+", " ", str(u or "").strip().lower().replace("degrees ", "").replace("degree ", ""))
    return u.replace("²", "2").rstrip(".")


def convert_units(args):
    try:
        v = float(args.get("value"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "I need a number to convert."}
    a, b = _norm(args.get("from_unit")), _norm(args.get("to_unit"))
    if a in TEMP and b in TEMP:
        c = {"c": v, "f": (v - 32) * 5 / 9, "k": v - 273.15}[TEMP[a]]
        r = {"c": c, "f": c * 9 / 5 + 32, "k": c + 273.15}[TEMP[b]]
    elif a in _U and b in _U:
        if _U[a][0] != _U[b][0]:
            return {"ok": False, "error": f"Can't convert {_U[a][0]} to {_U[b][0]}."}
        r = v * _U[a][1] / _U[b][1]
    else:
        return {"ok": False, "error": "I don't know one of those units."}
    rr = round(r, 2) if abs(r) >= 1 else float(f"{r:.3g}")
    return {"ok": True, "value": v, "from_unit": args.get("from_unit"), "result": rr, "to_unit": args.get("to_unit")}


def _fmt_num(n):
    """Short decimal for a card. Integers stay integers; tiny values keep 3 significant digits."""
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        return ""
    if isinstance(n, float) and (n != n or abs(n) == float("inf")):
        return ""
    if isinstance(n, float) and abs(n) < 1 and n != 0:
        return f"{n:.3g}"
    s = f"{n:.2f}".rstrip("0").rstrip(".") if isinstance(n, float) else str(n)
    return s if s not in ("", "-") else "0"


def _disp_unit(raw):
    n = _norm(raw)
    if n in TEMP:
        return _TEMP_DISP[TEMP[n]]
    return _DISP.get(n, " ".join(str(raw or "").split())[:12])


def convert_result(args):
    """(tool output, convert card or None). Card fields are formatted strings."""
    out = convert_units(args)
    if not out.get("ok"):
        return out, None
    card = {"type": "card", "kind": "convert", "value": _fmt_num(out["value"]), "from_unit": _disp_unit(out["from_unit"]),
            "result": _fmt_num(out["result"]), "to_unit": _disp_unit(out["to_unit"])}
    if not all(card[k] for k in ("value", "from_unit", "result", "to_unit")):
        return out, None
    return out, card


# ---------------------------------------------------------------- timers
# The station rings and shows them. The relay keeps the same book in a state file so a relay
# restart still knows the names, and a session whose station sent no list can hand them back.
# `now` is the caller's clock. Production passes MonoClock.now() so a wall-clock step does not
# shrink or stretch a running timer; across a process restart the file uses wall time, because
# the monotonic clock resets.
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8}
# A bare "stop" (no following word) is the dismiss command, not a timer name.
_LEADING_REF = re.compile(r"^(?:please\s+)?(?:stop|dismiss|cancel|clear|end|silence)(?:\s+|$)")
_ARTICLE = re.compile(r"^(?:the|a|an|my)\s+")
_ID_RE = re.compile(r"[a-z0-9]{4,12}")


def timer_state_path():
    """Relay timer book. GROK_TIMER_STATE wins; otherwise under GROK_STATE_DIR."""
    return E("GROK_TIMER_STATE", os.path.join(E("GROK_STATE_DIR", "/var/lib/grok-station"), "state", "timers.json"))


class MonoClock:
    """Wall-shaped clock that advances with time.monotonic(), not time.time().

    A NTP step moves time.time() and leaves this clock alone. Construct with callables in tests.
    """

    def __init__(self, wall=None, mono=None):
        self._wall = time.time if wall is None else wall
        self._mono = time.monotonic if mono is None else mono
        self._m0 = self._mono()
        self._w0 = self._wall()

    def now(self):
        return self._w0 + (self._mono() - self._m0)


def parse_timers(raw, now):
    """Pi query ?timers=<json [{id,label,left,ringing?,duration?}]> -> {id: record}.

    Malformed entries are dropped. A ringing timer is kept even when left is 0.
    """
    out = {}
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return out
    for t in items[:MAX_TIMERS] if isinstance(items, list) else []:
        if not isinstance(t, dict) or not _ID_RE.fullmatch(str(t.get("id", ""))):
            continue
        left = t.get("left")
        if isinstance(left, bool) or not isinstance(left, (int, float)):
            continue
        left = max(-MAX_TIMER_S, min(MAX_TIMER_S, float(left)))
        ringing = bool(t.get("ringing"))
        if left <= 0 and not ringing:
            continue
        dur = t.get("duration")
        if isinstance(dur, bool) or not isinstance(dur, (int, float)) or not 1 <= float(dur) <= MAX_TIMER_S:
            dur = max(left, 1.0)
        out[t["id"]] = {"label": clean_label(t.get("label")), "due": now + max(0.0, left),
                        "duration": float(dur), "ringing": ringing}
    return out


def clean_label(s):
    s = " ".join(re.sub(r"[^\w '\-]", " ", str(s or "")).split())[:24]
    return s or "timer"


def say_left(sec):
    sec = max(0, int(round(sec)))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    parts = [f"{h} hour{'s' * (h != 1)}"] * bool(h) + [f"{m} minute{'s' * (m != 1)}"] * bool(m) + [f"{s} second{'s' * (s != 1)}"] * bool(s and not h)
    return ", ".join(parts) or "0 seconds"


def _default_name(timers):
    """First unnamed timer is Timer; the next are Timer 2, Timer 3, ..."""
    used = {str(t.get("label", "")).lower() for t in timers.values() if isinstance(t, dict)}
    if "timer" not in used:
        return "Timer"
    n = 2
    while f"timer {n}" in used and n < 100:
        n += 1
    return f"Timer {n}"


def _stored_label(raw, timers):
    """Blank or plain 'timer' gets a unique default. 'pasta timer' is stored as pasta."""
    if raw is None or not str(raw).strip():
        return _default_name(timers)
    label = clean_label(raw)
    stripped = re.sub(r"\s+timer$", "", label, flags=re.I).strip()
    if not stripped or stripped.lower() == "timer":
        return _default_name(timers)
    return stripped[:24]


def _seconds(args, key="seconds"):
    v = args.get(key) if isinstance(args, dict) else None
    if isinstance(v, bool):
        return None
    try:
        sec = int(v)
    except (TypeError, ValueError):
        return None
    return sec


def _index_arg(args):
    if not isinstance(args, dict) or "index" not in args or args.get("index") in (None, ""):
        return None
    v = args.get("index")
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _core_phrase(text):
    """Lowercased ref with leading stop/dismiss/the stripped. '#' is dropped so '#2' is '2'."""
    raw = " ".join(re.sub(r"[^\w '\-#]", " ", str(text or "")).split()).lower().replace("#", "")
    while True:
        nxt = _ARTICLE.sub("", _LEADING_REF.sub("", raw)).strip()
        if nxt == raw:
            return raw
        raw = nxt


def parse_timer_ref(text):
    """('any', None), ('index', n), or ('name', core).

    'stop' and 'stop the timer' are any. '2', 'timer 2', 'second' and 'the second one' are indexes.
    'stop the pasta timer' is the name pasta. Matching prefers an exact label over an index.
    """
    raw = _core_phrase(text)
    if raw in ("", "it", "that", "this", "timer", "alarm", "one"):
        return ("any", None)
    if raw in _ORDINALS:
        return ("index", _ORDINALS[raw])
    m = re.fullmatch(r"(first|second|third|fourth|fifth|sixth|seventh|eighth)\s+(?:one|timer)", raw)
    if m:
        return ("index", _ORDINALS[m.group(1)])
    m = re.fullmatch(r"(?:timer|number)?\s*(\d+)", raw)
    if m:
        return ("index", int(m.group(1)))
    name = re.sub(r"\s+timer$", "", raw).strip()
    name = _ARTICLE.sub("", name).strip()
    if not name or name == "timer":
        return ("any", None)
    return ("name", name)


def _names_match(label, name):
    a = str(label or "").lower().strip()
    b = str(name or "").lower().strip()
    if not a or not b:
        return False
    a2 = re.sub(r"\s+timer$", "", a).strip()
    b2 = re.sub(r"\s+timer$", "", b).strip()
    return a == b or a2 == b or a == b2 or bool(a2) and a2 == b2


def note_due(timers, now):
    """Mark a timer ringing once its deadline has passed, so stop and list still find it.

    The station may be mid-conversation and not have chimed yet. More than an hour late matches
    the station's quiet drop. Returns (dropped ids, whether any record changed).
    """
    dropped, changed = [], False
    for k, t in list(timers.items()):
        if not isinstance(t, dict):
            continue
        due = t.get("due")
        if isinstance(due, bool) or not isinstance(due, (int, float)):
            continue
        late = float(now) - float(due)
        if late < 0:
            continue
        if late > 3600:
            timers.pop(k, None)
            dropped.append(str(k))
            changed = True
            continue
        if not t.get("ringing"):
            t["ringing"] = True
            changed = True
    return dropped, changed


def live_rows(timers, now):
    """Ringing first, then soonest deadline. note_due runs first, so a just-finished timer stays listed."""
    note_due(timers, now)
    rows = []
    for k, t in timers.items():
        if not isinstance(t, dict) or isinstance(t.get("due"), bool) or not isinstance(t.get("due"), (int, float)):
            continue
        left = t["due"] - now
        ringing = bool(t.get("ringing"))
        if left <= 0 and not ringing:
            continue
        rows.append((0 if ringing else 1, t["due"], str(t.get("label", "")).lower(), str(k)))
    rows.sort()
    return [(k, timers[k]) for *_, k in rows]


def _exact_ids(rows, text):
    return [k for k, t in rows if _names_match(t.get("label", ""), text)]


def resolve_timer(args, timers, now):
    """([ids], error or None). One id is a hit. Several ids means the caller should ask which."""
    rows = live_rows(timers, now)
    if not rows:
        return [], "No timer is running."
    idx = _index_arg(args)
    if idx is not None:
        if 1 <= idx <= len(rows):
            return [rows[idx - 1][0]], None
        return [], "No timer like that is running."
    kind, val = parse_timer_ref(args.get("label") if isinstance(args, dict) else None)
    if kind == "any":
        ringing = [k for k, t in rows if t.get("ringing")]
        if ringing:
            return [ringing[0]], None
        if len(rows) == 1:
            return [rows[0][0]], None
        return [k for k, _t in rows], "Several are running; ask which one."
    if kind == "index":
        named = _exact_ids(rows, _core_phrase(args.get("label")))
        if named:
            return named, None
        if 1 <= val <= len(rows):
            return [rows[val - 1][0]], None
        return [], "No timer like that is running."
    hits = _exact_ids(rows, val)
    if not hits:
        hits = [k for k, t in rows if val in str(t.get("label", "")).lower()]
    return hits, None


def _one_hit(hits, timers, err):
    labels = [timers[k].get("label", "timer") for k in hits if k in timers]
    if len(hits) > 1:
        return None, {"ok": False, "error": err or "Several are running; ask which one.", "labels": labels}, None
    if err and not hits:
        return None, {"ok": False, "error": err}, None
    if not hits:
        return None, {"ok": False, "error": "No timer like that is running."}, None
    return hits[0], None, None


def set_timer(args, timers, now):
    """Returns (tool output, message for the Pi or None)."""
    if not isinstance(args, dict):
        args = {}
    sec = _seconds(args)
    if sec is None:
        return {"ok": False, "error": "How long should the timer be?"}, None
    if not 1 <= sec <= MAX_TIMER_S:
        return {"ok": False, "error": "Timers can be 1 second to 24 hours."}, None
    note_due(timers, now)
    if len(timers) >= MAX_TIMERS:
        return {"ok": False, "error": "Eight timers are already running."}, None
    label = _stored_label(args.get("label") if isinstance(args, dict) else None, timers)
    tid = secrets.token_hex(3)
    timers[tid] = {"label": label, "due": now + sec, "duration": sec, "ringing": False}
    return {"ok": True, "label": label, "length": say_left(sec)}, {"type": "timer_set", "id": tid, "seconds": sec, "label": label}


def list_timers(timers, now):
    rows = live_rows(timers, now)
    out = []
    for i, (k, t) in enumerate(rows, 1):
        left = t["due"] - now
        ringing = bool(t.get("ringing"))
        out.append({"index": i, "label": t.get("label") or "timer",
                    "left": "ringing" if ringing else say_left(left), "ringing": ringing})
    return {"ok": True, "timers": out}


_ALL_RINGING = ("stop", "stop the timer", "dismiss timer", "dismiss the timer")


def _fold_utt(text):
    """Same folding as a recipe command: case, a leading hey-name, and a trailing please."""
    t = str(text or "").replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    t = " ".join(t.casefold().split())
    t = re.sub(r"^(?:hey [a-z0-9][a-z0-9' -]{0,40}?)[, ]+", "", t).strip()
    t = t.strip(" \t.!?")
    if t.endswith(" please"):
        t = t[: -len(" please")].strip(" \t.!?")
    return t


def _title_name(label):
    """First letter only: the spoken stop line uses "Garlic bread", not "Garlic Bread"."""
    text = " ".join(str(label or "timer").split())[:24]
    return (text[:1].upper() + text[1:]) if text else "Timer"


def stopped_say(names):
    """Grok's confirmation. "Stopped Orzo, Garlic bread and Timer." """
    names = [str(n) for n in names if str(n).strip()]
    if not names:
        return ""
    if len(names) == 1:
        return f"Stopped {names[0]}."
    if len(names) == 2:
        return f"Stopped {names[0]} and {names[1]}."
    return "Stopped " + ", ".join(names[:-1]) + " and " + names[-1] + "."


def _ringing_rows(timers, now):
    """Ringing timers, earliest deadline first. note_due runs inside live_rows."""
    return [(k, t) for k, t in live_rows(timers, now) if isinstance(t, dict) and t.get("ringing")]


def clear_ringing_utterance(text, timers, now):
    """Dismiss ringing timers from a whole utterance.

    "stop", "dismiss timer", and "stop the timer" clear every ringing timer.
    Bare "dismiss" clears the one that ended first (a recipe chip waits until none are ringing).
    "clear that" and a dismiss with nothing ringing return None so the chip path still runs.
    Running timers stay.
    """
    if not isinstance(timers, dict):
        return None
    utt = _fold_utt(text)
    if utt not in _ALL_RINGING and utt != "dismiss":
        return None
    ringing = _ringing_rows(timers, now)
    if not ringing:
        return None
    picked = ringing if utt in _ALL_RINGING else ringing[:1]
    ids, names, messages = [], [], []
    for k, t in picked:
        ids.append(k)
        names.append(_title_name(t.get("label")))
        messages.append({"type": "timer_cancel", "id": k})
        timers.pop(k, None)
    return {"ids": ids, "messages": messages, "say": stopped_say(names), "all": utt in _ALL_RINGING}


def pi_timer_messages(to_pi):
    """One Pi message, or the list a stop-all cancel returns."""
    msgs = to_pi if isinstance(to_pi, list) else ([to_pi] if isinstance(to_pi, dict) else [])
    return [m for m in msgs if isinstance(m, dict) and m.get("type") in ("card", "timer_set", "timer_cancel")]


def cancel_timer(args, timers, now):
    args = args if isinstance(args, dict) else {}
    if _fold_utt(args.get("label")) in _ALL_RINGING:
        ringing = _ringing_rows(timers, now)
        if len(ringing) > 1:
            raw, names, messages = [], [], []
            for k, t in ringing:
                raw.append(t.get("label") or "timer")
                names.append(_title_name(t.get("label")))
                messages.append({"type": "timer_cancel", "id": k})
                timers.pop(k, None)
            return {"ok": True, "cancelled": raw, "say": stopped_say(names)}, messages
    hits, err = resolve_timer(args, timers, now)
    tid, bad, _ = _one_hit(hits, timers, err)
    if bad:
        return bad, None
    t = timers.pop(tid)
    return {"ok": True, "cancelled": t.get("label") or "timer"}, {"type": "timer_cancel", "id": tid}


def extend_timer(args, timers, now):
    """Add seconds. The station message is timer_set for the new remaining length, same id."""
    sec = _seconds(args)
    if sec is None or not 1 <= sec <= MAX_TIMER_S:
        return {"ok": False, "error": "How much time should I add?"}, None
    hits, err = resolve_timer(args if isinstance(args, dict) else {}, timers, now)
    tid, bad, _ = _one_hit(hits, timers, err)
    if bad:
        return bad, None
    t = timers[tid]
    left = max(0.0, t["due"] - now)
    new_left = left + sec
    if new_left > MAX_TIMER_S:
        return {"ok": False, "error": "Timers can be 1 second to 24 hours."}, None
    whole = max(1, int(round(new_left)))
    t["due"] = now + whole
    t["duration"] = whole
    t["ringing"] = False
    return ({"ok": True, "label": t.get("label") or "timer", "added": say_left(sec), "left": say_left(whole)},
            {"type": "timer_set", "id": tid, "seconds": whole, "label": t.get("label") or "timer"})


def save_timer_state(path, timers, now, wall_now=None):
    """Write the book. Remaining time is `due - now` (the caller's clock) stamped with wall_now.

    An empty book does not create a file that was not already there. Returns False on OSError.
    """
    wall_now = time.time() if wall_now is None else wall_now
    doc_timers = {}
    for tid, t in timers.items():
        if not _ID_RE.fullmatch(str(tid)) or not isinstance(t, dict):
            continue
        due = t.get("due")
        if isinstance(due, bool) or not isinstance(due, (int, float)):
            continue
        remain = float(due) - float(now)
        ringing = bool(t.get("ringing"))
        if remain <= 0 and not ringing:
            continue
        if ringing and remain < -3600:
            continue
        dur = t.get("duration")
        if isinstance(dur, bool) or not isinstance(dur, (int, float)) or not 1 <= float(dur) <= MAX_TIMER_S:
            dur = max(remain, 1.0)
        doc_timers[str(tid)] = {"label": clean_label(t.get("label")), "remain": remain,
                                "duration": float(dur), "ringing": ringing}
    if not doc_timers and not os.path.exists(path):
        return True
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"saved_wall": float(wall_now), "timers": doc_timers}, f)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def load_timer_state(path, wall_now):
    """{id: {label, remain, duration, ringing}} at wall_now, or {} if the file is missing or bad.

    Remaining shrinks by the wall time since the save. The monotonic clock does not survive a
    restart, so this is the restart rule; MonoClock covers a running process.
    """
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    saved = doc.get("saved_wall")
    if isinstance(saved, bool) or not isinstance(saved, (int, float)):
        return {}
    elapsed = float(wall_now) - float(saved)
    out = {}
    items = doc.get("timers")
    if not isinstance(items, dict):
        return {}
    for tid, t in items.items():
        if len(out) >= MAX_TIMERS:
            break
        if not _ID_RE.fullmatch(str(tid)) or not isinstance(t, dict):
            continue
        remain = t.get("remain")
        if isinstance(remain, bool) or not isinstance(remain, (int, float)):
            continue
        remain = float(remain) - elapsed
        ringing = bool(t.get("ringing"))
        if remain <= 0 and not ringing:
            continue
        if ringing and remain < -3600:
            continue
        dur = t.get("duration")
        if isinstance(dur, bool) or not isinstance(dur, (int, float)) or not 1 <= float(dur) <= MAX_TIMER_S:
            dur = max(remain, 1.0)
        out[str(tid)] = {"label": clean_label(t.get("label")), "remain": remain,
                         "duration": float(dur), "ringing": ringing}
    return out


def restore_timers(path, now, wall_now):
    """In-memory book from the state file, with deadlines on `now` (a MonoClock in production)."""
    out = {}
    for tid, t in load_timer_state(path, wall_now).items():
        out[tid] = {"label": t["label"], "due": now + t["remain"], "duration": t["duration"], "ringing": t["ringing"]}
    return out


def timer_restore_messages(timers, now):
    """timer_set messages that hand a restored book to a station which sent no list of its own.

    seconds 0 means already ringing: the station shows it and does not start a new countdown.
    """
    msgs = []
    for tid, rec in timers.items():
        if not isinstance(rec, dict):
            continue
        due = rec.get("due")
        if isinstance(due, bool) or not isinstance(due, (int, float)):
            left = 0
        else:
            left = int(round(float(due) - float(now)))
        label = rec.get("label") or "timer"
        if rec.get("ringing") or left < 1:
            msgs.append({"type": "timer_set", "id": tid, "seconds": 0, "label": label})
        else:
            msgs.append({"type": "timer_set", "id": tid, "seconds": left, "label": label})
    return msgs


async def run(name, args, http, timers, now):
    """Dispatch one tool call -> (output dict, optional message for the Pi).

    The Pi message is a display card for time / weather / units, or timer_set / timer_cancel.
    extend_timer reuses timer_set with the same id and the new remaining seconds.
    Failed calls return None for the message, so the station shows nothing.
    """
    if name == "get_time":
        return time_result(args, None)
    if name == "get_weather":
        return await _weather(args, http)
    if name == "convert_units":
        return convert_result(args)
    if name == "set_timer":
        return set_timer(args, timers, now)
    if name == "list_timers":
        return list_timers(timers, now), None
    if name == "cancel_timer":
        return cancel_timer(args, timers, now)
    if name == "extend_timer":
        return extend_timer(args, timers, now)
    return {"ok": False, "error": "unknown tool"}, None
