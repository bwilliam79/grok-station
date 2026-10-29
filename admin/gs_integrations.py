"""WLED integration registry, discovery, and the outbound address guard.

No secrets live here. The registry is /var/lib/grok-station/integrations/integrations.json
(ADMIN_INTEGRATIONS overrides the path). It is written 0640 in a setgid grok-admin folder (install.sh), so
grok-relay (primary group grok-admin) can read it and nothing else can. ADMIN_ALLOW_LOOPBACK_FOR_TESTS=1 allows
127.0.0.1 only, for tests; never set it in a unit file.

169.254.169.254 is link-local but refused (cloud metadata). Other
169.254.0.0/16 addresses stay allowed.

No support-bundle endpoint exists. redact_registry() is what a bundle should
write: host becomes "redacted", names and aliases become the device id.
"""
import ipaddress, json, os, re, secrets, socket, struct, tempfile, time, urllib.error, urllib.request

BLINK_S = 0.4
MDNS_GROUP = ("224.0.0.251", 5353)
MAX_BODY = 64 * 1024
FETCH_TIMEOUT = 3
DISCOVER_S = 4.0
CAPS = ["onoff", "bri", "color", "preset"]
_META = ipaddress.ip_address("169.254.169.254")
_LOOPBACK_OK = ipaddress.ip_address("127.0.0.1")
_LOCAL_NAME = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+local$")
_FW = re.compile(r"[\w.+\-]{1,40}")
_SERVICE = "_wled._tcp.local"


def integrations_path():
    return os.environ.get("ADMIN_INTEGRATIONS", "/var/lib/grok-station/integrations/integrations.json")


def empty_doc():
    return {"version": 1, "integrations": {}, "devices": []}


def atomic_write(path, data, mode=0o640):
    """mkstemp in the same directory, fchmod, fsync file and dir, then rename."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _clean_devices(raw):
    out = []
    if not isinstance(raw, list):
        return out
    for d in raw:
        if not isinstance(d, dict):
            continue
        ident = d.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"w-[0-9a-f]{4}", ident):
            continue
        host = d.get("host")
        name = d.get("name")
        if not isinstance(host, str) or not host or not isinstance(name, str) or not name:
            continue
        aliases = d.get("aliases")
        if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
            continue
        presets = d.get("presets") if isinstance(d.get("presets"), dict) else {}
        clean_pre = {}
        for k, v in presets.items():
            if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool):
                clean_pre[k] = v
        out.append({
            "id": ident, "kind": "wled", "host": host, "name": name,
            "aliases": aliases[:8], "type": "light", "caps": list(CAPS), "presets": clean_pre,
        })
    return out


def load():
    """Registry, or an empty v1 document when the file is missing or unusable."""
    try:
        with open(integrations_path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return empty_doc()
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return empty_doc()
    integrations = {}
    got = raw.get("integrations")
    if isinstance(got, dict):
        wled = got.get("wled")
        if isinstance(wled, dict) and wled.get("enabled") is True:
            integrations["wled"] = {"enabled": True}
            if wled.get("voice") is True:  # 0.13.0 (Nix L2): voice home control is its own switch, off by default
                integrations["wled"]["voice"] = True
    return {"version": 1, "integrations": integrations, "devices": _clean_devices(raw.get("devices"))}


def save(doc):
    body = (json.dumps(doc, indent=1) + "\n").encode()
    atomic_write(integrations_path(), body, 0o640)


def new_id(used):
    for _ in range(32):
        ident = "w-" + secrets.token_hex(2)
        if ident not in used:
            return ident
    raise RuntimeError("id")


def spoken_key(text):
    """casefold, strip punctuation, collapse spaces, drop a leading 'the' and a trailing light/lights."""
    if not isinstance(text, str):
        return ""
    chars = []
    for ch in text.casefold():
        chars.append(ch if ch.isalnum() or ch.isspace() else " ")
    parts = "".join(chars).split()
    if parts and parts[0] == "the":
        parts = parts[1:]
    if parts and parts[-1] in ("light", "lights"):
        parts = parts[:-1]
    return " ".join(parts)


def redact_registry(doc):
    """Bundle view. Hosts become 'redacted'. Spoken names and aliases become the device id."""
    src = doc if isinstance(doc, dict) else {}
    devices = []
    for d in _clean_devices(src.get("devices")):
        presets = {str(v): v for v in d["presets"].values()}
        devices.append({
            "id": d["id"], "kind": "wled", "host": "redacted", "name": d["id"],
            "aliases": [d["id"] for _ in d["aliases"]], "type": "light",
            "caps": list(CAPS), "presets": presets,
        })
    integrations = {}
    if isinstance(src.get("integrations"), dict) and (src["integrations"].get("wled") or {}).get("enabled") is True:
        integrations["wled"] = {"enabled": True}
    return {"version": 1, "integrations": integrations, "devices": devices}


def _ip(text):
    try:
        ip = ipaddress.ip_address(str(text))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def allow_loopback():
    """127.0.0.1 only, and only when the test env is set. Read at call time."""
    return os.environ.get("ADMIN_ALLOW_LOOPBACK_FOR_TESTS") == "1"


def resolve_host(host):
    """Addresses for a name. Tests replace this."""
    found = []
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return found
    for fam, _type, _proto, _canon, sockaddr in infos:
        if fam in (socket.AF_INET, socket.AF_INET6) and sockaddr:
            found.append(sockaddr[0])
    return found


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


def _addr_ok(ip, own):
    """Private, link-local, or ULA. 169.254.169.254 is refused even though it is link-local.

    The test flag allows exactly 127.0.0.1, including when that address is also one of the
    station's own addresses (a dev machine's hostname often resolves there). ::1 stays refused.
    """
    if ip is None or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return False
    if ip == _META:
        return False
    if ip.is_loopback:
        return allow_loopback() and ip == _LOOPBACK_OK
    if ip in own:
        return False
    if ip.is_link_local:
        return True
    b = ip.packed
    if isinstance(ip, ipaddress.IPv4Address):  # RFC 1918 only, checked by octet (no range literals in the repo)
        return b[0] == 10 or (b[0] == 172 and 16 <= b[1] <= 31) or (b[0] == 192 and b[1] == 168)
    return (b[0] & 0xFE) == 0xFC  # IPv6 unique local (fc00::/7)


def split_host(raw):
    """(host, port or None) or None. Optional :port. No scheme, user, or path."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > 253 or any(c in text for c in " /@\\?#") or "://" in text:
        return None
    port = None
    if text.startswith("["):
        end = text.find("]")
        if end < 2:
            return None
        host, rest = text[1:end], text[end + 1:]
        if rest:
            if not rest.startswith(":"):
                return None
            port = rest[1:]
    elif text.count(":") > 1:
        return None
    elif ":" in text:
        host, port = text.rsplit(":", 1)
    else:
        host = text
    if host.endswith("."):
        host = host[:-1]
        if host.endswith("."):
            return None
    if port is not None:
        if not port.isdigit():
            return None
        port = int(port)
        if port < 1 or port > 65535:
            return None
    if not host or len(host) > 253:
        return None
    return host, port


def checked_addr(raw):
    """(ip, port or None, host header) for raw, or None. The name is resolved once, here.

    Every resolved address must pass _addr_ok (private, link-local or ULA; not loopback unless the test
    flag allows 127.0.0.1; not unspecified, multicast, public, 169.254.169.254, or one of the station's
    own addresses). The caller connects to the returned ip, never to the name again, so a later mDNS
    answer or a changed DNS answer cannot move the request (0.13.0, Nix L1).
    """
    parts = split_host(raw)
    if not parts:
        return None
    host, port = parts
    own = set()
    for item in station_addresses() or []:
        ip = _ip(item)
        if ip is not None:
            own.add(ip)
    if not own:  # 0.13.0 (Nix Info I1): fail closed; without the station's own addresses nothing is checked
        return None
    shown = display_host(raw)
    if not shown:
        return None
    literal = _ip(host)
    if literal is not None:
        return (literal, port, shown) if _addr_ok(literal, own) else None
    if _LOCAL_NAME.fullmatch(host.lower()) is None:
        return None
    ips = [_ip(a) for a in resolve_host(host) or []]
    if not ips or not all(_addr_ok(ip, own) for ip in ips):
        return None
    return ips[0], port, shown


def host_ok(raw):
    """True when raw is a .local name or a private/link-local/ULA literal, and resolved addresses are too."""
    return checked_addr(raw) is not None


def _ip_url(ip, port, path):
    loc = "[%s]" % ip.compressed if isinstance(ip, ipaddress.IPv6Address) else str(ip)
    return "http://" + loc + (":%d" % port if port else "") + path


def display_host(raw):
    """Host string to store and send, or None."""
    parts = split_host(raw)
    if not parts:
        return None
    host, port = parts
    literal = _ip(host)
    if literal is not None:
        shown = str(literal)
        if isinstance(literal, ipaddress.IPv6Address):
            shown = "[" + shown + "]"
    else:
        shown = host.lower()
    if port and port != 80:
        return shown + ":" + str(port)
    return shown


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def fetch(host, path, method="GET", payload=None):
    """('ok', obj) or ('refused', None) or ('not_wled', None). Never returns a device body on failure."""
    if not isinstance(path, str) or not path.startswith("/"):
        return "refused", None
    target = checked_addr(host)
    if target is None:
        return "refused", None
    ip, port, shown = target
    url = _ip_url(ip, port, path)  # the checked address; the name is only the Host header
    data = None
    headers = {"Accept": "application/json", "Host": shown}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
            raw = resp.read(MAX_BODY + 1)
            code = getattr(resp, "status", 200)
    except Exception:
        return "not_wled", None
    if code != 200 or len(raw) > MAX_BODY:
        return "not_wled", None
    try:
        doc = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return "not_wled", None
    if not isinstance(doc, (dict, list)):
        return "not_wled", None
    return "ok", doc


def _label(text, fallback):
    if not isinstance(text, str):
        text = fallback or "WLED"
    text = " ".join(text.split())
    if not text or any(ord(c) < 32 for c in text):
        text = fallback or "WLED"
    text = " ".join(text.split())
    return (text or "WLED")[:40]


def probe(host, fallback_name=""):
    """('ok', {name, host, firmware, leds}) or an error code and None."""
    if not host_ok(host):
        return "refused", None
    shown = display_host(host)
    status, doc = fetch(shown, "/json/info")
    if status != "ok":
        return status, None
    if not isinstance(doc, dict):
        return "not_wled", None
    ver = doc.get("ver")
    firmware = ver if isinstance(ver, str) and _FW.fullmatch(ver) else "unknown"
    leds = doc.get("leds") if isinstance(doc.get("leds"), dict) else {}
    count = leds.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > 100000:
        return "not_wled", None
    return "ok", {
        "name": _label(doc.get("name"), fallback_name),
        "host": shown,
        "firmware": firmware,
        "leds": count,
    }


def read_presets(host):
    """{casefolded name: id}. Empty when the light doesn't answer. Names only, no other fields."""
    status, doc = fetch(host, "/presets.json")
    out = {}
    if status != "ok":
        return out
    rows = []
    if isinstance(doc, dict):
        for key, val in doc.items():
            if isinstance(val, dict):
                rows.append((key, val))
    elif isinstance(doc, list):
        for val in doc:
            if isinstance(val, dict):
                rows.append((val.get("id"), val))
    for key, val in rows:
        name = val.get("n") if isinstance(val.get("n"), str) else val.get("name")
        if not isinstance(name, str):
            continue
        label = " ".join(name.casefold().split())
        if not label or len(label) > 40:
            continue
        pid = val.get("id")
        if isinstance(pid, bool) or not isinstance(pid, int):
            try:
                pid = int(key)
            except (TypeError, ValueError):
                continue
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 0 or pid > 65535:
            continue
        if label not in out:
            out[label] = pid
    return out


_WHITE = {"on": True, "bri": 255, "seg": [{"col": [[255, 255, 255]]}]}


def blink(host):
    """Save state, short white flash, restore. 'ok', 'refused', or 'not_wled'."""
    if not host_ok(host):
        return "refused"
    shown = display_host(host)
    status, state = fetch(shown, "/json/state")
    if status != "ok" or not isinstance(state, dict):
        return "refused" if status == "refused" else "not_wled"
    posted, _doc = fetch(shown, "/json/state", "POST", _WHITE)
    if posted != "ok":
        return "not_wled"
    time.sleep(BLINK_S)
    restored, _doc = fetch(shown, "/json/state", "POST", state)
    if restored != "ok":
        return "not_wled"
    return "ok"


def _encode_name(name):
    out = b""
    for label in name.split("."):
        raw = label.encode("ascii")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def mdns_query():
    """One QU-bit PTR question for _wled._tcp.local."""
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    question = _encode_name(_SERVICE) + struct.pack("!HH", 12, 0x8001)
    return header + question


def _read_name(buf, pos, seen=None):
    """(name, next_pos) or None. next_pos stays on the original name, not a pointer target."""
    if seen is None:
        seen = set()
    labels = []
    hops = 0
    orig = None
    while True:
        if pos < 0 or pos >= len(buf) or pos in seen or hops > 16 or len(labels) > 32:
            return None
        seen.add(pos)
        length = buf[pos]
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(buf):
                return None
            ptr = ((length & 0x3F) << 8) | buf[pos + 1]
            if ptr >= len(buf):
                return None
            if orig is None:
                orig = pos + 2
            pos = ptr
            hops += 1
            continue
        if length & 0xC0:
            return None
        if length == 0:
            if orig is None:
                orig = pos + 1
            return ".".join(labels), orig
        if length > 63 or pos + 1 + length > len(buf):
            return None
        try:
            label = buf[pos + 1:pos + 1 + length].decode("ascii")
        except UnicodeDecodeError:
            return None
        if not label:
            return None
        labels.append(label)
        pos += 1 + length


def parse_mdns(packet):
    """PTR/SRV/A answers as [{name, host, port}]. [] on any bounds or pointer problem. Never raises."""
    try:
        return _parse_mdns(packet)
    except Exception:
        return []


def _parse_mdns(packet):
    if not isinstance(packet, (bytes, bytearray)) or len(packet) < 12:
        return []
    qd, an, ns, ar = struct.unpack("!HHHH", packet[4:12])
    if qd > 8 or an + ns + ar > 64:
        return []
    pos = 12
    for _ in range(qd):
        got = _read_name(packet, pos)
        if not got or got[1] + 4 > len(packet):
            return []
        pos = got[1] + 4
    answers = []
    for _ in range(an + ns + ar):
        got = _read_name(packet, pos)
        if not got:
            return []
        name, pos = got
        if pos + 10 > len(packet):
            return []
        typ, _cls, _ttl, rdlen = struct.unpack("!HHIH", packet[pos:pos + 10])
        pos += 10
        if rdlen > 512 or pos + rdlen > len(packet):
            return []
        answers.append((name.rstrip(".").lower(), typ, pos, rdlen))
        pos += rdlen
    ptrs, srvs, addrs = {}, {}, {}
    for name, typ, at, rdlen in answers:
        if typ == 12:
            got = _read_name(packet, at)
            if got:
                ptrs.setdefault(name, []).append(got[0].rstrip("."))
        elif typ == 33 and rdlen >= 7:
            port = struct.unpack("!H", packet[at + 4:at + 6])[0]
            got = _read_name(packet, at + 6)
            if got and 0 < port < 65536:
                srvs[name] = (port, got[0].rstrip("."))
        elif typ == 1 and rdlen == 4:
            addrs[name] = ".".join(str(b) for b in packet[at:at + 4])
    found = []
    seen = set()
    for targets in ptrs.values():
        for target in targets:
            key = target.lower()
            if key in seen:
                continue
            seen.add(key)
            port, host = srvs.get(key, (80, ""))
            ip = addrs.get(host.lower()) if host else None
            if not host and not ip:
                continue
            use = host if host.lower().endswith(".local") else (ip or host)
            if not use:
                continue
            label = target.split(".")[0].replace("-", " ") or "WLED"
            found.append({"name": label[:40], "host": use, "port": port})
            if len(found) >= 32:
                return found
    return found


def _mdns_source_ok(addr):
    """A reply counts only from a private or link-local IPv4 source on port 5353 (0.13.0, Nix I2)."""
    if not isinstance(addr, tuple) or len(addr) < 2 or addr[1] != MDNS_GROUP[1]:
        return False
    ip = _ip(addr[0])
    if not isinstance(ip, ipaddress.IPv4Address) or ip.is_loopback or ip.is_multicast or ip.is_unspecified:
        return False
    if ip.is_link_local:
        return ip != _META
    b = ip.packed
    return b[0] == 10 or (b[0] == 172 and 16 <= b[1] <= 31) or (b[0] == 192 and b[1] == 168)


_new_socket = socket.socket  # tests replace this


def lan_address():
    """The station's LAN IPv4 (the route a private address would take), or "". Nothing is sent."""
    try:
        probe = _new_socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return ""
    try:
        probe.connect(("192.0.2.1", 9))
        ip = _ip(probe.getsockname()[0])
        return str(ip) if ip is not None and _mdns_source_ok((str(ip), MDNS_GROUP[1])) else ""
    except OSError:
        return ""
    finally:
        probe.close()



def mdns_browse(timeout=DISCOVER_S):
    """One-shot QU query on the LAN interface. Ephemeral UDP socket, closed in finally."""
    lan = lan_address()
    if not lan:  # 0.13.0 (Nix Info I2): no LAN address, no search; never bind to every interface
        return []
    sock = _new_socket(socket.AF_INET, socket.SOCK_DGRAM)
    found = []
    try:
        sock.bind((lan, 0))
        sock.settimeout(0.2)
        try:
            sock.sendto(mdns_query(), MDNS_GROUP)
        except OSError:
            return []
        deadline = time.monotonic() + timeout
        packets = 0
        while time.monotonic() < deadline and packets < 64:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            packets += 1
            if not _mdns_source_ok(addr):
                continue
            for item in parse_mdns(data):
                if item not in found:
                    found.append(item)
                if len(found) >= 32:
                    return found
        return found
    finally:
        sock.close()


def join_host(item):
    """Browse row -> host string the probe uses."""
    if not isinstance(item, dict):
        return "", ""
    host = item.get("host")
    name = item.get("name") if isinstance(item.get("name"), str) else ""
    if not isinstance(host, str) or not host.strip():
        return "", name
    port = item.get("port")
    text = host.strip()
    if isinstance(port, int) and not isinstance(port, bool) and port not in (0, 80) and ":" not in text.split("%")[0]:
        text = text + ":" + str(port)
    return text, name
