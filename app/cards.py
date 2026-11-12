"""Versioned display cards. Stdlib only.

Bots send data. Nothing here runs a command, fetches a URL that is not on the
logo allowlist, or treats card text as markup. Text is stripped, then drawn as
pixels by the station.
"""
import base64, hashlib, http.client, ipaddress, json, math, os, re, socket, ssl, stat, sys, tempfile, threading, time, unicodedata
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
# 0.12.16: a sports or calendar card that arrives while a recipe is up does not end the recipe. It is shown as
# a chip on the recipe; the recipe stays the current card until it ends.
CONVO_TYPES = frozenset(("recipe", "sports", "calendar"))
ZONE_TYPES = frozenset(("sports", "calendar"))
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


UPD_ROTATE_CHOICES = (10, 15, 20, 30, 45, 60)
_ROTATE_SNAP_LOGGED = set()


def snap_upd_rotate(value):
    """Nearest allowed seconds. An equal distance takes the higher choice."""
    best, best_dist = None, None
    for choice in UPD_ROTATE_CHOICES:
        dist = abs(choice - value)
        if best is None or dist < best_dist or (dist == best_dist and choice > best):
            best, best_dist = choice, dist
    return best


def upd_rotate_s(raw=None, env=None):
    """Seconds before the whole updates band advances, home cards included.

    Reads GROK_STATION_CARD_ROTATE. Missing or unparseable is 20. A saved number
    that is not 10, 15, 20, 30, 45 or 60 snaps to the nearest choice (ties go up)
    and is logged once per process.
    """
    if raw is None:
        src = os.environ if env is None else env
        raw = src.get("GROK_STATION_CARD_ROTATE", "")
    if raw is None or raw == "" or (isinstance(raw, str) and not str(raw).strip()):
        return ROTATE_DEFAULT
    if type(raw) is bool:
        return ROTATE_DEFAULT
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return ROTATE_DEFAULT
    if value in UPD_ROTATE_CHOICES:
        return value
    choice = snap_upd_rotate(value)
    if value not in _ROTATE_SNAP_LOGGED:
        _ROTATE_SNAP_LOGGED.add(value)
        print(f"update rotation {value}s snapped to {choice}s", file=sys.stderr, flush=True)
    return choice


def rotate_s(raw):
    """The shared band interval. Same clock as upd_rotate_s."""
    return upd_rotate_s(raw)


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


def _dish_name(value):
    """Optional recipe field, 16 characters, kept exactly as the recipe gives it ("McRib", "Garlic bread").

    All caps or all lowercase is title-cased once here ("GARLIC BREAD" -> "Garlic Bread"). Noor, Oct 30.
    """
    if not isinstance(value, str) or len(" ".join(value.split())) > 16:
        raise CardError("schema")
    text = " ".join(escape_text(value, 16).split())
    if not text:
        raise CardError("schema")
    letters = [c for c in text if c.isalpha()]
    if letters and (all(c.isupper() for c in letters) or all(c.islower() for c in letters)):
        text = " ".join(w[:1].upper() + w[1:].lower() for w in text.split(" "))
    return text


def _recipe(card):
    _keys(card, {"type", "v", "title", "servings", "ingredients", "steps", "step", "dish"},
          {"type", "v", "title", "servings", "ingredients", "steps", "step"})
    steps = _list(card.get("steps"), 1, 12, 160, escape_text)
    out = {"type": "recipe", "v": 1, "title": escape_text(card.get("title"), 60),
           "servings": _int(card.get("servings"), 1, 48),
           "ingredients": _list(card.get("ingredients"), 1, 12, 48, escape_text),
           "steps": steps, "step": _int(card.get("step"), 1, len(steps))}
    if "dish" in card:
        # A malformed dish is dropped, not the recipe: the card still shows (R2-5).
        try:
            out["dish"] = _dish_name(card.get("dish"))
        except (CardError, TypeError, AttributeError, IndexError):
            pass
    return out


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
    "dismiss": "dismiss",  # 0.12.16: a chip on the recipe; the station says no when there is none
    "clear that": "dismiss",
}
LAST_STEP_LINE = "That's the last step."
FIRST_STEP_LINE = "That's the first step."


def fold_utterance(text):
    """Case, apostrophes, a leading "hey <name>", and one trailing "please"."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = t.replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    t = " ".join(t.casefold().split())
    if not t:
        return ""
    t = re.sub(r"^(?:hey [a-z0-9][a-z0-9' -]{0,40}?)[, ]+", "", t).strip()
    t = t.strip(" \t.!?")
    if t.endswith(" please"):
        t = t[: -len(" please")].strip(" \t.!?")
    return t


# Whole-utterance asks that should put a content tile on the station.
# sports, recipe, calendar, and home are deck cards. weather, time, and convert are station tool cards.
CONTENT_TILES = ("sports", "recipe", "calendar", "home", "weather", "time", "convert")
DECK_TILES = ("sports", "recipe", "calendar", "home")
TOOL_TILES = ("weather", "time", "convert")
_CONTENT_PHRASES = {
    "sports update": "sports",
    "sports updates": "sports",
    "sports score": "sports",
    "sports scores": "sports",
    "the score": "sports",
    "the scores": "sports",
    "score update": "sports",
    "show the score": "sports",
    "show the scores": "sports",
    "show sports": "sports",
    "sports": "sports",
    "what's the score": "sports",
    "whats the score": "sports",
    "what is the score": "sports",
    "game update": "sports",
    "the game": "sports",
    "show the recipe": "recipe",
    "the recipe": "recipe",
    "open the recipe": "recipe",
    "recipe update": "recipe",
    "recipe": "recipe",
    "calendar": "calendar",
    "show the calendar": "calendar",
    "my calendar": "calendar",
    "show my calendar": "calendar",
    "calendar update": "calendar",
    "what's on today": "calendar",
    "whats on today": "calendar",
    "what is on today": "calendar",
    "what's on the calendar": "calendar",
    "whats on the calendar": "calendar",
    "home update": "home",
    "home updates": "home",
    "home readings": "home",
    "show home": "home",
    "the home card": "home",
    "home card": "home",
    "weather": "weather",
    "the weather": "weather",
    "weather update": "weather",
    "what's the weather": "weather",
    "whats the weather": "weather",
    "what is the weather": "weather",
    "how's the weather": "weather",
    "hows the weather": "weather",
    "what's the weather like": "weather",
    "whats the weather like": "weather",
    "weather today": "weather",
    "weather tomorrow": "weather",
    "what's the weather today": "weather",
    "what's the weather tomorrow": "weather",
    "whats the weather today": "weather",
    "whats the weather tomorrow": "weather",
    "what's the weather like today": "weather",
    "what's the weather like tomorrow": "weather",
    "what time is it": "time",
    "what's the time": "time",
    "whats the time": "time",
    "what is the time": "time",
    "the time": "time",
    "current time": "time",
    "time update": "time",
}
_CONVERT_RE = re.compile(r"^convert\s+(-?\d{1,15}(?:\.\d{1,15})?)\s+(.+?)\s+to\s+(.+)$")


def content_tile_intent(text):
    """The content tile a whole utterance asks to see, or None.

    sports, recipe, calendar, home, weather, time, or convert. A question that only
    contains one of these words does not match. Folding matches a recipe command:
    case, a leading hey-name, and a trailing please.
    """
    folded = fold_utterance(text)
    if not folded:
        return None
    kind = _CONTENT_PHRASES.get(folded)
    if kind:
        return kind
    if _CONVERT_RE.match(folded):
        return "convert"
    return None


def content_tile_args(text):
    """Arguments for a weather, time, or convert ask. None for a deck tile or a non-ask."""
    kind = content_tile_intent(text)
    folded = fold_utterance(text)
    if kind == "weather":
        if "tomorrow" in folded:
            day = "tomorrow"
        elif re.search(r"\btoday\b", folded):
            day = "today"
        else:
            day = "now"
        return {"day": day}
    if kind == "time":
        return {}
    if kind == "convert":
        match = _CONVERT_RE.match(folded)
        if not match:
            return None
        raw = match.group(1)
        value = float(raw) if "." in raw else int(raw)
        return {"value": value, "from_unit": match.group(2), "to_unit": match.group(3)}
    return None


def recipe_utterance(text):
    """A recipe-card command, or None.

    Exact after case, apostrophes, and surrounding punctuation are folded.
    An optional leading "hey <name>" and a trailing "please" are allowed.
    Anything else, including a question that contains "next", is None so it
    still goes to the bot.
    """
    t = fold_utterance(text)
    if not t:
        return None
    return _RECIPE_PHRASES.get(t)


_TIME_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_TIME_RE = re.compile(
    r"\b(\d{1,4}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety)"
    r"(?:[ \t-]+)(minutes|minute|mins|min|hours|hour|hrs|hr)\b",
    re.IGNORECASE)
_RANGE_LOW = re.compile(r"\b(\d{1,4})\s*(?:-|\u2013|\u2014|to)\s*$", re.IGNORECASE)
_OFFER_YES = frozenset({"yes", "yeah", "yep", "sure", "okay", "ok", "please", "do it", "set it"})
_OFFER_NO = frozenset({"no", "nope", "no thanks", "not now"})
OFFER_NO_SAY = "Okay."


def timer_source(raw):
    """{recipe_id, step} or None. The id is a card id; step is 1..12."""
    if not isinstance(raw, dict):
        return None
    rid, step = raw.get("recipe_id"), raw.get("step")
    if not isinstance(rid, str) or not CARD_ID.fullmatch(rid):
        return None
    if type(step) is not int or not 1 <= step <= 12:
        return None
    return {"recipe_id": rid, "step": step}


def step_time(text):
    """First time in a step, or None. Hours are stored as minutes for the offer sentence.

    "10 minutes", "20 min", "1 hour", and "twenty minutes" match. A later time in the
    same step is ignored.
    """
    text = str(text or "")
    match = _TIME_RE.search(text)
    if not match:
        return None
    raw = match.group(1).casefold()
    low = _RANGE_LOW.search(text[:match.start()])
    if low and raw.isdigit():  # "10-12 minutes" / "10 to 12 minutes": offer the lower bound (R2-8)
        raw = low.group(1)
    n = int(raw) if raw.isdigit() else _TIME_WORDS.get(raw)
    if not n or n < 1:
        return None
    unit = match.group(2).casefold()
    if unit.startswith("h"):
        seconds, minutes = n * 3600, n * 60
    else:
        seconds, minutes = n * 60, n
    if seconds > 86400:
        return None
    return {"start": match.start(), "end": match.end(), "seconds": seconds, "minutes": minutes}


def _span_words(minutes, adjective=False):
    """'1 hour' / '2 hours' for whole hours, else minutes. adjective: '1-hour', '10-minute' (R2-8)."""
    n = int(minutes)
    if n >= 60 and n % 60 == 0:
        h = n // 60
        return f"{h}-hour" if adjective else f"{h} hour{'s' if h != 1 else ''}"
    return f"{n}-minute" if adjective else f"{n} minute{'s' if n != 1 else ''}"


def build_timer_offer(text, dish, step):
    """Spoken offer for a step that mentions a time, or None."""
    hit = step_time(text)
    if not hit or type(step) is not int:
        return None
    n = hit["minutes"]
    if isinstance(dish, str) and dish.strip():
        say = f"Want a {_span_words(n, True)} timer for the {dish.strip().casefold()}?"
        label = dish.strip()
    else:
        say = f"Want a {_span_words(n, True)} timer for this step?"
        label = f"Step {step}"
    return {"say": say, "minutes": n, "seconds": hit["seconds"], "label": label}


def timer_set_say(name, minutes):
    """'Orzo timer set for 10 minutes.'"""
    return f"{name} timer set for {_span_words(minutes)}."


def offer_answer(text):
    """'yes', 'no', or None. The whole utterance, folded the same way as a recipe command."""
    folded = fold_utterance(text)
    if folded in _OFFER_YES:
        return "yes"
    if folded in _OFFER_NO:
        return "no"
    return None


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
        out = {"ok": True, "closed": False, "bot": bot, "id": cid, "step": step,
               "say": say, "title": card.get("title") or ""}
        if card.get("dish"):
            out["dish"] = card["dish"]
        return out


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
        recipe = next((it for it in self._ordered() if it["card"]["type"] == "recipe"), None)
        if recipe is not None:
            return self._view(recipe)  # a recipe stays on screen; other tiles are its chips
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
                # A recipe replaces every conversation card. A sports or calendar card replaces the other
                # sports or calendar card and keeps a recipe.
                gone = CONVO_TYPES if card["type"] == "recipe" else ZONE_TYPES
                self._items = [it for it in self._items
                               if it["card"]["type"] not in gone
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

    def expiry(self, bot, card_id):
        """When this card's ttl ends (epoch seconds), or None."""
        with self._lock:
            for it in self._items:
                if it["bot"] == bot and it["id"] == card_id:
                    return it["expires"]
        return None

    def current(self, now):
        with self._lock:
            return self._current(now)

    def find_type(self, kind, now):
        """The first live card of this type, in deck order, or None."""
        if kind not in ("recipe", "sports", "calendar", "home"):
            return None
        with self._lock:
            self._drop(now)
            hits = [it for it in self._ordered() if it["card"].get("type") == kind]
            if not hits:
                return None
            return self._view(hits[0])

    def home_row(self, now, n=3, stable=False):
        """Up to n home cards. If the card in the rotation slot is a home card, it is first.

        stable=True keeps priority order. The updates band uses that so its own step is the only rotation.
        """
        with self._lock:
            cur = self._current(now)
            homes = [it for it in self._ordered() if it["card"]["type"] == "home"]
            if not stable and cur and cur["card"]["type"] == "home":
                key = (cur["bot"], cur["id"])
                keys = [(it["bot"], it["id"]) for it in homes]
                if key in keys:
                    i = keys.index(key)
                    homes = homes[i:] + homes[:i]
            return [self._view(it) for it in homes[:n]]

    def suspend_idle(self, bot, card_id):
        """Drop the idle deadline so only ttl can remove this card. False when it is not here."""
        with self._lock:
            for it in self._items:
                if it["bot"] == bot and it["id"] == card_id:
                    it["idle_at"] = None
                    return True
            return False

    def rearm_idle(self, bot, card_id, now, span):
        """Start a fresh idle window from now. ttl still caps it. None when the card is gone."""
        if type(now) not in (int, float) or type(now) is bool:
            return None
        if type(span) not in (int, float) or type(span) is bool or span < 0:
            return None
        with self._lock:
            for it in self._items:
                if it["bot"] != bot or it["id"] != card_id:
                    continue
                if it["card"]["type"] not in CONVO_TYPES:
                    return None
                it["idle_at"] = min(it["expires"], float(now) + float(span))
                return it["idle_at"]
            return None

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


# ---------------------------------------------------------------- bot updates feed (0.13.10, Noor SPEC rev4)
# A bot sends an update inside the existing signed /voice/show envelope (op "update" or "update_clear").
# The relay checks the bot's Updates switch and forwards it to the station's localhost /show.
# HARD RULE (owner): the feed is visual only. Nothing here speaks, plays a sound or wakes a bot.
import datetime as _dt  # noqa: E402
import unicodedata  # noqa: E402

UPDATE_TYPES = ("update", "needs_you", "heads_up", "working_on", "done")
LINE_MAX = 80
UPD_RAW_MAX = 320  # raw NFKC line length accepted before clip_line
RULE_MAX = 60
UPD_ROTATE_S = ROTATE_DEFAULT        # 20 s; the live interval is upd_rotate_s()
DONE_FADE_S = 30 * 60                  # Done fades (aged look) at 30 min
DROP_S = 2 * 3600                      # Done gone, Working on / Heads-up without expires_at dropped at 2 h
FEED_MAX = 48                          # whole feed; oldest non-Needs-you dropped first
UPD_PER_BOT = 8
UPD_SLOTS = 2
UPDATE_KEYS = frozenset(("v", "op", "id", "update", "kind", "nonce", "exp_ms"))
UPD_CLEAR_KEYS = frozenset(("v", "op", "id", "kind", "nonce", "exp_ms"))
UPD_BODY_KEYS = frozenset(("type", "line", "time", "expires_at", "feed_rule", "kind", "detail"))
UPD_BODY_REQ = frozenset(("type", "line", "time"))
FEED_FILE_MAX = 65536
# JSON numbers above 2**53 are not exact in IEEE doubles. A larger seq is refused.
SEQ_MAX = 2 ** 53
_ROW_KEYS = ("bot", "id", "type", "line", "time", "seq", "kind", "detail", "expires_at")
UPDATE_OPS = ("update", "update_clear")


def _upd_iso(value):
    """Unix seconds from an ISO 8601 string with a zone. Raises CardError."""
    if not isinstance(value, str) or len(value) > 40:
        raise CardError("schema")
    try:
        t = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CardError("schema")
    if t.tzinfo is None:
        raise CardError("schema")
    return t.timestamp()


def clip_line(text):
    """1-80 characters. Longer is cut at the last word boundary <= 79 and given an ellipsis."""
    text = escape_text(text, 4 * LINE_MAX) if isinstance(text, str) else ""
    text = " ".join(text.split())
    if not text:
        raise CardError("schema")
    if len(text) <= LINE_MAX:
        return text
    cut = text[:LINE_MAX - 1]
    if " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip() + "\u2026"


def validate_update(doc):
    """An update or update_clear show document (kind "show", same replay fields). Raises CardError."""
    if not isinstance(doc, dict) or type(doc.get("v")) is not int or doc.get("v") != 1:
        raise CardError("schema")
    op = doc.get("op")
    if op not in UPDATE_OPS or set(doc) != (UPDATE_KEYS if op == "update" else UPD_CLEAR_KEYS):
        raise CardError("schema")
    if not isinstance(doc.get("id"), str) or not CARD_ID.fullmatch(doc["id"]):
        raise CardError("schema")
    replay = _replay(doc)
    if op == "update_clear":
        return {"v": 1, "op": op, "id": doc["id"], **replay}
    body = doc.get("update")
    if not isinstance(body, dict) or not UPD_BODY_REQ <= set(body) <= UPD_BODY_KEYS:
        raise CardError("schema")
    if body.get("type") not in UPDATE_TYPES:
        raise CardError("schema")
    if not isinstance(body.get("line"), str) or len(unicodedata.normalize("NFKC", body["line"])) > UPD_RAW_MAX:
        raise CardError("schema")  # Nix L2: an over-long raw line is refused, not silently clipped
    out = {"type": body["type"], "line": clip_line(body.get("line")), "time": _upd_iso(body.get("time"))}
    if body["type"] == "update" or "kind" in body or "detail" in body:
        for field, cap in (("kind", 18), ("line", 60), ("detail", 90)):
            value = body.get(field, "")
            if not isinstance(value, str) or len(value) > cap or (field in ("kind", "line") and not value):
                raise CardError("schema")
            out[field] = escape_text(value, cap) if value else ""
    if "expires_at" in body and body["type"] != "needs_you":   # ignored for Needs you (SPEC)
        out["expires_at"] = _upd_iso(body.get("expires_at"))
    if "feed_rule" in body:
        rule = escape_text(body.get("feed_rule"), RULE_MAX) if isinstance(body.get("feed_rule"), str) else None
        if rule is None:
            raise CardError("schema")
        out["feed_rule"] = " ".join(rule.split())
    return {"v": 1, "op": op, "id": doc["id"], "update": out, **replay}


def is_update_op(doc):
    return isinstance(doc, dict) and doc.get("op") in UPDATE_OPS


def _reject_non_finite(_token):
    """json.loads parse_constant. NaN and Infinity are not stored."""
    raise ValueError("non-finite")


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _seq_ok(value):
    return (not isinstance(value, bool) and isinstance(value, int)
            and 0 <= value <= SEQ_MAX)


def _feed_row_ok(row):
    """A stored feed row. A bad row makes the whole file ignored."""
    if not isinstance(row, dict):
        return False
    bot, uid = row.get("bot"), row.get("id")
    if not isinstance(bot, str) or not bot_slug_ok(bot):
        return False
    if not isinstance(uid, str) or not CARD_ID.fullmatch(uid):
        return False
    if row.get("type") not in UPDATE_TYPES:
        return False
    line = row.get("line")
    if not isinstance(line, str) or not line or len(line) > LINE_MAX:
        return False
    if not _finite_number(row.get("time")):
        return False
    if not _seq_ok(row.get("seq")):
        return False
    if "expires_at" in row and not _finite_number(row.get("expires_at")):
        return False
    for key in ("kind", "detail"):
        if key in row and (not isinstance(row[key], str) or len(row[key]) > 90):
            return False
    return True


class UpdateFeed:
    """The station's update feed.

    With a path, items, rules and seq are stored as one small JSON file (atomic
    replace, mode 0600) and loaded on start. Expiry runs on load. A missing
    path stays in memory, which is what the older tests use. paused_until is
    not stored: a voice pause does not outlive the process.
    """

    def __init__(self, path=None, now=None):
        self.path = path
        self.items = {}     # (bot, id) -> {"bot","id","type","line","time","expires_at"?,"seq"}
        self.rules = {}     # bot -> feed rule text shown on the Bots page ("" = All updates)
        self.seq = 0
        self.paused_until = None   # voice "pause updates": until resume or 10 min idle
        # put() runs on HTTP threads. ordered() expires and saves on the render thread.
        self._lock = threading.RLock()
        if path:
            self._load(time.time() if now is None else now)

    def _load(self, now):
        try:
            st = os.lstat(self.path)
        except OSError:
            return
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0 or st.st_size > FEED_FILE_MAX:
            return
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return
        try:
            raw = os.read(fd, FEED_FILE_MAX + 1)
        finally:
            os.close(fd)
        if len(raw) > FEED_FILE_MAX:
            return
        try:
            doc = json.loads(raw.decode("utf-8"), parse_constant=_reject_non_finite)
        except (ValueError, UnicodeError):
            return
        if not isinstance(doc, dict):
            return
        items, rules, seq = doc.get("items"), doc.get("rules"), doc.get("seq")
        if (not isinstance(items, list) or len(items) > FEED_MAX or not isinstance(rules, dict)
                or not _seq_ok(seq)):
            return
        loaded = {}
        for row in items:
            if not _feed_row_ok(row):
                return
            loaded[(row["bot"], row["id"])] = {k: row[k] for k in _ROW_KEYS if k in row}
        clean = {}
        for key, val in rules.items():
            if not isinstance(key, str) or not bot_slug_ok(key) or not isinstance(val, str) or len(val) > RULE_MAX:
                return
            clean[key] = val
        self.items = loaded
        self.rules = clean
        self.seq = max([seq] + [r["seq"] for r in loaded.values()])
        self.expire(now)

    def _save(self):
        """Write the feed. Caller may already hold _lock. The snapshot and the replace stay inside it,
        so a later put cannot be overwritten by an older snapshot."""
        if not self.path:
            return
        with self._lock:
            doc = {"items": [{k: row[k] for k in _ROW_KEYS if k in row} for row in self.items.values()],
                   "rules": dict(self.rules), "seq": self.seq}
            try:
                data = json.dumps(doc, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError):
                return
            if len(data) > FEED_FILE_MAX:
                return
            directory = os.path.dirname(os.path.abspath(self.path)) or "."
            try:
                fd, tmp = tempfile.mkstemp(prefix=".update-feed-", dir=directory)
            except OSError:
                return
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, data)
                os.fsync(fd)
            except OSError:
                os.close(fd)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return
            os.close(fd)
            try:
                os.replace(tmp, self.path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return
            try:
                dirfd = os.open(directory, os.O_RDONLY)
            except OSError:
                return
            try:
                os.fsync(dirfd)
            except OSError:
                pass
            finally:
                os.close(dirfd)

    def put(self, bot, uid, update):
        if not bot_slug_ok(bot):
            raise CardError("schema")
        with self._lock:
            return self._put(bot, uid, update)

    def _put(self, bot, uid, update):
        if "feed_rule" in update:
            self.rules[bot] = update["feed_rule"]
        if update["type"] == "working_on":     # one Working on per bot: the next update replaces it
            for key in [k for k, v in self.items.items() if k[0] == bot and v["type"] == "working_on"]:
                del self.items[key]
        if update["type"] in ("update", "needs_you"):
            for key in [k for k, v in self.items.items() if k[0] == bot
                        and v["type"] == update["type"]
                        and (update["type"] == "needs_you" or v.get("kind") == update.get("kind"))]:
                del self.items[key]
        self.seq += 1
        row = {"bot": bot, "id": uid, "seq": self.seq,
               **{k: v for k, v in update.items() if k != "feed_rule"}}
        self.items[(bot, uid)] = row
        self._cap(bot)
        self._save()
        return row

    def clear(self, bot, uid):
        with self._lock:
            removed = self.items.pop((bot, uid), None) is not None
            if removed:
                self._save()
            return removed

    def drop_bot(self, bot):
        """Updates switched off (or bot removed): its rows leave the station now."""
        with self._lock:
            keys = [k for k in self.items if k[0] == bot]
            for key in keys:
                del self.items[key]
            if keys:
                self._save()

    def _cap(self, bot):
        mine = sorted((v for v in self.items.values() if v["bot"] == bot), key=lambda r: r["seq"])
        while len(mine) > UPD_PER_BOT:
            old = next((r for r in mine if r["type"] != "needs_you"), mine[0])
            mine.remove(old)
            self.items.pop((old["bot"], old["id"]), None)
        while len(self.items) > FEED_MAX:
            rows = sorted(self.items.values(), key=lambda r: r["seq"])
            old = next((r for r in rows if r["type"] != "needs_you"), rows[0])
            self.items.pop((old["bot"], old["id"]), None)

    def expire(self, now):
        """Ageing keeps running at night and while paused.

        A type "update" row is the bot's latest of that kind. It stays until
        that bot replaces it, clears it, or is removed. heads_up, working_on
        and done still leave after their expires_at or after DROP_S (2 hours).
        needs_you stays until it is cleared.
        """
        with self._lock:
            self._expire(now)

    def _expire(self, now):
        removed = False
        for key, r in list(self.items.items()):
            t = r["type"]
            if t in ("needs_you", "update"):
                continue
            if t == "heads_up" and "expires_at" in r:
                gone = now >= r["expires_at"]
            else:
                gone = now - r["time"] >= DROP_S
            if gone:
                del self.items[key]
                removed = True
        if removed:
            self._save()

    def ordered(self, now):
        """Needs you newest first, then the rest newest first. Done past 30 min is marked aged."""
        with self._lock:
            self._expire(now)
            rows = [dict(r) for r in self.items.values()]
        for r in rows:
            r["aged"] = r["type"] == "done" and now - r["time"] >= DONE_FADE_S
        newest = lambda r: (-r["time"], -r["seq"])
        needs = sorted((r for r in rows if r["type"] == "needs_you"), key=newest)
        rest = sorted((r for r in rows if r["type"] != "needs_you"), key=newest)
        return needs, rest


def _band_window(seq, n, step, advance):
    """Up to n items, starting at step * advance. Never repeats an item in one window."""
    if not seq or n <= 0:
        return []
    n = min(int(n), len(seq))
    start = (max(0, int(step)) * int(advance)) % len(seq)
    out = []
    for i in range(len(seq)):
        item = seq[(start + i) % len(seq)]
        if item in out:
            break
        out.append(item)
        if len(out) == n:
            break
    return out


def _band_positions(seq, n, step, advance):
    """1-based indexes of the items _band_window would show, in that order."""
    if not seq or n <= 0:
        return []
    n = min(int(n), len(seq))
    start = (max(0, int(step)) * int(advance)) % len(seq)
    positions, seen = [], set()
    for i in range(len(seq)):
        index = (start + i) % len(seq)
        if index in seen:
            break
        seen.add(index)
        positions.append(index + 1)
        if len(positions) == n:
            break
    return positions


def update_slots(needs, others, step, slots=UPD_SLOTS):
    """Two slots (rev6). Needs you stays in slot 1 and rotates there when several are waiting.

    No Needs you: both slots advance two ordinary items per step. One Needs you: it stays in
    slot 1 and slot 2 advances one. Two or more, with other items: each slot advances one.
    Only Needs you: both slots take the priority deck, two per step. Nothing is duplicated,
    and a short deck does not repeat a tile to fill a slot.
    """
    step = max(0, int(step))
    needs, others = list(needs), list(others)
    if not needs:
        return _band_window(others, slots, step, 2)
    if len(needs) == 1:
        second = _band_window(others, 1, step, 1)
        return [needs[0]] + second
    if not others:
        return _band_window(needs, slots, step, 2)
    return _band_window(needs, 1, step, 1) + _band_window(others, 1, step, 1)


def _marker_range(positions, total):
    if len(positions) == 1:
        return f"{positions[0]} of {total}"
    return f"{positions[0]}\u2013{positions[-1]} of {total}"


def update_marker(needs, others, step, paused=False, slots=UPD_SLOTS):
    """Where the two slots are in each queue (rev6).

    One Needs you: "Needs you · others 3 of 6". Several: "Needs you 1 of 3 · others 2 of 4".
    No Needs you: "3–4 of 6". Only Needs you: "Needs you 1–2 of 3". One ordinary item, or an
    empty secondary queue, has no counter. The display does not show this counter, and it
    never says Paused.
    """
    step = max(0, int(step))
    needs, others = list(needs), list(others)
    text = ""
    if len(needs) == 1:
        text = "Needs you"
        if others:
            pos = _band_positions(others, 1, step, 1)
            text += " \u00b7 others " + _marker_range(pos, len(others))
    elif len(needs) > 1 and not others:
        pos = _band_positions(needs, slots, step, 2)
        if len(needs) > 1 and pos:
            text = "Needs you " + _marker_range(pos, len(needs))
    elif len(needs) > 1:
        npos = _band_positions(needs, 1, step, 1)
        text = "Needs you " + _marker_range(npos, len(needs))
        opos = _band_positions(others, 1, step, 1)
        text += " \u00b7 others " + _marker_range(opos, len(others))
    elif len(others) > slots:
        pos = _band_positions(others, slots, step, 2)
        text = _marker_range(pos, len(others))
    del paused
    return text


def update_band_state(feed, now, step, *, night=False, bot_talking=False, timer_ringing=False, paused=False):
    """What the updates band does right now.

    night: hidden entirely, Needs you included; only the dimmed night clock shows.
    bot talking / timer ringing / voice pause: visible but paused (rotation holds its step).
    """
    needs, rest = feed.ordered(now)
    if night:
        return {"visible": False, "paused": False, "tiles": [], "marker": ""}
    hold = bool(bot_talking or timer_ringing or paused)
    tiles = update_slots(needs, rest, step)
    return {"visible": bool(tiles), "paused": hold, "tiles": tiles,
            "marker": update_marker(needs, rest, step, paused=hold)}


def one_update(needs, others, step):
    """The single card for this step. Needs you come first, then the other updates and home cards.

    One item at a time. The step wraps. An empty pair of queues is None.
    """
    rows = list(needs) + list(others)
    if not rows:
        return None
    step = max(0, int(step))
    return rows[step % len(rows)]


def update_next_step(step, elapsed_s, paused, interval=None):
    """Advance one step per interval of unpaused time. The interval is the shared band clock."""
    wait = upd_rotate_s() if interval is None else interval
    if paused or elapsed_s < wait:
        return step
    return step + 1


def update_rule_line(on, rule, online=True):
    """The line under a bot's Updates switch on the Bots page (SPEC)."""
    if not online:
        return "Rule can\u2019t change while offline"
    if not on:
        return "Off: nothing shows on the station"
    rule = " ".join(str(rule or "").split())
    return rule or "All updates"


_UPD_PAUSE = re.compile(r"^(?:hey grok )?(pause|stop|resume) updates$")


def updates_pause_command(heard):
    """'pause' / 'resume' for "Hey Grok, pause|stop|resume updates", else None. A reply to the wake word only."""
    t = " ".join(str(heard or "").lower().replace(",", " ").replace(".", " ").split())
    m = _UPD_PAUSE.match(t)
    if not m:
        return None
    return "resume" if m.group(1) == "resume" else "pause"
