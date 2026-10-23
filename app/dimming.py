#!/usr/bin/env python3
"""Screen dimming for the station display. Stdlib only. No network.

Modes: off, schedule (clock times, including a window that crosses midnight),
sun (NOAA sunrise/sunset from latitude and longitude, plus minute offsets),
and auto-sensor (an ambient light sensor when the kernel already exposes one).

Sensors are probed read-only. A missing sensor is normal: auto-sensor then
uses the schedule, then sunrise, then the day level. Brightness is a percent.
The display writes /sys/class/backlight when that file is writable, and
otherwise composites a black overlay (the same math as a CSS opacity layer).
"""
import math
import os
import re
import stat
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MODES = ("off", "schedule", "sun", "auto-sensor")
# Official sunrise/sunset zenith: geometric 90° plus refraction and the solar radius.
ZENITH = 90.833
# Lux below this is night; at or above DAY_LUX is the day level. Between them, linear.
NIGHT_LUX = 20.0
DAY_LUX = 80.0
DEFAULT_DAY = 90
DEFAULT_NIGHT = 20
_CHIPS = ("bh1750", "tsl2561", "veml7700")
# Read addresses only. A successful read does not change the chip's mode.
_I2C_ADDRS = ((0x23, "bh1750"), (0x5C, "bh1750"), (0x39, "tsl2561"), (0x29, "tsl2561"),
              (0x49, "tsl2561"), (0x10, "veml7700"))
_I2C_RDWR = 0x0707
_I2C_M_RD = 0x0001

PASS_KEYS = ("GROK_STATION_HOLD", "GROK_STATION_NIGHT", "GROK_STATION_UNITS", "GROK_STATION_FADE")
DIM_KEYS = ("GROK_STATION_DIM_MODE", "GROK_STATION_DIM_DAY", "GROK_STATION_DIM_NIGHT",
            "GROK_STATION_DIM_FROM", "GROK_STATION_DIM_TO", "GROK_STATION_DIM_LAT",
            "GROK_STATION_DIM_LON", "GROK_STATION_DIM_SUNRISE", "GROK_STATION_DIM_SUNSET")

OVERLAY = {"level": None}  # None, or 0-99: black-overlay strength the framebuffer should apply


def set_overlay(level):
    """Remember the software dim. None or 100 or more clears it. Hardware backlight wins when it works."""
    if level is None or level >= 100:
        OVERLAY["level"] = None
    else:
        OVERLAY["level"] = max(0, int(level))


def overlay_level():
    return OVERLAY["level"]


def overlay_alpha(level):
    """0-255 alpha of a black layer. None and 100 are fully clear. 0 is solid black.

    This is the CSS rule `background:#000; opacity:(100-level)/100` in pixel terms.
    """
    if level is None:
        return 0
    try:
        n = int(level)
    except (TypeError, ValueError):
        return 0
    n = max(0, min(100, n))
    return int(round((100 - n) * 255 / 100))


def overlay_opacity(level):
    """CSS opacity of that black layer, 0 to 1."""
    return round(overlay_alpha(level) / 255, 4)


# ---------------------------------------------------------------- sensors (read-only)


def _read_text(path, limit=64):
    try:
        with open(path) as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _read_float(path):
    raw = _read_text(path).strip()
    if not re.fullmatch(r"[+-]?\d+(\.\d+)?([eE][+-]?\d+)?", raw or ""):
        return None
    try:
        n = float(raw)
    except ValueError:
        return None
    if n != n or abs(n) == float("inf"):
        return None
    return n


def _chip_name(text):
    raw = (text or "").strip().lower()
    for chip in _CHIPS:
        if chip in raw:
            return chip
    safe = re.sub(r"[^a-z0-9._-]", "", raw)[:32]
    return safe or "light"


def _iio_dirs(root):
    base = os.path.join(root, "sys", "bus", "iio", "devices")
    if not os.path.isdir(base):
        return
    for name in sorted(os.listdir(base)):
        if not re.fullmatch(r"[A-Za-z0-9:_.-]{1,64}", name):
            continue
        dev = os.path.join(base, name)
        if os.path.isdir(dev):
            yield dev


def _has_illuminance(dev):
    try:
        names = os.listdir(dev)
    except OSError:
        return False
    return any(n.startswith("in_illuminance") for n in names)


def read_lux(dev):
    """Lux from one IIO device directory, or None. Prefers the processed input, else raw * scale."""
    for name in ("in_illuminance_input", "in_illuminance0_input"):
        n = _read_float(os.path.join(dev, name))
        if n is not None:
            return max(0.0, n)
    raw = None
    for name in ("in_illuminance_raw", "in_illuminance0_raw"):
        raw = _read_float(os.path.join(dev, name))
        if raw is not None:
            break
    if raw is None:
        return None
    scale = _read_float(os.path.join(dev, "in_illuminance_scale"))
    if scale is None:
        scale = 1.0
    lux = raw * scale
    if lux != lux or abs(lux) == float("inf") or lux < -1:
        return None
    return max(0.0, lux)


def _detect_iio(root):
    found = []
    for dev in _iio_dirs(root):
        if not _has_illuminance(dev):
            continue
        found.append({"source": "iio", "name": _chip_name(_read_text(os.path.join(dev, "name"))), "dir": dev})
    return found


def _detect_i2c_sysfs(root):
    base = os.path.join(root, "sys", "bus", "i2c", "devices")
    found = []
    if not os.path.isdir(base):
        return found
    for name in sorted(os.listdir(base)):
        if not re.fullmatch(r"[A-Za-z0-9:_.-]{1,64}", name):
            continue
        chip = _chip_name(_read_text(os.path.join(base, name, "name")))
        if chip not in _CHIPS:
            continue
        found.append({"source": "i2c", "name": chip, "dir": os.path.join(base, name)})
    return found


def _i2c_read_byte(fd, addr):
    """One read byte. False when the address does not answer. Never writes a register."""
    try:
        import ctypes
    except ImportError:
        return False
    buf = ctypes.create_string_buffer(1)

    class Msg(ctypes.Structure):
        _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16), ("len", ctypes.c_uint16),
                    ("buf", ctypes.c_char_p)]

    class Rdwr(ctypes.Structure):
        _fields_ = [("msgs", ctypes.POINTER(Msg)), ("nmsgs", ctypes.c_uint32)]

    msg = Msg(addr, _I2C_M_RD, 1, ctypes.cast(buf, ctypes.c_char_p))
    data = Rdwr(ctypes.pointer(msg), 1)
    try:
        import fcntl
        fcntl.ioctl(fd, _I2C_RDWR, data)
    except OSError:
        return False
    return True


def _detect_i2c_dev(root):
    """Best-effort read of known sensor addresses on /dev/i2c-N character devices."""
    base = os.path.join(root, "dev")
    found = []
    if not os.path.isdir(base):
        return found
    buses = [n for n in sorted(os.listdir(base)) if re.fullmatch(r"i2c-\d{1,3}", n)]
    for name in buses[:4]:
        path = os.path.join(base, name)
        try:
            if not stat.S_ISCHR(os.stat(path).st_mode):
                continue
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            continue
        try:
            for addr, chip in _I2C_ADDRS:
                if _i2c_read_byte(fd, addr):
                    found.append({"source": "i2c", "name": chip, "dir": path})
                    break
        finally:
            os.close(fd)
    return found


def detect_sensors(root="/"):
    """Ambient light sensors under a sysfs root. Empty when nothing is there. Never raises.

    `root` is the filesystem root, so tests can point it at a fake tree. Only names and
    illuminance files are read. /dev/i2c-* is probed with a single read, and only when
    it is a character device.
    """
    try:
        found = _detect_iio(root) + _detect_i2c_sysfs(root)
        if not found:
            found.extend(_detect_i2c_dev(root))
    except OSError:
        return []
    return found


def current_lux(root="/"):
    """First readable IIO illuminance, or None."""
    try:
        for dev in _detect_iio(root):
            lux = read_lux(dev["dir"])
            if lux is not None:
                return lux
    except OSError:
        return None
    return None


def hardware_status(root="/"):
    """What the admin page may show. Names and a lux reading only, no device paths.

    `lux` is None when nothing is reporting illuminance. An I2C name with no IIO file is
    that case: the chip was seen, but there is no reading to dim from.
    """
    sensors = detect_sensors(root)
    lights = backlight_devices(root)
    sensor = {"name": sensors[0]["name"], "source": sensors[0]["source"]} if sensors else None
    light = {"name": lights[0]["name"], "max": lights[0]["max"]} if lights else None
    lux = current_lux(root)
    if lux is not None:
        lux = round(float(lux), 1)
    return {"sensor": sensor, "backlight": light, "lux": lux}


# ---------------------------------------------------------------- backlight


def backlight_devices(root="/"):
    base = os.path.join(root, "sys", "class", "backlight")
    out = []
    if not os.path.isdir(base):
        return out
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            continue
        mx = _read_float(os.path.join(base, name, "max_brightness"))
        if mx is None or mx < 1:
            continue
        cur = _read_float(os.path.join(base, name, "brightness"))
        out.append({"name": name, "max": int(mx), "brightness": None if cur is None else int(cur)})
    return out


def write_backlight(level, root="/"):
    """Write one brightness file. False when there is no writable backlight. The level is 0-100."""
    devs = backlight_devices(root)
    if not devs:
        return False
    dev = devs[0]
    try:
        n = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        return False
    value = int(round(n / 100 * dev["max"]))
    value = max(0, min(dev["max"], value))
    path = os.path.join(root, "sys", "class", "backlight", dev["name"], "brightness")
    try:
        with open(path, "w") as handle:
            handle.write(str(value))
    except OSError:
        return False
    return True


def apply_brightness(level, root="/", stamp=None):
    """Apply a percent, or release the backlight when level is None.

    None leaves the hardware alone unless `stamp` exists, in which case the backlight
    is restored to 100 and the stamp is removed. A writable backlight is preferred.
    Otherwise the overlay level is set and the stamp is not written.
    Returns 'backlight', 'overlay', 'released', or 'leave'.
    """
    if level is None:
        set_overlay(None)
        if stamp and os.path.isfile(stamp):
            write_backlight(100, root)
            try:
                os.remove(stamp)
            except OSError:
                pass
            return "released"
        return "leave"
    try:
        n = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        set_overlay(None)
        return "leave"
    if write_backlight(n, root):
        set_overlay(None)
        if stamp and n < 100:
            try:
                folder = os.path.dirname(stamp)
                if folder:
                    os.makedirs(folder, exist_ok=True)
                with open(stamp, "w") as handle:
                    handle.write("1\n")
            except OSError:
                pass
        elif stamp and os.path.isfile(stamp):
            try:
                os.remove(stamp)
            except OSError:
                pass
        return "backlight"
    set_overlay(None if n >= 100 else n)
    return "overlay"


# ---------------------------------------------------------------- clock window


def clock_minutes(now):
    return int(now.tm_hour) * 60 + int(now.tm_min)


def in_window(minute, start, end):
    """True when minute is in [start, end). The window wraps midnight when start > end.

    Equal endpoints are an empty window (never inside), not a full day.
    """
    minute %= 1440
    start %= 1440
    end %= 1440
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def level_from_lux(lux, day, night, night_lux=NIGHT_LUX, day_lux=DAY_LUX):
    if lux <= night_lux or day_lux <= night_lux:
        return night
    if lux >= day_lux:
        return day
    t = (lux - night_lux) / (day_lux - night_lux)
    return int(round(night + t * (day - night)))


def shade_rgb565(raw, level):
    """Scale little-endian RGB565 bytes by level/100. Does not touch 32-bit pixels.

    `raw` is a bytearray of a 16bpp framebuffer (2 bytes per pixel, including row
    padding). A black overlay at opacity (100-level)/100 is the same as multiplying
    the color by level/100. Level None or >= 100 leaves the bytes alone.
    """
    if level is None:
        return raw
    try:
        n = int(level)
    except (TypeError, ValueError):
        return raw
    if n >= 100:
        return raw
    n = max(0, n)
    length = len(raw) - (len(raw) % 2)
    for i in range(0, length, 2):
        pix = raw[i] | (raw[i + 1] << 8)
        r = int(round(((pix >> 11) & 0x1F) * n / 100))
        g = int(round(((pix >> 5) & 0x3F) * n / 100))
        b = int(round((pix & 0x1F) * n / 100))
        pix = (min(31, r) << 11) | (min(63, g) << 5) | min(31, b)
        raw[i] = pix & 0xFF
        raw[i + 1] = (pix >> 8) & 0xFF
    return raw


# ---------------------------------------------------------------- NOAA solar calculator
# Equations from the NOAA ESRL solar position worksheet (general solar position,
# one refinement so declination and the equation of time are taken at the event).
# Longitude is degrees east. Times are minutes from 00:00 UTC and may fall outside 0-1440.


def _julian_day(year, month, day):
    """Julian day at 00:00 UTC."""
    y, m = int(year), int(month)
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + int(day) + b - 1524.5


def _jc(jd):
    return (jd - 2451545.0) / 36525.0


def _geom_mean_long(t):
    return (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0


def _geom_mean_anom(t):
    return 357.52911 + t * (35999.05029 - 0.0001537 * t)


def _eccent(t):
    return 0.016708634 - t * (0.000042037 + 0.0000001267 * t)


def _sun_eq_center(t):
    m = math.radians(_geom_mean_anom(t))
    return (math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
            + math.sin(2 * m) * (0.019993 - 0.000101 * t)
            + math.sin(3 * m) * 0.000289)


def _apparent_long(t):
    omega = 125.04 - 1934.136 * t
    return _geom_mean_long(t) + _sun_eq_center(t) - 0.00569 - 0.00478 * math.sin(math.radians(omega))


def _obliq_corr(t):
    sec = 21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))
    e0 = 23.0 + (26.0 + sec / 60.0) / 60.0
    omega = 125.04 - 1934.136 * t
    return e0 + 0.00256 * math.cos(math.radians(omega))


def _declination(t):
    e = _obliq_corr(t)
    lam = _apparent_long(t)
    return math.degrees(math.asin(math.sin(math.radians(e)) * math.sin(math.radians(lam))))


def solar_declination(year, month, day):
    """Solar declination in degrees at 00:00 UTC."""
    return _declination(_jc(_julian_day(year, month, day)))


def _equation_of_time(t):
    """Minutes. Apparent solar time minus mean solar time."""
    eps = _obliq_corr(t)
    l0 = _geom_mean_long(t)
    e = _eccent(t)
    m = _geom_mean_anom(t)
    y = math.tan(math.radians(eps) / 2.0)
    y *= y
    et = (y * math.sin(2.0 * math.radians(l0))
          - 2.0 * e * math.sin(math.radians(m))
          + 4.0 * e * y * math.sin(math.radians(m)) * math.cos(2.0 * math.radians(l0))
          - 0.5 * y * y * math.sin(4.0 * math.radians(l0))
          - 1.25 * e * e * math.sin(2.0 * math.radians(m)))
    return math.degrees(et) * 4.0


def _hour_angle(lat, decl):
    """Degrees, or 'up' / 'down' when the sun does not set or does not rise."""
    lat_r = math.radians(lat)
    cos_lat = math.cos(lat_r)
    cos_dec = math.cos(math.radians(decl))
    if abs(cos_lat) < 1e-6 or abs(cos_dec) < 1e-6:
        return "up" if lat * decl > 0 else "down"
    cos_h = math.cos(math.radians(ZENITH)) / (cos_lat * cos_dec) - math.tan(lat_r) * math.tan(math.radians(decl))
    if cos_h > 1:
        return "down"
    if cos_h < -1:
        return "up"
    return math.degrees(math.acos(min(1.0, max(-1.0, cos_h))))


def _utc_minutes(rise, jd, lat, lon):
    def once(when):
        t = _jc(when)
        ha = _hour_angle(lat, _declination(t))
        if not isinstance(ha, float):
            return ha
        signed = ha if rise else -ha
        return 720.0 - 4.0 * (lon + signed) - _equation_of_time(t)

    first = once(jd)
    if not isinstance(first, float):
        return first
    second = once(jd + first / 1440.0)
    return second if isinstance(second, float) else first


def sun_times(lat, lon, year, month, day):
    """Sunrise and sunset for a UTC date.

    `rise` and `set` are minutes from 00:00 UTC (negative or >= 1440 when the event
    is on the neighboring UTC day). `always_up` / `always_down` cover polar day and night.
    """
    lat, lon = float(lat), float(lon)
    if not -90 <= lat <= 90 or not -180 <= lon <= 180 or lat != lat or lon != lon:
        raise ValueError("latitude or longitude out of range")
    jd = _julian_day(year, month, day)
    rise = _utc_minutes(True, jd, lat, lon)
    sett = _utc_minutes(False, jd, lat, lon)
    up = rise == "up" or sett == "up"
    down = rise == "down" or sett == "down"
    return {
        "rise": rise if isinstance(rise, float) else None,
        "set": sett if isinstance(sett, float) else None,
        "always_up": bool(up and not isinstance(rise, float) and not isinstance(sett, float)),
        "always_down": bool(down and not isinstance(rise, float) and not isinstance(sett, float)),
    }


def _zone_offset_min(now, override):
    if override is not None:
        return int(override)
    off = getattr(now, "tm_gmtoff", None)
    if isinstance(off, int):
        return off // 60
    return 0


def sun_is_dark(lat, lon, now, sunrise_offset=0, sunset_offset=0, utc_offset_min=None):
    """True from sunset+offset until sunrise+offset, in the clock's local minutes.

    Offsets are minutes added to each event (positive is later). Polar day is never dark.
    Polar night is always dark. None when the position cannot be computed.
    """
    try:
        times = sun_times(lat, lon, now.tm_year, now.tm_mon, now.tm_mday)
    except (TypeError, ValueError):
        return None
    if times["always_down"]:
        return True
    if times["always_up"] or times["rise"] is None or times["set"] is None:
        return False
    off = _zone_offset_min(now, utc_offset_min)
    rise = (times["rise"] + off + int(sunrise_offset)) % 1440
    sett = (times["set"] + off + int(sunset_offset)) % 1440
    return in_window(clock_minutes(now), sett, rise)


# ---------------------------------------------------------------- level choice


def _blank_cfg():
    return {"mode": "off", "day": DEFAULT_DAY, "night": DEFAULT_NIGHT, "start": None, "end": None,
            "lat": None, "lon": None, "sunrise_offset": 0, "sunset_offset": 0}


def select_level(cfg, now, lux=None, utc_offset_min=None):
    """(percent or None, attention or None).

    None means do not change the backlight (mode off). attention is a short token the
    station does not show as a red error: no-sensor, no-times, no-location.
    """
    mode = cfg.get("mode") or "off"
    day, night = int(cfg.get("day", DEFAULT_DAY)), int(cfg.get("night", DEFAULT_NIGHT))
    if mode == "off":
        return None, None
    if mode == "schedule":
        if cfg.get("start") is None or cfg.get("end") is None:
            return day, "no-times"
        return (night if in_window(clock_minutes(now), cfg["start"], cfg["end"]) else day), None
    if mode == "sun":
        if cfg.get("lat") is None or cfg.get("lon") is None:
            return day, "no-location"
        dark = sun_is_dark(cfg["lat"], cfg["lon"], now, cfg.get("sunrise_offset") or 0,
                           cfg.get("sunset_offset") or 0, utc_offset_min)
        if dark is None:
            return day, "no-location"
        return (night if dark else day), None
    if mode == "auto-sensor":
        if lux is not None:
            return level_from_lux(float(lux), day, night), None
        if cfg.get("start") is not None and cfg.get("end") is not None:
            level, _why = select_level(dict(cfg, mode="schedule"), now, utc_offset_min=utc_offset_min)
            return level, "no-sensor"
        if cfg.get("lat") is not None and cfg.get("lon") is not None:
            level, why = select_level(dict(cfg, mode="sun"), now, utc_offset_min=utc_offset_min)
            return level, "no-sensor" if why is None else why
        return day, "no-sensor"
    return None, None


def in_dim_period(cfg, now, lux=None, utc_offset_min=None):
    """True when the idle screen should use the night clock (s11), aside from the on/off toggle.

    For a sensor, that is at or below the night lux threshold. Partial light keeps the day screen
    and only moves the brightness.
    """
    mode = cfg.get("mode") or "off"
    if mode == "schedule":
        if cfg.get("start") is None or cfg.get("end") is None:
            return False
        return in_window(clock_minutes(now), cfg["start"], cfg["end"])
    if mode == "sun":
        if cfg.get("lat") is None or cfg.get("lon") is None:
            return False
        return sun_is_dark(cfg["lat"], cfg["lon"], now, cfg.get("sunrise_offset") or 0,
                           cfg.get("sunset_offset") or 0, utc_offset_min) is True
    if mode == "auto-sensor":
        if lux is not None:
            return float(lux) <= NIGHT_LUX
        if cfg.get("start") is not None and cfg.get("end") is not None:
            return in_dim_period(dict(cfg, mode="schedule"), now, utc_offset_min=utc_offset_min)
        if cfg.get("lat") is not None and cfg.get("lon") is not None:
            return in_dim_period(dict(cfg, mode="sun"), now, utc_offset_min=utc_offset_min)
        return False
    return False


def _parse_stored_coord(text, limit):
    """A stored or typed coordinate, rounded to 2 decimals, or None when blank or unusable."""
    shown = normalize_coord(text, limit)
    if shown in ("", None):
        return None
    return float(shown)


def config_from_env(env=None):
    """Dimming settings from the environment. Blank dim coordinates fall back to the station
    location, still rounded to 2 decimals, and only when both are present."""
    src = os.environ if env is None else env

    def raw(key, default=""):
        v = src.get(key, default)
        return v if isinstance(v, str) else default

    cfg = _blank_cfg()
    mode = raw("GROK_STATION_DIM_MODE", "off").strip()
    cfg["mode"] = mode if mode in MODES else "off"
    for key, field, default in (("GROK_STATION_DIM_DAY", "day", DEFAULT_DAY),
                                ("GROK_STATION_DIM_NIGHT", "night", DEFAULT_NIGHT)):
        n = parse_level(raw(key, str(default)))
        cfg[field] = default if n is None else n
    for key, field in (("GROK_STATION_DIM_FROM", "start"), ("GROK_STATION_DIM_TO", "end")):
        text = raw(key, "").strip()
        cfg[field] = parse_hhmm(text) if text else None
    dim_lat = _parse_stored_coord(raw("GROK_STATION_DIM_LAT"), 90) if raw("GROK_STATION_DIM_LAT").strip() else None
    dim_lon = _parse_stored_coord(raw("GROK_STATION_DIM_LON"), 180) if raw("GROK_STATION_DIM_LON").strip() else None
    if raw("GROK_STATION_DIM_LAT").strip() or raw("GROK_STATION_DIM_LON").strip():
        cfg["lat"], cfg["lon"] = (dim_lat, dim_lon) if dim_lat is not None and dim_lon is not None else (None, None)
    else:
        lat = _parse_stored_coord(raw("GROK_STATION_LAT"), 90)
        lon = _parse_stored_coord(raw("GROK_STATION_LON"), 180)
        if lat is not None and lon is not None:
            cfg["lat"], cfg["lon"] = lat, lon
    for key, field in (("GROK_STATION_DIM_SUNRISE", "sunrise_offset"), ("GROK_STATION_DIM_SUNSET", "sunset_offset")):
        n = parse_offset(raw(key, "0") or "0")
        cfg[field] = 0 if n is None else n
    return cfg


# ---------------------------------------------------------------- admin form


def parse_level(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, float):
        if value != int(value):
            return None
        n = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d{1,3}", value.strip() or ""):
        n = int(value.strip())
    else:
        return None
    if 0 <= n <= 100:
        return n
    return None


def parse_offset(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, float):
        if value != int(value):
            return None
        n = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d{1,3}", value.strip() or ""):
        n = int(value.strip())
    else:
        return None
    if -180 <= n <= 180:
        return n
    return None


def parse_hhmm(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?", value.strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def normalize_coord(value, limit):
    """'' when blank, '12.34' when usable, None when it is not a coordinate in range.

    Rounded half-up to 2 decimal places. More than 6 decimal places is rejected.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value or abs(value) == float("inf")):
            return None
        text = format(value, "f")
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if text == "":
        return ""
    if not re.fullmatch(r"[+-]?\d{1,3}(\.\d{1,6})?", text):
        return None
    try:
        qty = Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    if qty == 0:
        return "0.00"
    if abs(qty) > Decimal(limit):
        return None
    return f"{qty:.2f}"


def public_coord(value, limit):
    """What the browser may see: 2 decimals, or '' when the stored value is blank or unusable."""
    shown = normalize_coord("" if value is None else value, limit)
    return "" if shown is None else shown


def display_view(values):
    """Copy display settings for the browser, with coordinates forced to 2 decimals."""
    if not isinstance(values, dict):
        values = {}
    out = {}
    for key, value in values.items():
        if key == "GROK_STATION_DIM_LAT":
            out[key] = public_coord(value, 90)
        elif key == "GROK_STATION_DIM_LON":
            out[key] = public_coord(value, 180)
        else:
            out[key] = value
    return out


def normalize_form(data):
    """Browser JSON -> helper stdin. (body, error). error is a sentence, or None.

    Coordinates in `body` are already rounded to 2 decimals. Callers must not log `body`.
    """
    if not isinstance(data, dict):
        return None, "bad request"
    allowed = set(PASS_KEYS) | set(DIM_KEYS)
    if any(k not in allowed for k in data):
        return None, "That setting isn't on this page."
    out = {}
    for key in PASS_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None, "That setting isn't on this page."
        out[key] = str(value).strip()
    if not any(k in data for k in DIM_KEYS):
        return out, None
    mode = data.get("GROK_STATION_DIM_MODE", "off")
    if not isinstance(mode, str) or mode.strip() not in MODES:
        return None, "Pick a dimming mode: off, schedule, sun, or auto-sensor."
    mode = mode.strip()
    out["GROK_STATION_DIM_MODE"] = mode
    for key, label, default in (("GROK_STATION_DIM_DAY", "Day brightness", DEFAULT_DAY),
                                ("GROK_STATION_DIM_NIGHT", "Night brightness", DEFAULT_NIGHT)):
        if key not in data:
            out[key] = str(default)
            continue
        n = parse_level(data[key])
        if n is None:
            return None, f"{label} must be a whole number from 0 to 100."
        out[key] = str(n)
    for key, label in (("GROK_STATION_DIM_SUNRISE", "Sunrise offset"), ("GROK_STATION_DIM_SUNSET", "Sunset offset")):
        if key not in data or data[key] in ("", None):
            out[key] = "0"
            continue
        n = parse_offset(data[key])
        if n is None:
            return None, f"{label} must be a whole number of minutes from -180 to 180."
        out[key] = str(n)
    start = data.get("GROK_STATION_DIM_FROM", "")
    end = data.get("GROK_STATION_DIM_TO", "")

    def _present(value):
        if value in (None, ""):
            return False
        if isinstance(value, str) and not value.strip():
            return False
        return True

    sm = parse_hhmm(start) if isinstance(start, str) else None
    em = parse_hhmm(end) if isinstance(end, str) else None
    bad = (_present(start) and sm is None) or (_present(end) and em is None)
    if bad or (mode == "schedule" and (sm is None or em is None)):
        return None, "Dim from and dim until need a time like 22:00."
    if sm is not None:
        out["GROK_STATION_DIM_FROM"] = _hhmm(sm)
    if em is not None:
        out["GROK_STATION_DIM_TO"] = _hhmm(em)
    lat_in, lon_in = data.get("GROK_STATION_DIM_LAT", ""), data.get("GROK_STATION_DIM_LON", "")
    if lat_in in (None, "") and lon_in in (None, ""):
        out["GROK_STATION_DIM_LAT"] = ""
        out["GROK_STATION_DIM_LON"] = ""
    else:
        lat, lon = normalize_coord(lat_in, 90), normalize_coord(lon_in, 180)
        if lat is None:
            return None, "Latitude must be between -90 and 90."
        if lon is None:
            return None, "Longitude must be between -180 and 180."
        if lat == "" or lon == "":
            return None, "Enter both latitude and longitude, or leave both blank."
        out["GROK_STATION_DIM_LAT"] = lat
        out["GROK_STATION_DIM_LON"] = lon
    return out, None
