"""WLED commands inside the relay. Outbound HTTP only.

Registry file: $HOME_REGISTRY or /var/lib/grok-station/integrations/integrations.json.
Reloaded when the file mtime changes. Missing or malformed means no devices.

HOME_ALLOW_LOOPBACK_FOR_TESTS=1 allows a loopback host (127.0.0.1 or ::1),
including a port ("127.0.0.1:PORT"). Tests only. Leave it unset on a Pi:
loopback is refused, and a public address is refused either way.
"""
import asyncio, ipaddress, json, os, re, socket, time

import aiohttp

DEFAULT_PATH = "/var/lib/grok-station/integrations/integrations.json"
_READ_MAX = 256 * 1024
_BODY_MAX = 64 * 1024
_TIMEOUT = aiohttp.ClientTimeout(total=3)
# 20% of the WLED brightness range (0-255).
BRI_STEP = round(255 * 20 / 100)

# Spoken color -> RGB. Sixteen names, fixed here, not taken from a device.
_COLOR_ROWS = (
    ("red", (255, 0, 0)),
    ("orange", (255, 80, 0)),
    ("yellow", (255, 200, 0)),
    ("lime", (160, 255, 0)),
    ("green", (0, 255, 0)),
    ("teal", (0, 128, 128)),
    ("cyan", (0, 255, 255)),
    ("blue", (0, 0, 255)),
    ("purple", (128, 0, 255)),
    ("magenta", (255, 0, 255)),
    ("pink", (255, 64, 160)),
    ("lavender", (180, 140, 255)),
    ("white", (255, 255, 255)),
    ("warm white", (255, 180, 70)),
    ("cool white", (200, 220, 255)),
    ("gold", (255, 160, 0)),
)
COLORS = {name: rgb for name, rgb in _COLOR_ROWS}

_ACTIONS = frozenset(("on", "off", "bri", "brighten", "dim", "color", "preset"))
_CAPS = frozenset(("onoff", "bri", "color", "preset"))
_META = ipaddress.ip_address("169.254.169.254")  # cloud metadata, never a light
_LOCAL = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+local$", re.IGNORECASE)
_HEY = re.compile(r"^hey [a-z0-9][a-z0-9'-]* ")
_POLITE = re.compile(r"^(?:can you|could you|please) ")
_PCT = r"(?:100|[1-9][0-9]|[0-9])"
_NO = object()

_cache = {}   # path -> (mtime_ns or None, registry)
_offline = {}  # device id -> epoch when it became unreachable


def normalize_name(s):
    """Spoken form of a device name.

    Casefold, remove punctuation (it is deleted, not turned into a space),
    collapse whitespace, drop one leading "the", and drop a trailing "light"
    or "lights". "Porch Light", "the porch lights", and "porch" are one name.
    "Mom's Lamp" and "moms lamp" are one name. A hyphen is removed, so
    "under-cabinet" and "under cabinet" are not the same. An empty result
    means the words were only "the", "light", or "lights".
    """
    text = "".join(ch if ch.isalnum() or ch.isspace() else "" for ch in str(s or "").casefold())
    words = text.split()
    if words and words[0] == "the":
        words = words[1:]
    if words and words[-1] in ("light", "lights"):
        words = words[:-1]
    return " ".join(words)


def name_clashes(name, other_names):
    """True when name is the same spoken name as any of other_names.

    Comparison is normalize_name equality, including two empty results
    (both names were only "the" / "light" / "lights"). The admin track has
    its own copy of this rule; keep this a pure function.
    """
    key = normalize_name(name)
    for other in other_names or []:
        if normalize_name(other) == key:
            return True
    return False


def _empty():
    return {"version": 1, "integrations": {}, "devices": []}


def _preset_map(raw):
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, val in raw.items():
        if isinstance(key, str) and key.strip() and isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            out[key.strip()] = val
    return out


def _device(row):
    """One WLED device, or None when the row is not a usable WLED light."""
    if not isinstance(row, dict) or row.get("kind") != "wled":
        return None
    ident, host, name = row.get("id"), row.get("host"), row.get("name")
    if not all(isinstance(v, str) and v.strip() for v in (ident, host, name)):
        return None
    aliases = []
    raw_aliases = row.get("aliases")
    if isinstance(raw_aliases, list):
        for alias in raw_aliases:
            if isinstance(alias, str) and alias.strip() and alias.strip() not in aliases:
                aliases.append(alias.strip())
    caps = []
    raw_caps = row.get("caps")
    if isinstance(raw_caps, list):
        for cap in raw_caps:
            if isinstance(cap, str) and cap in _CAPS and cap not in caps:
                caps.append(cap)
    kind = row.get("type")
    return {
        "id": ident.strip(),
        "kind": "wled",
        "host": host.strip(),
        "name": name.strip(),
        "aliases": aliases,
        "type": kind.strip() if isinstance(kind, str) and kind.strip() else "light",
        "caps": caps,
        "presets": _preset_map(row.get("presets")),
    }


def _parse(raw):
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return _empty()
    if not isinstance(data, dict) or data.get("version") != 1:
        return _empty()
    integrations = data.get("integrations")
    if not isinstance(integrations, dict):
        integrations = {}
    wled = integrations.get("wled")
    enabled = isinstance(wled, dict) and wled.get("enabled") is True
    devices = []
    if enabled:
        for row in data.get("devices") or []:
            dev = _device(row)
            if dev:
                devices.append(dev)
    out = _empty()
    if enabled:
        out["integrations"] = {"wled": {"enabled": True}}
        if wled.get("voice") is True:  # 0.13.0 (Nix L2): voice home control, off unless turned on in the admin
            out["integrations"]["wled"]["voice"] = True
    out["devices"] = devices
    return out


def load_registry(path=None):
    """The registry at path, or $HOME_REGISTRY, or the default path.

    Cached per path. A changed mtime reloads. A missing file, a file over
    256 KB, or JSON that is not version 1 yields an empty registry. Devices
    whose kind is not "wled", and every device while WLED is not enabled,
    are left out.
    """
    if path is None:
        path = os.environ.get("HOME_REGISTRY") or DEFAULT_PATH
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        mtime = None
    hit = _cache.get(path)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    reg = _empty()
    if mtime is not None:
        try:
            with open(path, "rb") as fh:
                blob = fh.read(_READ_MAX + 1)
        except OSError:
            blob = b""
        if blob and len(blob) <= _READ_MAX:
            reg = _parse(blob)
    _cache[path] = (mtime, reg)
    return reg


def _loopback_ok():
    return os.environ.get("HOME_ALLOW_LOOPBACK_FOR_TESTS") == "1"


def _port(text):
    if not text or not str(text).isdigit():
        return None
    n = int(text)
    return n if 1 <= n <= 65535 else None


def _split_host(host):
    """(name, port or None), or None when the shape is not a host."""
    if not isinstance(host, str):
        return None
    h = host.strip()
    if not h or len(h) > 300 or any(c in h for c in " \t\r\n/@?#\\%"):
        return None
    if h.startswith("["):
        m = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\](?::([0-9]{1,5}))?", h)
        if not m:
            return None
        port = None
        if m.group(2) is not None:
            port = _port(m.group(2))
            if port is None:
                return None
        return m.group(1), port
    if h.count(":") > 1:
        return h, None
    port = None
    if h.count(":") == 1:
        name, text = h.rsplit(":", 1)
        port = _port(text)
        if port is None or not name:
            return None
        h = name
    if h.endswith("."):
        h = h[:-1]
    return (h, port) if h else None


def _ip_ok(ip, loopback_ok):
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_multicast or ip.is_unspecified or (ip.is_reserved and not ip.is_loopback):
        return False
    if ip.is_loopback:
        return loopback_ok
    if ip == _META:
        return False
    if ip.is_link_local:
        return True
    b = ip.packed
    if isinstance(ip, ipaddress.IPv4Address):  # RFC 1918 by octet (no range literals in the repo)
        return b[0] == 10 or (b[0] == 172 and 16 <= b[1] <= 31) or (b[0] == 192 and b[1] == 168)
    return (b[0] & 0xFE) == 0xFC  # IPv6 unique local


def station_addresses():
    """This machine's own addresses. Tests replace this. Never logged."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            if info[4]:
                found.add(info[4][0])
    except OSError:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.0.2.1", 9))
            found.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    return found


def _as_ip(text):
    try:
        ip = ipaddress.ip_address(str(text).split("%", 1)[0])
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _own():
    out = set()
    for item in station_addresses() or []:
        ip = _as_ip(item)
        if ip is not None:
            out.add(ip)
    return out


def _target_ok(ip, own):
    """_ip_ok, and never one of the station's own addresses. Test-only loopback is decided by _ip_ok alone."""
    if ip is None or not _ip_ok(ip, _loopback_ok()):
        return False
    return ip.is_loopback or ip not in own


def host_ok(host):
    """True for a .local name (shape only; resolved at use) or a private or link-local literal
    that is not one of the station's own addresses.

    Loopback is refused unless HOME_ALLOW_LOOPBACK_FOR_TESTS=1 (tests only).
    A :port on the host is allowed so tests can use 127.0.0.1:PORT.
    """
    parts = _split_host(host)
    if not parts:
        return False
    name, _port_n = parts
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return _LOCAL.fullmatch(name) is not None and len(name) <= 253
    own = _own()
    return bool(own) and _target_ok(_as_ip(name), own)  # 0.13.0 (Nix Info I1): no own list, refuse


async def resolve_host(name):
    """Addresses for a .local name, resolved once. Tests replace this."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(name, None, type=socket.SOCK_STREAM), 2)
    except (OSError, asyncio.TimeoutError):
        return []
    return [i[4][0] for i in infos if i[0] in (socket.AF_INET, socket.AF_INET6) and i[4]]


async def checked_target(host):
    """(url, host header) for a device, or None (0.13.0, Nix L1).

    A .local name is resolved once here. Every address must pass _target_ok (private or link-local,
    not 169.254.169.254, not loopback outside tests, not the station's own addresses). The request
    goes to the checked address with the name only in the Host header, so a later mDNS answer or
    a changed DNS answer cannot move it to the relay, the admin, or a public address.
    """
    parts = _split_host(host)
    if not parts:
        return None
    name, port = parts
    own = _own()
    if not own:  # 0.13.0 (Nix Info I1): fail closed when the station's own addresses are unknown
        return None
    if _as_ip(name) is not None:
        ips = [_as_ip(name)]
    else:
        if _LOCAL.fullmatch(name) is None or len(name) > 253:
            return None
        ips = [_as_ip(a) for a in await resolve_host(name)]
        if not ips:
            return None
    if not all(_target_ok(ip, own) for ip in ips):
        return None
    ip = ips[0]
    loc = "[%s]" % ip.compressed if ip.version == 6 else str(ip)
    shown = ("[%s]" % name if ":" in name else name.lower())
    if port:
        loc += ":%d" % port
        shown += ":%d" % port
    return "http://%s/json/state" % loc, shown


def _clean(text):
    """Same cleanup as import_team_request: hey-word, two politeness prefixes, punctuation."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    t = raw.lower().replace("\u2019", "'")
    for ch in ",?!.":
        t = t.replace(ch, " ")
    t = " ".join(t.split())
    m = _HEY.match(t)
    if m:
        t = t[m.end():]
    for _ in range(2):
        m = _POLITE.match(t)
        if not m:
            break
        t = t[m.end():]
    if t.endswith(" please"):
        t = t[:-len(" please")].strip()
    return t


def _soft(s):
    """Casefold and strip punctuation, without the light/the device-name rule."""
    text = "".join(ch if ch.isalnum() or ch.isspace() else "" for ch in str(s or "").casefold())
    return " ".join(text.split())


def _wled_on(registry):
    if not isinstance(registry, dict):
        return False
    integrations = registry.get("integrations")
    if not isinstance(integrations, dict):
        return False
    wled = integrations.get("wled")
    return isinstance(wled, dict) and wled.get("enabled") is True


def voice_on(registry):
    """True only when the admin turned on Voice home control (0.13.0, Nix L2). Saving a device does not."""
    if not _wled_on(registry):
        return False
    return registry["integrations"]["wled"].get("voice") is True


VOICE_OFF_SAY = "Voice home control is off. Turn it on in the admin under Integrations."


def _usable(registry):
    if not _wled_on(registry):
        return []
    out = []
    for row in registry.get("devices") or []:
        dev = row if isinstance(row, dict) and row.get("kind") == "wled" else None
        if dev and isinstance(dev.get("host"), str) and isinstance(dev.get("name"), str) and isinstance(dev.get("id"), str):
            out.append(dev)
    return out


def _phrases(dev):
    names = [dev.get("name")]
    aliases = dev.get("aliases") or []
    if isinstance(aliases, list):
        names.extend(aliases)
    out = []
    for raw in names:
        if not isinstance(raw, str):
            continue
        key = normalize_name(raw)
        if key and key not in out:
            out.append(key)
    return out


def _lookup(devices, span):
    key = normalize_name(span)
    if not key:
        return None
    hits = [dev for dev in devices if key in _phrases(dev)]
    return hits[0] if len(hits) == 1 else None


def _caps(dev):
    caps = dev.get("caps") or []
    if not isinstance(caps, list):
        return set()
    return {c for c in caps if isinstance(c, str)}


def _intent(dev, action, **extra):
    out = {"id": dev.get("id"), "kind": "wled", "host": dev.get("host"), "action": action}
    out.update(extra)
    return out


def _onoff(devices, span, which):
    dev = _lookup(devices, span)
    if not dev or "onoff" not in _caps(dev):
        return _NO
    return _intent(dev, "on" if which == "on" else "off")


def _power(text, devices):
    m = re.fullmatch(r"(?:turn|switch) (on|off) (.+)", text)
    if m:
        return _onoff(devices, m.group(2), m.group(1))
    m = re.fullmatch(r"(?:turn|switch) (.+) (on|off)", text)
    if m:
        return _onoff(devices, m.group(1), m.group(2))
    return None


def _level(text, devices):
    m = re.fullmatch(r"(?:dim|set) (.+) to (" + _PCT + r")(?: percent)?", text)
    if not m:
        return None
    dev = _lookup(devices, m.group(1))
    if not dev or "bri" not in _caps(dev):
        return _NO
    return _intent(dev, "bri", pct=int(m.group(2)))


def _relative(text, devices):
    m = re.fullmatch(r"(brighten|dim) (.+)", text)
    if not m:
        return None
    dev = _lookup(devices, m.group(2))
    if not dev or "bri" not in _caps(dev):
        return _NO
    return _intent(dev, m.group(1))


def _preset_id(dev, spoken):
    want = _soft(spoken)
    if not want:
        return None
    hits = []
    presets = dev.get("presets") or {}
    if not isinstance(presets, dict):
        return None
    for key, val in presets.items():
        if _soft(key) == want and isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            hits.append(val)
    return hits[0] if len(hits) == 1 else None


def _color_or_preset(dev, rest):
    raw = " ".join(str(rest).split())
    forced = raw.startswith("preset ")
    if forced:
        raw = raw[len("preset "):].strip()
    if not forced:
        rgb = COLORS.get(_soft(raw))
        if rgb:
            if "color" not in _caps(dev):
                return _NO
            return _intent(dev, "color", rgb=[int(n) for n in rgb])
    if "preset" not in _caps(dev):
        return _NO
    ps = _preset_id(dev, raw)
    if ps is None:
        return _NO
    return _intent(dev, "preset", ps=ps)


def _set_to(text, devices):
    m = re.fullmatch(r"set (.+) to (.+)", text)
    if not m:
        return None
    dev = _lookup(devices, m.group(1))
    if not dev:
        return _NO
    return _color_or_preset(dev, m.group(2))


def _preset_phrase(text, devices):
    m = re.fullmatch(r"(.+) preset (.+)", text)
    if not m:
        return None
    dev = _lookup(devices, m.group(1))
    if not dev or "preset" not in _caps(dev):
        return _NO
    ps = _preset_id(dev, m.group(2))
    if ps is None:
        return _NO
    return _intent(dev, "preset", ps=ps)


def home_match(text, registry):
    """A whole-utterance WLED command, or None.

    Questions, an unknown device, and unnamed "the lights" / "everything off"
    are None (v1 has no groups). "ask Max to ..." does not match. The intent
    is id, kind, host, and action (on, off, bri, brighten, dim, color, preset)
    plus pct (0-100), rgb, or ps. The device name is not included.
    """
    text = _clean(text)
    if not text:
        return None
    devices = _usable(registry)
    if not devices:
        return None
    for fn in (_power, _level, _relative, _set_to, _preset_phrase):
        got = fn(text, devices)
        if got is None:
            continue
        return None if got is _NO else got
    return None


def bri_from_pct(pct):
    """WLED bri for a 0-100 percent. Half values use Python round."""
    return int(round(pct * 255 / 100))


def offline_since(device_id):
    """Epoch when this device became unreachable, or None if it is not marked."""
    return _offline.get(device_id)


def _mark(device_id, ok):
    if not isinstance(device_id, str) or not device_id:
        return
    if ok:
        _offline.pop(device_id, None)
    elif device_id not in _offline:
        _offline[device_id] = time.time()


def _log(logger, action, ok):
    if action not in _ACTIONS:
        action = "command"
    line = "home wled %s %s" % (action, "ok" if ok else "fail")
    if logger is None:
        return line
    try:
        logger(line)
    except Exception:
        pass
    return line


async def _call_raw(http, method, url, body, host=None):
    """One request. None unless the status is 2xx and the body is at most 64 KB.

    Redirects are not followed. The caller does not log the URL.
    """
    kw = {"allow_redirects": False, "timeout": _TIMEOUT, "max_redirects": 0}
    if host:
        kw["headers"] = {"Host": host}
    if body is not None:
        kw["json"] = body
    try:
        async with http.request(method, url, **kw) as resp:
            data = await resp.content.read(_BODY_MAX + 1)
            if resp.status < 200 or resp.status >= 300 or len(data) > _BODY_MAX:
                return None
            return data
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        return None


def _rgb(intent):
    rgb = intent.get("rgb")
    if not isinstance(rgb, (list, tuple)) or len(rgb) != 3:
        return None
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 or n > 255 for n in rgb):
        return None
    return [int(n) for n in rgb]


async def _perform(http, intent):
    target = await checked_target(intent.get("host"))
    if target is None:
        return False
    url, shown = target

    async def _call(http_, method, url_, body):
        return await _call_raw(http_, method, url_, body, shown)
    action = intent.get("action")
    if action in ("on", "off"):
        return await _call(http, "POST", url, {"on": action == "on"}) is not None
    if action == "bri":
        pct = intent.get("pct")
        if isinstance(pct, bool) or not isinstance(pct, int) or not 0 <= pct <= 100:
            return False
        return await _call(http, "POST", url, {"on": True, "bri": bri_from_pct(pct)}) is not None
    if action == "color":
        rgb = _rgb(intent)
        if not rgb:
            return False
        return await _call(http, "POST", url, {"on": True, "seg": [{"col": [rgb]}]}) is not None
    if action == "preset":
        ps = intent.get("ps")
        if isinstance(ps, bool) or not isinstance(ps, int) or ps < 0:
            return False
        return await _call(http, "POST", url, {"ps": ps}) is not None
    if action in ("brighten", "dim"):
        data = await _call(http, "GET", url, None)
        if not data:
            return False
        try:
            state = json.loads(data)
        except ValueError:
            return False
        bri = state.get("bri") if isinstance(state, dict) else None
        if isinstance(bri, bool) or not isinstance(bri, int) or not 0 <= bri <= 255:
            return False
        step = BRI_STEP if action == "brighten" else -BRI_STEP
        new = max(0, min(255, bri + step))
        return await _call(http, "POST", url, {"bri": new}) is not None
    return False


async def run_intent(http, intent, logger=None):
    """Run one WLED intent. True when the device accepted it.

    logger, when set, is called with one line: "home wled <action> ok|fail".
    The line has no device name, host, or value. An unreachable device is
    remembered in memory (offline_since); nothing is written to disk.
    """
    action = "command"
    device_id = None
    if isinstance(intent, dict) and intent.get("action") in _ACTIONS:
        action = intent["action"]
        if isinstance(intent.get("id"), str):
            device_id = intent["id"]
    ok = False
    try:
        if action in _ACTIONS:
            ok = await _perform(http, intent)
    except Exception:
        ok = False
    _mark(device_id, ok)
    _log(logger, action, ok)
    return ok
