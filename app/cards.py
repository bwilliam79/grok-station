"""Versioned display cards. Stdlib only.

Bots send data. Nothing here runs a command, fetches a URL that is not on the
logo allowlist, or treats card text as markup. Text is stripped, then drawn as
pixels by the station.
"""
import base64, hashlib, http.client, ipaddress, json, os, re, socket, ssl, tempfile, threading, time, unicodedata
from urllib.parse import urlsplit

CARD_MAX = 4096
TYPES = frozenset(("recipe", "sports", "calendar", "home"))
CARD_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
BOT_SLUG = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
ABBR = re.compile(r"^[A-Z0-9]{2,4}$")
LOGO_ID = re.compile(r"^[a-f0-9]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HOST_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_BAD_HOST = ("local", "lan", "internal", "localhost", "home.arpa", "localdomain")
STATUSES = frozenset(("live", "final", "scheduled", "delayed"))
TRENDS = frozenset(("up", "down"))
TTL_HARD = 86400
TTL_FLOOR = 60
ROTATE_DEFAULT = 20
MAX_CARDS = 24
PER_BOT = 8
HOSTS_MAX = 8
LOGO_MAX = 160 * 1024  # 0.12.11: many 500 px college logos are 80-110 KiB (64 KiB dropped them)
LOGO_PX = 512
LOGO_FILES = 32
LOGO_TOTAL = 4 << 20
LOGO_MAX_AGE = 7 * 24 * 3600
# Conversation cards (recipe, sports, calendar) replace each other. Home cards still stack.
CONVO_TYPES = frozenset(("recipe", "sports", "calendar"))
CONVO_IDLE_S = 60          # calendar leaves this long after the conversation goes idle
SPORTS_TILE_S = 15         # 0.12.11: a score tile leaves 15 s after it showed (or when speech ends, if later)
RECIPE_IDLE_S = 30 * 60    # a recipe stays while someone is cooking; refreshed by a step command
LOGO_WAIT_S = 0.0          # 0.12.11: show the card now; logos fill in when their download lands
ESPN_LOGO_HOST = "a.espncdn.com"
# Documented team-logo file. The /combiner/ resize URL is a different path and is not fetched here.
_ESPN_TEAMLOGO = re.compile(r"^/i/teamlogos/[a-z0-9][a-z0-9-]{0,31}/\d{2,4}/[a-z0-9]{1,16}\.png$")
_PUT_KEYS = frozenset(("v", "op", "id", "priority", "ttl_s", "card", "kind", "nonce", "exp_ms"))
_CLEAR_KEYS = frozenset(("v", "op", "id", "kind", "nonce", "exp_ms"))
_NONCE_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_REASONS = frozenset(("type", "schema", "size", "ttl", "escape"))


class CardError(ValueError):
    """A card or show document the station must not keep. reason is safe to log."""

    def __init__(self, reason="schema"):
        self.reason = reason if reason in _REASONS else "schema"
        super().__init__(self.reason)


def bot_slug_ok(name):
    """Same shape as relay handoff.BOT_SLUG, and not the built-in voice."""
    return isinstance(name, str) and name != "grok" and BOT_SLUG.fullmatch(name) is not None


def ttl_cap(raw):
    """Hard max 86400. A whole number from 60 up is clamped to that max. Anything else stays 86400."""
    if raw is None or raw == "":
        return TTL_HARD
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return TTL_HARD
    if v < TTL_FLOOR or type(raw) is bool:
        return TTL_HARD
    return TTL_HARD if v > TTL_HARD else v


def rotate_s(raw):
    """Seconds between cards. Default 20. A whole number from 5 to 120 is kept; anything else is 20."""
    if raw is None or raw == "":
        return ROTATE_DEFAULT
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return ROTATE_DEFAULT
    if type(raw) is bool or v < 5 or v > 120:
        return ROTATE_DEFAULT
    return v


def escape_text(value, cap):
    """NFKC, drop markup and control/bidi characters, collapse spaces, then enforce the cap.

    Idempotent. A value that is not text, or that is empty after cleaning, is an error.
    The cap is a schema failure (the text was too long), not an escape failure.
    """
    if not isinstance(value, str):
        raise CardError("escape")
    s = unicodedata.normalize("NFKC", value)
    s = re.sub(r"<[^>]*>", "", s)
    out = []
    for ch in s:
        if ch in "<>&\"'`":
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cf", "Cc", "Cs", "Co") or ord(ch) < 32:
            continue
        out.append(ch)
    cleaned = " ".join("".join(out).split())
    if not cleaned:
        raise CardError("escape")
    if len(cleaned) > cap:
        raise CardError("schema")
    return cleaned


def _int(value, lo, hi):
    if type(value) is not int or value < lo or value > hi:
        raise CardError("schema")
    return value


def _raw_size(card):
    try:
        n = len(json.dumps(card, separators=(",", ":"), ensure_ascii=False).encode())
    except (TypeError, ValueError):
        raise CardError("size")
    if n > CARD_MAX:
        raise CardError("size")


def _keys(doc, allowed, required=None):
    if not isinstance(doc, dict) or not set(doc) <= allowed:
        raise CardError("schema")
    if required is not None and not required <= set(doc):
        raise CardError("schema")


def host_ok(host):
    """A public DNS name: dotted, letter TLD, not an address, not a local suffix."""
    if not isinstance(host, str):
        return False
    h = host.strip().lower().rstrip(".")
    if not h or len(h) > 253 or ".." in h or any(ord(c) > 126 for c in h):
        return False
    for s in _BAD_HOST:
        if h == s or h.endswith("." + s):
            return False
    try:
        ipaddress.ip_address(h)
        return False
    except ValueError:
        pass
    labels = h.split(".")
    if len(labels) < 2 or not re.fullmatch(r"[a-z]{2,63}", labels[-1]):
        return False
    return all(HOST_LABEL.fullmatch(lab) for lab in labels)


def normalize_hosts(items):
    """A list of at most 8 legal hosts, lowercased, or None if anything is illegal."""
    if not isinstance(items, list) or len(items) > HOSTS_MAX:
        return None
    out = []
    for item in items:
        if not isinstance(item, str):
            return None
        h = item.strip().lower().rstrip(".")
        if not h or not host_ok(h):
            return None
        if h not in out:
            out.append(h)
    return out


def parse_hosts(value):
    """Admin input: a list, or a comma/newline string. Blank is an empty allowlist. Illegal is None."""
    if isinstance(value, list):
        return normalize_hosts(value)
    if not isinstance(value, str):
        return None
    if not value.strip():
        return []
    parts = [p.strip() for p in value.replace(",", "\n").splitlines() if p.strip()]
    return normalize_hosts(parts)


# ESPN's public team-logo CDN (a.espncdn.com/i/teamlogos/<league>/500/<abbr>.png, or the
# /combiner/i?img=...&w=200&h=200 resize, which stays well under the 64 KiB cap). Used until the
# user saves their own list on the Display page; an explicitly saved empty list stays empty.
DEFAULT_LOGO_HOSTS = ("a.espncdn.com",)


def hosts_from_env(raw):
    """Runtime allowlist. Unset means the default; one illegal token, or more than 8, fails closed to no hosts."""
    if raw is None:
        return list(DEFAULT_LOGO_HOSTS)
    if raw == "":
        return []
    if not isinstance(raw, str):
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) > HOSTS_MAX:
        return []
    out = []
    for p in parts:
        h = p.strip().lower().rstrip(".")
        if not host_ok(h):
            return []
        if h not in out:
            out.append(h)
    return out


def _split_url(url):
    if not isinstance(url, str) or not url or len(url) > 200:
        return None
    if any(c.isspace() or c in "<>\"'`" for c in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or parts.fragment:
        return None
    host = parts.hostname
    if not host_ok(host or ""):
        return None
    return parts


def logo_url_shape(url):
    """HTTPS URL shape only. The allowlist is checked later, at fetch time."""
    return _split_url(url) is not None


def canonical_logo_url(url):
    parts = _split_url(url)
    if parts is None:
        raise CardError("schema")
    host = parts.hostname.lower().rstrip(".")
    port = f":{parts.port}" if parts.port and parts.port != 443 else ""
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"https://{host}{port}{path}{query}"


def logo_url_ok(url, hosts):
    if not logo_url_shape(url):
        return False
    host = urlsplit(url).hostname.lower().rstrip(".")
    allow = set()
    for h in hosts or ():
        if isinstance(h, str):
            allow.add(h.strip().lower().rstrip("."))
    return host in allow


def espn_teamlogo_url(url):
    """True only for https://a.espncdn.com/i/teamlogos/<league>/<size>/<abbr>.png.

    The combiner URL (a query string on /combiner/) is not this path. Other hosts are not.
    """
    parts = _split_url(url)
    if parts is None:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if host != ESPN_LOGO_HOST:
        return False
    if parts.query or (parts.port and parts.port != 443):
        return False
    return _ESPN_TEAMLOGO.fullmatch(parts.path or "") is not None


def ip_ok(ip):
    """False for private, loopback, link-local, multicast, reserved, or unspecified."""
    try:
        addr = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast:
        return False
    if addr.is_reserved or addr.is_unspecified or not addr.is_global:
        return False
    return True


def _same_ip(a, b):
    try:
        left = ipaddress.ip_address(str(a).split("%", 1)[0])
        right = ipaddress.ip_address(str(b).split("%", 1)[0])
    except ValueError:
        return False
    left = getattr(left, "ipv4_mapped", None) or left
    right = getattr(right, "ipv4_mapped", None) or right
    return left == right


def _lookup_ips(host, port, resolve=None):
    """One lookup. resolve(host) -> list of addresses, for tests. The name is not looked up again."""
    if resolve is not None:
        got = resolve(host)
        if isinstance(got, str):
            return [got]
        if isinstance(got, (list, tuple)):
            return [str(item) for item in got]
        return []
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []
    out = []
    for info in infos:
        try:
            out.append(info[4][0])
        except (IndexError, TypeError):
            return []
    return out


def pin_ip(host, port=443, resolve=None):
    """One address from one lookup, or None unless every answer passes ip_ok.

    IPv4 first when the answer has one: many home networks (the station's included) have no IPv6
    route, and a CDN answer that lists IPv6 first made every logo download fail (0.12.6)."""
    addrs = _lookup_ips(host, port, resolve)
    if not addrs:
        return None
    for ip in addrs:
        if not ip_ok(ip):
            return None
    for ip in addrs:
        if ":" not in str(ip):
            return ip
    return addrs[0]


def public_host(host, resolve=None):
    """True only when every resolved address is public. resolve(host) is a test hook (true/false)."""
    if resolve is not None:
        return bool(resolve(host))
    if not host_ok(host):
        return False
    return pin_ip(host, 443) is not None


def _peer_is_pin(sock, pin):
    try:
        got = sock.getpeername()[0]
    except Exception:
        return False
    return ip_ok(got) and _same_ip(got, pin)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TCP to a pinned address. SNI and the certificate check use the hostname, not the IP."""

    def __init__(self, host, pin, port=None, timeout=3, context=None):
        super().__init__(host, port or 443, timeout=timeout, context=context)
        self._pin = pin

    def _open_tcp(self):
        return socket.create_connection((self._pin, self.port), self.timeout)

    def connect(self):
        raw = self._open_tcp()
        wrapped = None
        try:
            if not _peer_is_pin(raw, self._pin):
                raise CardError("schema")
            wrapped = self._context.wrap_socket(raw, server_hostname=self.host)
            if not _peer_is_pin(wrapped, self._pin):
                raise CardError("schema")
        except Exception:
            try:
                (wrapped or raw).close()
            except Exception:
                pass
            self.sock = None
            raise
        self.sock = wrapped


def _png_ok(data):
    if len(data) < 29 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    length = int.from_bytes(data[8:12], "big")
    if length != 13 or data[12:16] != b"IHDR":
        return False
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    bit, color = data[24], data[25]
    if not (1 <= w <= LOGO_PX and 1 <= h <= LOGO_PX):
        return False
    if bit not in (1, 2, 4, 8, 16) or color not in (0, 2, 3, 4, 6):
        return False
    return True


def _jpeg_ok(data):
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        return False
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            return False
        while i < len(data) and data[i] == 0xFF:
            i += 1
        if i >= len(data):
            return False
        marker = data[i]
        i += 1
        if marker in (0xD8,):
            continue
        if marker == 0xD9:
            return False
        if marker == 0x00 or 0xD0 <= marker <= 0xD7:
            continue
        if i + 2 > len(data):
            return False
        seglen = int.from_bytes(data[i:i + 2], "big")
        if seglen < 2 or i + seglen > len(data):
            return False
        if marker in (0xC0, 0xC1, 0xC2):
            if seglen < 7:
                return False
            h = int.from_bytes(data[i + 3:i + 5], "big")
            w = int.from_bytes(data[i + 5:i + 7], "big")
            return 1 <= w <= LOGO_PX and 1 <= h <= LOGO_PX
        if marker == 0xDA:
            return False
        i += seglen
    return False


def sniff_image(data, max_bytes=LOGO_MAX):
    """PNG (IHDR) or JPEG (SOF0/1/2) only. SVG and oversize files are rejected."""
    if not isinstance(data, (bytes, bytearray)):
        return False
    if len(data) < 8 or len(data) > max_bytes:
        return False
    head = bytes(data[:256]).lower()
    if head.lstrip().startswith(b"<") or b"<svg" in head:
        return False
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png_ok(data)
    if data.startswith(b"\xff\xd8"):
        return _jpeg_ok(data)
    return False


def default_opener(url, resolve=None, connection=None):
    """GET one HTTPS image. No redirects. One DNS lookup, then a connection to that pinned address.

    The certificate is checked against the hostname (SNI is the hostname). After connect,
    getpeername() must still be that address. resolve(host) -> addresses is a test hook.
    """
    if _split_url(url) is None:
        raise CardError("schema")
    canon = canonical_logo_url(url)
    parts = urlsplit(canon)
    host, port = parts.hostname, parts.port or 443
    pin = pin_ip(host, port, resolve)
    if pin is None:
        raise CardError("schema")
    ctx = ssl.create_default_context()
    cls = connection or PinnedHTTPSConnection
    conn = cls(host, pin, port=port, timeout=3, context=ctx)
    path = parts.path or "/"
    if parts.query:
        path = path + "?" + parts.query
    try:
        conn.request("GET", path, headers={"User-Agent": "grok-station", "Accept": "image/png, image/jpeg"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise CardError("schema")
        ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in ("image/png", "image/jpeg"):
            raise CardError("schema")
        cl = resp.getheader("Content-Length")
        if cl is not None:
            try:
                n = int(cl)
            except ValueError:
                raise CardError("schema")
            if n > LOGO_MAX:
                raise CardError("size")
        data = resp.read(LOGO_MAX + 1)
    finally:
        conn.close()
    if len(data) > LOGO_MAX:
        raise CardError("size")
    return data, ctype


class LogoCache:
    """On-disk logo cache. Callers pass an opener in tests so this never touches the network."""

    def __init__(self, root, hosts, opener=None, resolve=None, max_files=LOGO_FILES,
                 max_total=LOGO_TOTAL, max_bytes=LOGO_MAX, max_age=LOGO_MAX_AGE, log=None):
        self.root = os.path.abspath(root)
        self.log = log or (lambda msg: print(msg, flush=True))
        self.hosts = list(hosts or [])
        self.opener = opener
        self.resolve = resolve
        self.max_files = max_files
        self.max_total = max_total
        self.max_bytes = max_bytes
        self.max_age = max_age
        self._lock = threading.Lock()
        self._pending = set()

    def skipped(self, reason):
        """One log line per skipped logo. The reason only, never the URL."""
        try:
            self.log("logo skipped: " + reason)
        except Exception:
            pass

    def id_for(self, url):
        """Logo id for an allowlisted public URL, or None. Does not fetch."""
        if not logo_url_ok(url, self.hosts):
            self.skipped("host not allowed" if self.hosts else "no logo hosts")
            return None
        try:
            canon = canonical_logo_url(url)
        except CardError:
            self.skipped("bad url")
            return None
        if not public_host(urlsplit(canon).hostname, self.resolve):
            self.skipped("host not public or lookup failed")
            return None
        return hashlib.sha256(canon.encode()).hexdigest()

    def fetch_later(self, url):
        """Return the logo id and download in the background when the file is not here yet.

        A cached file is not downloaded again. The caller can remember the id immediately;
        drawing uses a monogram until the file exists.
        """
        lid = self.id_for(url)
        if lid is None:
            return None
        path = self.path_for(lid)
        if path and self._fresh(path):
            return lid
        with self._lock:
            if lid in self._pending:
                return lid
            self._pending.add(lid)

        def run():
            try:
                self.fetch(url)
            finally:
                with self._lock:
                    self._pending.discard(lid)

        threading.Thread(target=run, daemon=True).start()
        return lid

    def path_for(self, logo_id):
        if not isinstance(logo_id, str) or not LOGO_ID.fullmatch(logo_id):
            return None
        path = os.path.abspath(os.path.join(self.root, logo_id))
        if os.path.dirname(path) != self.root:
            return None
        return path

    def fetch(self, url):
        """Return a logo id, or None. Not allowlisted, private, or a bad image: None, and no file."""
        if not logo_url_ok(url, self.hosts):
            return None
        try:
            canon = canonical_logo_url(url)
        except CardError:
            return None
        lid = hashlib.sha256(canon.encode()).hexdigest()
        path = self.path_for(lid)
        if path and self._fresh(path):
            return lid
        host = urlsplit(canon).hostname
        try:
            if self.opener is not None:
                if not public_host(host, self.resolve):
                    self.skipped("host not public or lookup failed")
                    return None
                opened = self.opener(canon)
            else:
                opened = default_opener(canon)
            data, ctype = opened[0], opened[1]
        except CardError as e:
            self.skipped("too big" if str(e) == "size" else "download refused")
            return None
        except Exception as e:
            self.skipped("download failed (" + type(e).__name__ + ")")
            return None
        if ctype not in ("image/png", "image/jpeg"):
            self.skipped("not a png or jpeg")
            return None
        if not isinstance(data, (bytes, bytearray)) or len(data) > self.max_bytes or not sniff_image(data, self.max_bytes):
            self.skipped("bad image")
            return None
        with self._lock:
            self._store(lid, bytes(data))
        return lid

    def _fresh(self, path):
        """A stored logo is reusable for about a week. Older files are fetched again."""
        try:
            st = os.stat(path)
        except OSError:
            return False
        if st.st_size <= 0 or st.st_size > self.max_bytes:
            return False
        return (time.time() - st.st_mtime) <= self.max_age

    def _files(self):
        rows = []
        if not os.path.isdir(self.root):
            return rows
        for name in os.listdir(self.root):
            if not LOGO_ID.fullmatch(name):
                continue
            path = os.path.join(self.root, name)
            if not os.path.isfile(path):
                continue
            st = os.stat(path)
            rows.append((st.st_mtime, path, st.st_size))
        rows.sort()
        return rows

    def _evict(self, extra=0):
        now = time.time()
        rows = []
        for mt, path, sz in self._files():
            if now - mt > self.max_age:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            rows.append((mt, path, sz))
        total = sum(sz for _mt, _p, sz in rows)
        while rows and (len(rows) + (1 if extra else 0) > self.max_files or total + extra > self.max_total):
            _mt, path, sz = rows.pop(0)
            try:
                os.unlink(path)
            except OSError:
                pass
            total -= sz

    def _store(self, lid, data):
        os.makedirs(self.root, exist_ok=True)
        try:
            os.chmod(self.root, 0o755)
        except OSError:
            pass
        self._evict(extra=len(data))
        path = self.path_for(lid)
        if path is None:
            return
        fd, tmp = tempfile.mkstemp(prefix=".logo-", dir=self.root)
        try:
            os.write(fd, data)
            os.fchmod(fd, 0o644)
            os.close(fd)
            fd = -1
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
            os.chmod(path, 0o644)
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        self._evict()


def _list(items, lo, hi, cap, clean):
    if not isinstance(items, list) or not lo <= len(items) <= hi:
        raise CardError("schema")
    return [clean(item, cap) for item in items]


def _team(doc):
    _keys(doc, {"name", "abbr", "logo_url", "logo_id", "logo_badge"}, {"name", "abbr"})
    if "logo_url" in doc and "logo_id" in doc:
        raise CardError("schema")
    out = {"name": escape_text(doc.get("name"), 24), "abbr": doc.get("abbr")}
    if not isinstance(out["abbr"], str) or not ABBR.fullmatch(out["abbr"]):
        raise CardError("schema")
    if "logo_url" in doc:
        if not logo_url_shape(doc.get("logo_url")):
            raise CardError("schema")
        out["logo_url"] = canonical_logo_url(doc["logo_url"])
    if "logo_id" in doc:
        if not isinstance(doc.get("logo_id"), str) or not LOGO_ID.fullmatch(doc["logo_id"]):
            raise CardError("schema")
        out["logo_id"] = doc["logo_id"]
    if "logo_badge" in doc:
        if doc.get("logo_badge") is not True:
            raise CardError("schema")
        out["logo_badge"] = True
    return out


def _recipe(card):
    _keys(card, {"type", "v", "title", "servings", "ingredients", "steps", "step"},
          {"type", "v", "title", "servings", "ingredients", "steps", "step"})
    steps = _list(card.get("steps"), 1, 12, 160, escape_text)
    return {"type": "recipe", "v": 1, "title": escape_text(card.get("title"), 60),
            "servings": _int(card.get("servings"), 1, 48),
            "ingredients": _list(card.get("ingredients"), 1, 12, 48, escape_text),
            "steps": steps, "step": _int(card.get("step"), 1, len(steps))}


def _sports(card):
    _keys(card, {"type", "v", "league", "home", "away", "home_score", "away_score", "status", "period", "schedule"},
          {"type", "v", "league", "home", "away", "home_score", "away_score", "status", "period", "schedule"})
    if card.get("status") not in STATUSES:
        raise CardError("schema")
    sched = card.get("schedule")
    if not isinstance(sched, list) or len(sched) > 4:
        raise CardError("schema")
    games = []
    for g in sched:
        _keys(g, {"label", "when"}, {"label", "when"})
        games.append({"label": escape_text(g.get("label"), 48), "when": escape_text(g.get("when"), 24)})
    return {"type": "sports", "v": 1, "league": escape_text(card.get("league"), 24),
            "home": _team(card.get("home")), "away": _team(card.get("away")),
            "home_score": _int(card.get("home_score"), 0, 999), "away_score": _int(card.get("away_score"), 0, 999),
            "status": card["status"], "period": escape_text(card.get("period"), 16), "schedule": games}


def _date(value):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise CardError("schema")
    month, day = int(value[5:7]), int(value[8:10])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise CardError("schema")
    return value


def _calendar(card):
    _keys(card, {"type", "v", "date", "events"}, {"type", "v", "date", "events"})
    events = card.get("events")
    if not isinstance(events, list) or len(events) > 8:
        raise CardError("schema")
    rows = []
    for ev in events:
        _keys(ev, {"time", "title"}, {"time", "title"})
        rows.append({"time": escape_text(ev.get("time"), 16), "title": escape_text(ev.get("title"), 48)})
    return {"type": "calendar", "v": 1, "date": _date(card.get("date")), "events": rows}


def _home(card):
    _keys(card, {"type", "v", "title", "items"}, {"type", "v", "title", "items"})
    items = card.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 6:
        raise CardError("schema")
    rows = []
    for item in items:
        _keys(item, {"label", "value", "trend"}, {"label", "value"})
        row = {"label": escape_text(item.get("label"), 32), "value": escape_text(item.get("value"), 24)}
        if "trend" in item:
            if item.get("trend") not in TRENDS:
                raise CardError("schema")
            row["trend"] = item["trend"]
        rows.append(row)
    return {"type": "home", "v": 1, "title": escape_text(card.get("title"), 24), "items": rows}


_BUILD = {"recipe": _recipe, "sports": _sports, "calendar": _calendar, "home": _home}


def validate_card(card):
    """Return a cleaned card. Unknown type, bad version, extra keys, and oversize documents fail."""
    if not isinstance(card, dict):
        raise CardError("schema")
    _raw_size(card)
    kind = card.get("type")
    if kind not in TYPES:
        raise CardError("type")
    if type(card.get("v")) is not int or card.get("v") != 1:
        raise CardError("schema")
    cleaned = _BUILD[kind](card)
    _raw_size(cleaned)
    return cleaned


def nonce_ok(value):
    """URL-safe base64 (no padding) of at least 16 bytes. Standard base64 and short values fail."""
    if not isinstance(value, str) or not 22 <= len(value) <= 200:
        return False
    if any(c not in _NONCE_ALPHABET for c in value):
        return False
    try:
        raw = base64.urlsafe_b64decode(value + ("=" * ((-len(value)) % 4)))
    except Exception:
        return False
    return len(raw) >= 16


def _replay(doc):
    """kind, nonce, and exp_ms. The time window is the relay's job; this only checks shape."""
    if doc.get("kind") != "show" or not nonce_ok(doc.get("nonce")) or type(doc.get("exp_ms")) is not int:
        raise CardError("schema")
    return {"kind": "show", "nonce": doc["nonce"], "exp_ms": doc["exp_ms"]}


def validate_show(doc, ttl_max):
    """A put or clear. No bot field: the relay stamps the verified slug after the signature check.

    kind is show. nonce and exp_ms travel with the card so a later check of the same document
    still matches. This does not look at the clock.
    """
    if not isinstance(doc, dict):
        raise CardError("schema")
    if type(doc.get("v")) is not int or doc.get("v") != 1:
        raise CardError("schema")
    op = doc.get("op")
    if op == "clear":
        if set(doc) != _CLEAR_KEYS:
            raise CardError("schema")
    elif op == "put":
        if set(doc) != _PUT_KEYS:
            raise CardError("schema")
    else:
        raise CardError("schema")
    if not isinstance(doc.get("id"), str) or not CARD_ID.fullmatch(doc["id"]):
        raise CardError("schema")
    replay = _replay(doc)
    if op == "clear":
        return {"v": 1, "op": "clear", "id": doc["id"], **replay}
    if type(ttl_max) is not int or ttl_max < 1:
        raise CardError("ttl")
    priority = doc.get("priority")
    ttl_s = doc.get("ttl_s")
    if type(priority) is not int or not 0 <= priority <= 100:
        raise CardError("schema")
    if type(ttl_s) is not int or ttl_s < 1 or ttl_s > ttl_max:
        raise CardError("ttl")
    card = validate_card(doc.get("card"))
    return {"v": 1, "op": "put", "id": doc["id"], "priority": priority, "ttl_s": ttl_s, "card": card, **replay}


def _logo_file_ready(cache, logo_id):
    """True when this cache has a non-empty file for the id. Does not fetch."""
    if cache is None or not isinstance(logo_id, str):
        return False
    path = cache.path_for(logo_id)
    if not path or not os.path.isfile(path):
        return False
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    return 0 < size <= getattr(cache, "max_bytes", LOGO_MAX)


def sports_idle_span(shown_at, now, hold_s=SPORTS_TILE_S):
    """Seconds from now until a score tile leaves: hold_s after it showed, never before now."""
    try:
        return max(0.0, float(shown_at) + float(hold_s) - float(now))
    except (TypeError, ValueError):
        return float(hold_s)


_NCAA_PATH = re.compile(r"^/i/teamlogos/ncaa[a-z0-9-]*/\d{2,4}/([a-z0-9]{1,16})\.png$")


def logo_known_missing(url):
    """True for a college logo URL ESPN cannot serve: college logos are named by the numeric ESPN
    team id (ncaa/500/2250.png), never by an abbreviation (ncaa/500/gonz.png is a 404)."""
    parts = _split_url(url)
    m = _NCAA_PATH.fullmatch((parts.path or "") if parts else "")
    return bool(m) and not m.group(1).isdigit()


def logo_id_of(url):
    """The cache id a fetch of this URL will store. Does not fetch or resolve."""
    return hashlib.sha256(canonical_logo_url(url).encode()).hexdigest()


def prepare_display_logos(card, cache, wait_s=LOGO_WAIT_S, on_ready=None):
    """Settle ESPN team logos for a sports card without holding the card back. Other cards are only re-validated.

    HTTPS only, host a.espncdn.com, the /i/teamlogos/ file path only, and that host must
    be on the cache allowlist. A cached logo is used at once. Otherwise the side shows the
    team's abbreviation badge, keeps the logo id, and the download runs in the background;
    on_ready() is called when a file lands so the display repaints with the logo. A logo that
    never arrives stays the abbreviation badge. No URL and no badge is the name alone.
    """
    card = validate_card(card)
    if card["type"] != "sports":
        return card
    jobs = []
    for side in ("home", "away"):
        team = card[side]
        url = team.get("logo_url")
        if not url:
            team.pop("logo_url", None)
            if _logo_file_ready(cache, team.get("logo_id")):
                team.pop("logo_badge", None)
            else:
                team.pop("logo_id", None)
                if team.get("logo_badge") is not True:
                    team.pop("logo_badge", None)
            continue
        allowed = (cache is not None and espn_teamlogo_url(url) and not logo_known_missing(url)
                   and logo_url_ok(url, getattr(cache, "hosts", ())))
        team.pop("logo_url", None)
        team.pop("logo_id", None)
        team["logo_badge"] = True
        if not allowed:
            continue
        try:
            lid = logo_id_of(url)
        except CardError:
            continue
        team["logo_id"] = lid
        fresh = getattr(cache, "_fresh", None)
        if _logo_file_ready(cache, lid) and (fresh is None or fresh(cache.path_for(lid))):
            team.pop("logo_badge", None)
            continue
        box = {}

        def run(u=url, box=box, cache=cache):
            try:
                box["id"] = cache.fetch(u)
            except Exception:
                box["id"] = None
            if box["id"] and on_ready is not None:
                try:
                    on_ready()
                except Exception:
                    pass

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        jobs.append((team, thread, box))
    deadline = time.monotonic() + max(0.0, float(wait_s))
    for team, thread, box in jobs:
        left = deadline - time.monotonic()
        if left > 0:
            thread.join(left)
        lid = box.get("id")
        if lid and _logo_file_ready(cache, lid):
            team["logo_id"] = lid
            team.pop("logo_badge", None)
    return validate_card(card)


def settle_logos(card, cache):
    """Drop logo URLs. Keep a logo id only when this cache stored the image. Never fetch while drawing."""
    card = validate_card(card)
    if card["type"] != "sports" or cache is None:
        for side in ("home", "away"):
            team = card.get(side)
            if isinstance(team, dict):
                team.pop("logo_url", None)
                if cache is None:
                    team.pop("logo_id", None)
        return validate_card(card)
    for side in ("home", "away"):
        team = card[side]
        url = team.pop("logo_url", None)
        team.pop("logo_id", None)
        if url:
            lid = cache.fetch_later(url)
            if isinstance(lid, str) and LOGO_ID.fullmatch(lid):
                team["logo_id"] = lid
    return validate_card(card)


def _copy(doc):
    return json.loads(json.dumps(doc))


# Whole-utterance recipe commands. A question that merely contains one of these words does not match.
_RECIPE_PHRASES = {
    "next": "next",
    "next step": "next",
    "previous step": "prev",
    "go back": "prev",
    "back": "prev",
    "repeat": "repeat",
    "repeat that step": "repeat",
    "what's this step": "repeat",
    "whats this step": "repeat",
    "what is this step": "repeat",
    "start over": "start",
    "first step": "start",
    "done": "close",
    "close recipe": "close",
}
LAST_STEP_LINE = "That's the last step."
FIRST_STEP_LINE = "That's the first step."


def recipe_utterance(text):
    """A recipe-card command, or None.

    Exact after case, apostrophes, and surrounding punctuation are folded.
    An optional leading "hey <name>" and a trailing "please" are allowed.
    Anything else, including a question that contains "next", is None so it
    still goes to the bot.
    """
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = t.replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    t = " ".join(t.casefold().split())
    if not t:
        return None
    t = re.sub(r"^(?:hey [a-z0-9][a-z0-9' -]{0,40}?)[, ]+", "", t).strip()
    t = t.strip(" \t.!?")
    if t.endswith(" please"):
        t = t[: -len(" please")].strip(" \t.!?")
    return _RECIPE_PHRASES.get(t)


def apply_recipe_command(deck, cmd, now):
    """Advance, repeat, restart, or close the one recipe card. {ok: False} when none is up.

    A step change is spoken by the caller. The follow-up window is not this function's job.
    """
    if cmd not in ("next", "prev", "repeat", "start", "close"):
        return {"ok": False}
    if type(now) not in (int, float) or type(now) is bool:
        return {"ok": False}
    with deck._lock:
        deck._drop(now)
        it = next((x for x in deck._ordered() if x["card"]["type"] == "recipe"), None)
        if it is None:
            return {"ok": False}
        card = it["card"]
        steps = card["steps"]
        step = int(card["step"])
        n = len(steps)
        bot, cid = it["bot"], it["id"]
        if cmd == "close":
            deck._items = [x for x in deck._items if x is not it]
            deck._anchor = None
            return {"ok": True, "closed": True, "bot": bot, "id": cid, "step": step, "say": ""}
        if cmd == "next":
            if step >= n:
                say = LAST_STEP_LINE
            else:
                step += 1
                say = steps[step - 1]
        elif cmd == "prev":
            if step <= 1:
                say = FIRST_STEP_LINE
            else:
                step -= 1
                say = steps[step - 1]
        elif cmd == "start":
            step = 1
            say = steps[0]
        else:
            say = steps[step - 1]
        card["step"] = step
        it["step_at"] = now
        if it.get("idle_at") is not None:
            it["idle_at"] = min(it["expires"], now + RECIPE_IDLE_S)
        return {"ok": True, "closed": False, "bot": bot, "id": cid, "step": step,
                "say": say, "title": card.get("title") or ""}


class Deck:
    """In-memory cards for one station. Higher priority first, then older seq. Expired cards are gone."""

    def __init__(self, rotate_s=ROTATE_DEFAULT, ttl_max=TTL_HARD, max_cards=MAX_CARDS, per_bot=PER_BOT):
        self.rotate_s = rotate_s
        self.ttl_max = ttl_max
        self.max_cards = max_cards
        self.per_bot = per_bot
        self._lock = threading.RLock()
        self._items = []
        self._seq = 0
        self._anchor = None

    def _live(self, it, now):
        """ttl is the upper bound. An armed idle deadline can remove the card sooner."""
        if it["expires"] <= now:
            return False
        idle = it.get("idle_at")
        if idle is not None and idle <= now:
            return False
        return True

    def _drop(self, now):
        self._items = [it for it in self._items if self._live(it, now)]
        if not self._items:
            self._anchor = None

    def _ordered(self):
        return sorted(self._items, key=lambda it: (-it["priority"], it["seq"]))

    def _view(self, it):
        return {"bot": it["bot"], "id": it["id"], "priority": it["priority"], "seq": it["seq"],
                "expires": it["expires"], "card": _copy(it["card"])}

    def _current(self, now):
        self._drop(now)
        if not self._items:
            return None
        if self._anchor is None:
            self._anchor = now
        step = self.rotate_s if self.rotate_s > 0 else 1
        slot = int((now - self._anchor) // step)
        return self._view(self._ordered()[slot % len(self._items)])

    def put(self, bot, card_id, priority, ttl_s, card, now):
        if not bot_slug_ok(bot) or not isinstance(card_id, str) or not CARD_ID.fullmatch(card_id):
            raise CardError("schema")
        if type(priority) is not int or not 0 <= priority <= 100:
            raise CardError("schema")
        if type(ttl_s) is not int or ttl_s < 1 or ttl_s > self.ttl_max:
            raise CardError("ttl")
        if type(now) not in (int, float) or type(now) is bool:
            raise CardError("schema")
        card = validate_card(card)
        if card["type"] == "sports":
            for side in ("home", "away"):
                card[side].pop("logo_url", None)
            card = validate_card(card)
        with self._lock:
            self._drop(now)
            # One conversation card at a time, whatever the id or the bot. Home cards stay.
            if card["type"] in CONVO_TYPES:
                self._items = [it for it in self._items
                               if it["card"]["type"] not in CONVO_TYPES
                               or (it["bot"] == bot and it["id"] == card_id)]
            existing = next((it for it in self._items if it["bot"] == bot and it["id"] == card_id), None)
            if existing is None:
                if sum(1 for it in self._items if it["bot"] == bot) >= self.per_bot:
                    raise CardError("schema")
                if len(self._items) >= self.max_cards:
                    raise CardError("schema")
                self._seq += 1
                seq = self._seq
            else:
                seq = existing["seq"]
                self._items.remove(existing)
            self._items.append({"bot": bot, "id": card_id, "priority": priority, "seq": seq,
                                "expires": now + ttl_s, "card": card, "idle_at": None, "step_at": None})
            self._anchor = None

    def clear(self, bot, card_id):
        """Remove only this bot's card. Returns whether one was removed."""
        if not bot_slug_ok(bot) or not isinstance(card_id, str):
            return False
        with self._lock:
            before = len(self._items)
            self._items = [it for it in self._items if not (it["bot"] == bot and it["id"] == card_id)]
            if len(self._items) != before:
                self._anchor = None
                return True
            return False

    def get(self, bot, card_id):
        """This bot's card body by id, or None."""
        with self._lock:
            for it in self._items:
                if it["bot"] == bot and it["id"] == card_id:
                    return it["card"]
        return None

    def current(self, now):
        with self._lock:
            return self._current(now)

    def home_row(self, now, n=3):
        """Up to n home cards. If the card in the rotation slot is a home card, it is first."""
        with self._lock:
            cur = self._current(now)
            homes = [it for it in self._ordered() if it["card"]["type"] == "home"]
            if cur and cur["card"]["type"] == "home":
                key = (cur["bot"], cur["id"])
                keys = [(it["bot"], it["id"]) for it in homes]
                if key in keys:
                    i = keys.index(key)
                    homes = homes[i:] + homes[:i]
            return [self._view(it) for it in homes[:n]]

    def arm_idle(self, bot, card_id, now, sports_idle=CONVO_IDLE_S, recipe_idle=RECIPE_IDLE_S):
        """Start the idle clock for one conversation card. None when that card is not here.

        Sports and calendar use sports_idle from now. A recipe uses recipe_idle from the
        last step command, or from now when there has not been one. ttl still caps it.
        While a conversation is active this is not called, so only ttl applies.
        """
        if type(now) not in (int, float) or type(now) is bool:
            return None
        with self._lock:
            for it in self._items:
                if it["bot"] != bot or it["id"] != card_id:
                    continue
                if it["card"]["type"] not in CONVO_TYPES:
                    return None
                if it["card"]["type"] == "recipe":
                    base = it["step_at"] if it.get("step_at") is not None else now
                    span = recipe_idle
                else:
                    base, span = now, sports_idle
                it["idle_at"] = min(it["expires"], float(base) + float(span))
                return it["idle_at"]
            return None

    def idle_deadline(self, bot, card_id):
        """The armed idle time, or None when the card is not waiting on idle."""
        with self._lock:
            for it in self._items:
                if it["bot"] == bot and it["id"] == card_id:
                    return it.get("idle_at")
            return None

    def touch_recipe(self, bot, card_id, now, recipe_idle=RECIPE_IDLE_S):
        """Remember a step command. If the idle clock is already running, extend it."""
        if type(now) not in (int, float) or type(now) is bool:
            return False
        with self._lock:
            for it in self._items:
                if it["bot"] == bot and it["id"] == card_id and it["card"]["type"] == "recipe":
                    it["step_at"] = now
                    if it.get("idle_at") is not None:
                        it["idle_at"] = min(it["expires"], now + recipe_idle)
                    return True
            return False

    def seconds_until_change(self, now):
        """Seconds until the slot, the next expiry, or an armed idle deadline.

        None when nothing is showing. Does not set the anchor. At a boundary
        (including the moment a slot is chosen) the wait is a full rotate interval.
        """
        with self._lock:
            self._drop(now)
            if not self._items:
                return None
            anchor = self._anchor if self._anchor is not None else now
            step = self.rotate_s if self.rotate_s > 0 else 1
            into = (now - anchor) % step
            rot_left = step if into == 0 else step - into
            limits = []
            for it in self._items:
                limits.append(it["expires"] - now)
                if it.get("idle_at") is not None:
                    limits.append(it["idle_at"] - now)
            exp_left = min(limits)
            if exp_left < 0:
                exp_left = 0
            return min(rot_left, exp_left)
