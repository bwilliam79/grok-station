"""Signed, sealed bot replies. Pure checks plus the station box key and the pinned verify keys.

The relay calls these. Nothing here speaks, logs a body, or stores a bot's private key.
A reply is SealedBox(station public key) around an Ed25519 attached signature of the reply JSON.
"""
import base64, json, os, re, tempfile, threading, time, unicodedata
import handoff
from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, SealedBox
from nacl.signing import VerifyKey

OUTER_MAX = 8192
AVATAR_OUTER_MAX = 4 << 20  # sealed picture; a 2 MB image is about 3.7 MB once sealed
AVATAR_IMAGE_MAX = 2 << 20
_AVATAR_TYPES = frozenset(("image/png", "image/jpeg", "image/jpg", "image/webp"))
_AVATAR_KEYS = frozenset(("kind", "nonce", "exp_ms", "content_type", "image_b64"))
REPLY_BY_S = 60
SKEW_S = 120  # accept a sealed reply this long after issue (clock skew / replay). The station stops waiting sooner.
_OUTER_KEYS = frozenset(("v", "bot", "sealed"))
_REPLY_KEYS = frozenset(("request_id", "text", "tier", "action_class", "kind"))
_ANSWER_KEYS = frozenset(("request_id", "text", "tier", "action_class"))  # 0.6.0 answers omit kind
_LIST_KEYS = frozenset(("request_id", "kind", "tier", "action_class", "bots"))
LIST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .'-]{0,31}$")
LIST_MAX = 24
_NONCE_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
SHOW_SEEN_MAX = 1024

class Reject(Exception):
    """A reply the relay must not accept. status is the HTTP code. verdict is safe to log."""

    def __init__(self, status, verdict, bot="-", rid="-"):
        self.status = int(status)
        self.verdict = verdict if isinstance(verdict, str) else "error"
        self.bot = bot if handoff.bot_slug_ok(bot) else "-"
        self.rid = rid if isinstance(rid, str) and handoff.REQUEST_ID.fullmatch(rid) else "-"
        super().__init__(self.verdict)


class StationKeyError(Exception):
    """The station box key file is the wrong size. It is not overwritten."""


def parse_pub(text):
    """Standard base64 of 32 bytes, or None. Whitespace around a paste is ignored; nothing else is."""
    if not isinstance(text, str):
        return None
    s = "".join(text.split())
    if not s or len(s) > 80:
        return None
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception:
        return None
    if len(raw) != 32:
        return None
    return raw


def _atomic_0600(path, data):
    """Replace path with data. Mode stays 0600 even when GROK_SHARED_STATE would widen other state files."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def keys_path(bots_dir):
    return os.path.join(bots_dir, "bot_keys.json")


def load_keys(path):
    """{"killed": bool, "keys": {slug: b64}}. A missing or corrupt file is no keys (replies fail closed)."""
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {"killed": False, "keys": {}}
    if not isinstance(doc, dict):
        return {"killed": False, "keys": {}}
    keys = {}
    raw = doc.get("keys")
    if isinstance(raw, dict):
        for slug, b64 in raw.items():
            if not handoff.bot_slug_ok(slug):
                continue
            vk = parse_pub(b64)
            if vk is None:
                continue
            keys[slug] = base64.b64encode(vk).decode()
    return {"killed": doc.get("killed") is True, "keys": keys}


def _save_keys(path, doc):
    _atomic_0600(path, (json.dumps({"killed": bool(doc.get("killed")), "keys": doc.get("keys") or {}},
                                   sort_keys=True, indent=2) + "\n").encode())


def pin_key(path, slug, b64):
    """Store one verify key and clear the kill switch. False if the slug or the key is not usable."""
    raw = parse_pub(b64)
    if raw is None or not handoff.bot_slug_ok(slug):
        return False
    doc = load_keys(path)
    doc["keys"][slug] = base64.b64encode(raw).decode()
    doc["killed"] = False
    _save_keys(path, doc)
    return True


def revoke_one(path, slug):
    """Drop one pinned key. Does not set the kill switch."""
    doc = load_keys(path)
    doc["keys"].pop(slug, None)
    _save_keys(path, doc)
    return True


def revoke_all(path):
    """Drop every pinned key and reject every reply until a key is paired again."""
    _save_keys(path, {"killed": True, "keys": {}})
    return True


def pinned_bytes(doc, slug):
    if not isinstance(doc, dict):
        return None
    keys = doc.get("keys")
    if not isinstance(keys, dict):
        return None
    return parse_pub(keys.get(slug))


def station_key_path(secrets_dir):
    return os.path.join(secrets_dir, "station_box_key")


def load_or_create_station(secrets_dir):
    """X25519 private key, created on first use, mode 0600. A short or long file is left as it is."""
    path = station_key_path(secrets_dir)
    os.makedirs(secrets_dir, exist_ok=True)
    if os.path.isfile(path):
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) != 32:
            raise StationKeyError()
        return PrivateKey(raw)
    sk = PrivateKey.generate()
    raw = bytes(sk)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) != 32:
            raise StationKeyError()
        return PrivateKey(raw)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return PrivateKey(raw)


def station_pub_b64(sk):
    return base64.b64encode(bytes(sk.public_key)).decode()


def env_rate(name, default):
    """A positive int from the environment, else default. Junk and zero keep the default."""
    try:
        v = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


class Limiter:
    """In-memory token buckets: one per bot and one global. Rates are tokens per minute, starting full."""

    def __init__(self, per_bot=6, global_rate=30):
        self.per = float(per_bot)
        self.glob = float(global_rate)
        self._bots = {}
        self._global = (self.glob, None)

    def _refill(self, tokens, ts, cap, now):
        if ts is None:
            return cap, now
        if now < ts:
            now = ts
        return min(cap, tokens + (now - ts) * (cap / 60.0)), now

    def allow(self, bot, now=None):
        """Take one token. False when this bot or the station is over its rate."""
        now = time.monotonic() if now is None else float(now)
        if self.per < 1 or self.glob < 1:
            return False
        bt, bts = self._bots.get(bot, (self.per, None))
        bt, _bts = self._refill(bt, bts, self.per, now)
        gt, gts = self._global
        gt, _gts = self._refill(gt, gts, self.glob, now)
        if bt < 1.0 or gt < 1.0:
            self._bots[bot] = (bt, now)
            self._global = (gt, now)
            return False
        self._bots[bot] = (bt - 1.0, now)
        self._global = (gt - 1.0, now)
        return True


def _rows(raw):
    out = []
    if not isinstance(raw, list):
        return out
    for row in raw[-10:]:
        if not isinstance(row, dict):
            continue
        if row.get("timeout") is True and "rtt_ms" not in row:
            out.append({"timeout": True})
        elif type(row.get("rtt_ms")) is int and row["rtt_ms"] >= 0 and "timeout" not in row:
            out.append({"rtt_ms": row["rtt_ms"]})
    return out


def _load_rtt(path):
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    bots = {}
    raw_bots = doc.get("bots") if isinstance(doc.get("bots"), dict) else {}
    for slug, rows in raw_bots.items():
        if handoff.bot_slug_ok(slug):
            bots[slug] = _rows(rows)
    return {"overall": _rows(doc.get("overall")), "bots": bots}


def summarize(rows):
    """Average round trip of the answered rows, and how many of the rows are timeouts."""
    answered = [r["rtt_ms"] for r in rows if "rtt_ms" in r]
    avg = int(round(sum(answered) / len(answered))) if answered else None
    timeouts = sum(1 for r in rows if r.get("timeout") is True)
    return {"avg_rtt_ms": avg, "timeouts": timeouts}


class Stats:
    """Last 10 asks per bot and overall. Each row is {rtt_ms} or {timeout: true}. No question, no reply."""

    def __init__(self, path):
        self.path = path
        self.data = _load_rtt(path)

    def record(self, bot, rtt_ms=None, timeout=False):
        if not handoff.bot_slug_ok(bot):
            return
        if timeout:
            row = {"timeout": True}
        elif type(rtt_ms) is int and rtt_ms >= 0:
            row = {"rtt_ms": rtt_ms}
        else:
            return
        self.data["overall"] = (self.data["overall"] + [row])[-10:]
        self.data["bots"][bot] = (self.data["bots"].get(bot, []) + [row])[-10:]
        _atomic_0600(self.path, (json.dumps(self.data, sort_keys=True) + "\n").encode())

    def bot_view(self, bot):
        return summarize(self.data["bots"].get(bot, []))

    def overall_view(self):
        return summarize(self.data["overall"])


def _shape(bot="-"):
    raise Reject(400, "shape", bot)


def list_name_clean(raw):
    """Display name for one list entry, or None.

    Control and format characters in the raw string reject (they are not stripped into a different
    name). handoff.clean does not truncate-accept: longer than 32 after cleaning is a reject.
    """
    if not isinstance(raw, str) or not raw:
        return None
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in raw):
        return None
    # Reject before NFKC. Fullwidth letters would otherwise fold into ASCII and pass LIST_NAME.
    if any(ord(c) > 127 for c in raw):
        return None
    cleaned = handoff.clean(raw, 33)
    if not cleaned or len(cleaned) > 32 or LIST_NAME.fullmatch(cleaned) is None:
        return None
    return cleaned


def _pending_open(pending_for, rid, bot, now_ms):
    """(pending, None) or (None, Reject) for a request that is not this bot's open ask."""
    try:
        pending = pending_for(rid) if pending_for is not None else None
    except Exception:
        pending = None
    if not isinstance(pending, dict) or pending.get("request_id") != rid or pending.get("persona") != bot:
        return None, Reject(403, "request", bot, rid)
    exp = pending.get("expires_ms")
    now = time.time() * 1000 if now_ms is None else now_ms
    if type(exp) is not int:
        return None, Reject(403, "request", bot, rid)
    if exp < now:
        return None, Reject(403, "expired", bot, rid)
    return pending, None


def _accept_answer(doc, bot, pending_for, seen, hosts, names, now_ms):
    """Answer schema. Order: schema, duplicate, pending, lint.

    kind "reply" is the signed binding. An answer that omits kind is still an answer
    (the 0.6.0 shape), not a list and not a card. Any other kind is rejected by ingest
    before this runs.
    """
    if not isinstance(doc, dict):
        raise Reject(400, "schema", bot)
    keys = set(doc)
    if keys == _REPLY_KEYS:
        if doc.get("kind") != "reply":
            raise Reject(400, "schema", bot)
    elif keys != _ANSWER_KEYS:
        raise Reject(400, "schema", bot)
    rid = doc.get("request_id")
    if not isinstance(rid, str) or not handoff.REQUEST_ID.fullmatch(rid):
        raise Reject(400, "schema", bot)
    if type(doc.get("tier")) is not int or doc.get("tier") != 0 or doc.get("action_class") != "answer":
        raise Reject(400, "schema", bot, rid)
    text = doc.get("text")
    if not isinstance(text, str):
        raise Reject(400, "schema", bot, rid)
    cleaned = handoff.clean(text, handoff.REPLY_MAX + 1)
    if not cleaned or len(cleaned) > handoff.REPLY_MAX:
        raise Reject(400, "schema", bot, rid)
    if isinstance(seen, set) and rid in seen:
        raise Reject(409, "duplicate", bot, rid)
    pending, bad = _pending_open(pending_for, rid, bot, now_ms)
    if bad is not None:
        raise bad
    if pending.get("kind") == "bot_list":
        raise Reject(403, "request", bot, rid)
    pii = pending.get("pii_mode")
    if not isinstance(pii, bool):
        pii = True
    try:
        why = handoff.lint_reply(cleaned, hosts, names, pii=pii)
    except Exception:
        why = "error"
    if why:
        raise Reject(400, "lint", bot, rid)
    nonce = pending.get("nonce")
    return {"request_id": rid, "text": cleaned, "tier": 0, "action_class": "answer", "bot": bot,
            "nonce": nonce if isinstance(nonce, str) else ""}


def _accept_list(doc, bot, pending_for, seen, hosts, names, now_ms, source):
    """List schema. Order: exact keys, names, lint, duplicate, pending kind, list source."""
    if not isinstance(doc, dict) or set(doc) != _LIST_KEYS:
        raise Reject(400, "schema", bot)
    rid = doc.get("request_id")
    if not isinstance(rid, str) or not handoff.REQUEST_ID.fullmatch(rid):
        raise Reject(400, "schema", bot)
    if (doc.get("kind") != "bot_list" or type(doc.get("tier")) is not int or doc.get("tier") != 0
            or doc.get("action_class") != "bot_list"):
        raise Reject(400, "schema", bot, rid)
    bots = doc.get("bots")
    if not isinstance(bots, list) or len(bots) > LIST_MAX:
        raise Reject(400, "schema", bot, rid)
    names_out, folded = [], set()
    for ent in bots:
        if not isinstance(ent, dict) or set(ent) != {"name"}:
            raise Reject(400, "schema", bot, rid)
        cleaned = list_name_clean(ent.get("name"))
        if cleaned is None:
            raise Reject(400, "schema", bot, rid)
        key = cleaned.casefold()
        if key in folded:
            raise Reject(400, "schema", bot, rid)
        folded.add(key)
        names_out.append(cleaned)
    for cleaned in names_out:
        try:
            why = handoff.lint_reply(cleaned, hosts, names, pii=True)
        except Exception:
            why = "error"
        if why:
            raise Reject(400, "lint", bot, rid)
    if isinstance(seen, set) and rid in seen:
        raise Reject(409, "duplicate", bot, rid)
    pending, bad = _pending_open(pending_for, rid, bot, now_ms)
    if bad is not None:
        raise bad
    if pending.get("kind") != "bot_list":
        raise Reject(403, "request", bot, rid)
    src = source() if callable(source) else source
    if not isinstance(src, str) or src != bot:
        raise Reject(403, "source", bot, rid)
    nonce = pending.get("nonce")
    return {"request_id": rid, "kind": "bot_list", "tier": 0, "action_class": "bot_list", "bot": bot,
            "nonce": nonce if isinstance(nonce, str) else "", "names": names_out}


def open_signed(raw, station_sk, keys_doc, known, limiter, rate_now=None, *, limit=None, rate=True):
    """Open one sealed POST body. Returns (bot, message bytes) or raises Reject.

    Order: size and outer shape, kill switch, unknown bot, revoked key, rate limit, unseal,
    signature (before the message is parsed). The limiter is charged only when a paired key
    would be unsealed. The caller decides what the signed bytes mean.

    limit raises the outer size cap (a picture is larger than a reply). rate=False skips the
    reply limiter when the caller has its own cap. Neither is taken positionally.
    """
    cap = OUTER_MAX if limit is None else limit
    if type(cap) is not int or cap < 1 or cap > AVATAR_OUTER_MAX:
        cap = OUTER_MAX
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > cap:
        _shape()
    try:
        outer = json.loads(raw)
    except ValueError:
        _shape()
    if not isinstance(outer, dict) or set(outer) != _OUTER_KEYS:
        bot = outer.get("bot") if isinstance(outer, dict) else "-"
        _shape(bot if handoff.bot_slug_ok(bot) else "-")
    bot = outer.get("bot")
    if type(outer.get("v")) is not int or outer.get("v") != 1 or not handoff.bot_slug_ok(bot):
        _shape(bot if handoff.bot_slug_ok(bot) else "-")
    sealed = outer.get("sealed")
    if not isinstance(sealed, str) or not sealed or any(c.isspace() for c in sealed):
        _shape(bot)
    try:
        blob = base64.b64decode(sealed, validate=True)
    except Exception:
        _shape(bot)
    if not blob:
        _shape(bot)
    if isinstance(keys_doc, dict) and keys_doc.get("killed") is True:
        raise Reject(403, "killed", bot)
    if bot not in known:
        raise Reject(403, "unknown", bot)
    vk_raw = pinned_bytes(keys_doc, bot)
    if vk_raw is None:
        raise Reject(403, "revoked", bot)
    if rate and (limiter is None or not limiter.allow(bot, now=rate_now)):
        raise Reject(429, "rate", bot)
    try:
        signed = SealedBox(station_sk).decrypt(blob)
    except (CryptoError, Exception):
        raise Reject(403, "unseal", bot)
    if not isinstance(signed, (bytes, bytearray)) or len(signed) < 64:
        raise Reject(403, "signature", bot)
    try:
        message = VerifyKey(vk_raw).verify(bytes(signed))
    except Exception:
        raise Reject(403, "signature", bot)
    return bot, bytes(message)


def nonce_ok(value):
    """URL-safe base64 (no padding) of at least 16 bytes."""
    if not isinstance(value, str) or not 22 <= len(value) <= 200:
        return False
    if any(c not in _NONCE_ALPHABET for c in value):
        return False
    try:
        raw = base64.urlsafe_b64decode(value + ("=" * ((-len(value)) % 4)))
    except Exception:
        return False
    return len(raw) >= 16


class ShowSeen:
    """Persisted (bot, nonce) pairs for sealed cards. Expired rows are dropped. The file is mode 0600.

    reserve() records the pair and returns False when it was already there. release() undoes a
    reserve when the station did not accept the card, so a failed post can be retried.
    """

    def __init__(self, path, max_n=SHOW_SEEN_MAX):
        self.path = path
        self.max_n = max_n if type(max_n) is int and max_n >= 1 else SHOW_SEEN_MAX
        self._lock = threading.Lock()

    def _load(self, now_ms):
        try:
            with open(self.path) as f:
                rows = json.load(f)
        except (OSError, ValueError):
            return []
        if not isinstance(rows, list):
            return []
        kept = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 3:
                continue
            bot, nonce, exp = row
            if not isinstance(bot, str) or not isinstance(nonce, str) or type(exp) is not int:
                continue
            if exp < now_ms:
                continue
            kept.append([bot, nonce, exp])
        return kept[-self.max_n:]

    def _write(self, rows):
        _atomic_0600(self.path, json.dumps(rows, separators=(",", ":")).encode())

    def contains(self, bot, nonce, now_ms):
        with self._lock:
            return any(r[0] == bot and r[1] == nonce for r in self._load(now_ms))

    def reserve(self, bot, nonce, exp_ms, now_ms):
        """True when this call stored the pair. False when (bot, nonce) is already current.

        None when the set is full of unexpired rows: refuse the show rather than forget a live nonce.
        """
        with self._lock:
            rows = self._load(now_ms)
            if any(r[0] == bot and r[1] == nonce for r in rows):
                return False
            if len(rows) >= self.max_n:
                return None  # _load already dropped expired rows; never evict a live one
            rows.append([bot, nonce, int(exp_ms)])
            self._write(rows)
            return True

    def release(self, bot, nonce, now_ms):
        with self._lock:
            rows = [r for r in self._load(now_ms) if not (r[0] == bot and r[1] == nonce)]
            self._write(rows)


def _seen_hit(seen, bot, nonce, now_ms):
    if seen is None:
        return False
    if isinstance(seen, set):
        return (bot, nonce) in seen
    return bool(seen.contains(bot, nonce, now_ms))


def ingest(raw, station_sk, keys_doc, known, pending_for, seen, limiter, hosts=(), names=(), now_ms=None,
           rate_now=None, source=None):
    """Check one POST body. Returns the reply to hand to the waiter, or raises Reject.

    Shared prefix is open_signed. Dispatch is on the signed kind. A list (kind bot_list) is
    schema, names, lint, duplicate, pending, then the list source. An answer is kind reply,
    or the shape with no kind: schema, duplicate, pending, lint. Any other kind, including
    show, is schema. seen and the pending file are not changed here. The limiter is charged
    only when a paired key would be unsealed. pending_for(rid) runs only after the signed
    message parses.
    """
    bot, message = open_signed(raw, station_sk, keys_doc, known, limiter, rate_now=rate_now)
    try:
        doc = json.loads(message)
    except ValueError:
        raise Reject(400, "schema", bot)
    if isinstance(doc, dict) and doc.get("kind") == "bot_list":
        return _accept_list(doc, bot, pending_for, seen, hosts, names, now_ms, source)
    if isinstance(doc, dict) and "kind" in doc and doc.get("kind") != "reply":
        rid = doc.get("request_id") if isinstance(doc.get("request_id"), str) else "-"
        raise Reject(400, "schema", bot, rid)
    return _accept_answer(doc, bot, pending_for, seen, hosts, names, now_ms)


def ingest_show(raw, station_sk, keys_doc, known, limiter, parse_show, rate_now=None, seen=None, now_ms=None):
    """Open a sealed card put/clear. parse_show runs only after the signature verifies.

    The signed document carries kind "show", a nonce, and exp_ms. exp_ms in the past, or more
    than SKEW_S seconds ahead, is rejected. (bot, nonce) already in seen is a replay. seen is
    not updated here. A bot field inside the signed document is rejected: the outer slug is
    the only bot id, and it is stamped here. A reply (kind "reply") does not pass.
    """
    bot, message = open_signed(raw, station_sk, keys_doc, known, limiter, rate_now=rate_now)
    try:
        doc = json.loads(message)
        show = parse_show(doc)
    except Reject:
        raise
    except Exception:
        raise Reject(400, "schema", bot)
    if not isinstance(show, dict) or "bot" in show or show.get("kind") != "show":
        raise Reject(400, "schema", bot)
    nonce, exp = show.get("nonce"), show.get("exp_ms")
    if not nonce_ok(nonce) or type(exp) is not int:
        raise Reject(400, "schema", bot)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if type(now) is not int or exp < now or exp > now + SKEW_S * 1000:
        raise Reject(403, "expired", bot)
    if _seen_hit(seen, bot, nonce, now):
        raise Reject(409, "duplicate", bot)
    out = dict(show)
    out["bot"] = bot
    return out


def ingest_avatar(raw, station_sk, keys_doc, known, seen=None, now_ms=None):
    """Open a sealed profile picture. Returns bot, nonce, exp_ms, content_type, and image bytes.

    The signed document is kind "avatar" plus nonce, exp_ms, content_type, and image_b64.
    A bot field inside is rejected. The reply limiter is not charged. seen is not updated here.
    """
    bot, message = open_signed(raw, station_sk, keys_doc, known, None, limit=AVATAR_OUTER_MAX, rate=False)
    try:
        doc = json.loads(message)
    except ValueError:
        raise Reject(400, "schema", bot)
    if not isinstance(doc, dict) or set(doc) != _AVATAR_KEYS or doc.get("kind") != "avatar":
        raise Reject(400, "schema", bot)
    nonce, exp = doc.get("nonce"), doc.get("exp_ms")
    if not nonce_ok(nonce) or type(exp) is not int:
        raise Reject(400, "schema", bot)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if type(now) is not int or exp < now or exp > now + SKEW_S * 1000:
        raise Reject(403, "expired", bot)
    if _seen_hit(seen, bot, nonce, now):
        raise Reject(409, "duplicate", bot)
    ctype = doc.get("content_type")
    if not isinstance(ctype, str) or ctype not in _AVATAR_TYPES:
        raise Reject(415, "type", bot)
    b64 = doc.get("image_b64")
    if not isinstance(b64, str) or not b64:
        raise Reject(415, "type", bot)
    try:
        image = base64.b64decode(b64, validate=True)
    except Exception:
        raise Reject(415, "type", bot)
    if not image:
        raise Reject(415, "type", bot)
    if len(image) > AVATAR_IMAGE_MAX:
        raise Reject(413, "size", bot)
    return {"bot": bot, "nonce": nonce, "exp_ms": exp, "content_type": ctype, "image": image}
