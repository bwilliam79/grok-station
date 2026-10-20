"""Station tools for every voice persona (T0-equivalent): read-only lookups plus timers that live on the station.
Nothing here sends, posts, buys, books or changes anything outside the station, and no tool needs a credential.
  get_time       local date/time (TZ_NAME), or the time in another IANA zone
  get_weather    Open-Meteo (no key) for the configured location WEATHER_LAT/WEATHER_LON (WEATHER_PLACE names it)
  convert_units  length, weight, volume (incl. cooking), temperature, speed, area
  set_timer / list_timers / cancel_timer   the station rings; the relay only passes the request along

run() returns (tool output, message for the Pi). A successful time, weather or unit lookup also sends a small
display card ({"type":"card",...}); timers still send timer_set / timer_cancel. A failed tool sends nothing.
The weather card has no place name and no coordinates — those stay in the tool output for the spoken answer only.
"""
import datetime, json, os, re, secrets, urllib.parse
from zoneinfo import ZoneInfo, available_timezones

E = os.environ.get
TZ = E("TZ_NAME", "America/Chicago")
LAT, LON, PLACE = E("WEATHER_LAT", ""), E("WEATHER_LON", ""), E("WEATHER_PLACE", "")
UNITS = E("WEATHER_UNITS", "fahrenheit")
# Single device: the admin page saves the location in the station env file (GROK_STATION_LAT/LON/UNITS).
# Read it on every weather call (cached by mtime), so a save takes effect without a relay restart.
STATION_ENV = E("STATION_ENV_FILE", "/etc/grok-station/env")
_station_env = {"mtime": None, "vals": {}}


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
    except OSError:
        _station_env.update(mtime=None, vals={})
    v = _station_env["vals"]
    lat = LAT or v.get("GROK_STATION_LAT", "")
    lon = LON or v.get("GROK_STATION_LON", "")
    units = E("WEATHER_UNITS") or v.get("GROK_STATION_UNITS") or "fahrenheit"
    if not (re.fullmatch(r"-?\d{1,2}(\.\d+)?", lat or "") and re.fullmatch(r"-?\d{1,3}(\.\d+)?", lon or "")):
        lat = lon = ""
    return lat, lon, units if units in ("fahrenheit", "celsius") else "fahrenheit"
MAX_TIMERS, MAX_TIMER_S = 10, 24 * 3600

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
         "label": {"type": "string", "description": "Short name, e.g. pasta. Optional."}}, ("seconds",)),
    _fn("list_timers", "List the station's running timers and the time left on each."),
    _fn("cancel_timer", "Cancel a running timer by its label, or the only one running.",
        {"label": {"type": "string", "description": "Which timer. Omit when only one is running."}}),
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


# ---------------------------------------------------------------- timers (state lives on the station)
def parse_timers(raw, now):
    """Pi query ?timers=<json [{id,label,left}]> -> {id: {label, due}}; anything malformed is dropped."""
    out = {}
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return out
    for t in items[:MAX_TIMERS] if isinstance(items, list) else []:
        if isinstance(t, dict) and re.fullmatch(r"[a-z0-9]{4,12}", str(t.get("id", ""))) and isinstance(t.get("left"), (int, float)):
            out[t["id"]] = {"label": clean_label(t.get("label")), "due": now + max(0, min(MAX_TIMER_S, t["left"]))}
    return out


def clean_label(s):
    s = " ".join(re.sub(r"[^\w '\-]", " ", str(s or "")).split())[:24]
    return s or "timer"


def say_left(sec):
    sec = int(round(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    parts = [f"{h} hour{'s' * (h != 1)}"] * bool(h) + [f"{m} minute{'s' * (m != 1)}"] * bool(m) + [f"{s} second{'s' * (s != 1)}"] * bool(s and not h)
    return ", ".join(parts) or "0 seconds"


def set_timer(args, timers, now):
    """Returns (tool output, message for the Pi or None)."""
    try:
        sec = int(args.get("seconds"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "How long should the timer be?"}, None
    if not 1 <= sec <= MAX_TIMER_S:
        return {"ok": False, "error": "Timers can be 1 second to 24 hours."}, None
    if len(timers) >= MAX_TIMERS:
        return {"ok": False, "error": "Ten timers are already running."}, None
    label = clean_label(args.get("label"))
    tid = secrets.token_hex(3)
    timers[tid] = {"label": label, "due": now + sec}
    return {"ok": True, "label": label, "length": say_left(sec)}, {"type": "timer_set", "id": tid, "seconds": sec, "label": label}


def list_timers(timers, now):
    live = sorted(((t["due"] - now, t["label"]) for t in timers.values() if t["due"] > now))
    return {"ok": True, "timers": [{"label": l, "left": say_left(s)} for s, l in live]}


def cancel_timer(args, timers, now):
    live = {k: t for k, t in timers.items() if t["due"] > now}
    want = clean_label(args.get("label")) if args.get("label") else None
    hits = [k for k, t in live.items() if want is None or t["label"].lower() == want.lower()]
    if not hits:
        return {"ok": False, "error": "No timer like that is running."}, None
    if len(hits) > 1:
        return {"ok": False, "error": "Several are running; ask which one.", "labels": [live[k]["label"] for k in hits]}, None
    t = timers.pop(hits[0])
    return {"ok": True, "cancelled": t["label"]}, {"type": "timer_cancel", "id": hits[0]}


async def run(name, args, http, timers, now):
    """Dispatch one tool call -> (output dict, optional message for the Pi).

    The Pi message is a display card for time / weather / units, or timer_set / timer_cancel.
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
    return {"ok": False, "error": "unknown tool"}, None
