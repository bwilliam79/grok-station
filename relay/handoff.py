"""Teammate handoff (real-bot pilot, Nix GO 2026-10-19): the station asks a teammate's own Grok Bot routine and speaks its
answer. Off unless HANDOFF_ENABLED=1 and that bot has its own webhook. Router mode is retired: a "_router" entry is
dropped and never used. Every bot ships at T0 (answer only).

Blockers covered here: B4 (one POST, never retried after the body is sent), B6 (question stripped of control/zero-width/bidi,
capped at 500, stored on the pending file), B10 (one outstanding per bot; optional per-bot/global daily caps and min gap via
HANDOFF_PER_BOT_DAY / HANDOFF_GLOBAL_DAY / HANDOFF_GAP_S, all off by default; when on, failed POSTs count), B13 (lint before speech: always-blocked secrets, plus PII only when pii_mode is on), B14 (nonce
bound pending file, single use, expiry; the answer is spoken only while the session still waits). Logs carry request_id,
agent, length and verdict only. There is no guest mode on this path.
"""
import json, math, os, re, secrets, time, unicodedata, urllib.parse

REQUEST_ID = re.compile(r"^vs_[A-Za-z0-9_-]{22}$")
Q_MAX, REPLY_MAX = 500, 400
TIER_ACTIONS = {0: ("answer",), 1: ("answer", "note"), 2: ("answer", "note", "queue_approval")}
MAX_TIER = 0                                   # pilot: every bot answers only; a reply above T0 is dropped


def _env_num(name, cast):
    """A cap from the relay env; unset, empty, 0, negative or junk means no limit (returns 0)."""
    try:
        v = cast(os.environ.get(name, "") or 0)
    except ValueError:
        return 0
    return v if v > 0 else 0


# Off by default (owner request 2026-10-20: no daily limits). 0 = no limit. The one-outstanding-per-bot check always applies.
PER_BOT_DAY = _env_num("HANDOFF_PER_BOT_DAY", int)
GLOBAL_DAY = _env_num("HANDOFF_GLOBAL_DAY", int)
GAP_S = _env_num("HANDOFF_GAP_S", float)
BEGIN, END = "<<<STATION_QUESTION untrusted>>>", "<<<END_STATION_QUESTION>>>"

# Spoken-reply lint. Always blocked, after NFKC + lowercase + spelled-out digits + digit separators removed:
# a password that has a value, key/token prefixes, PEM, a long high-entropy token, an SSN, a Luhn card.
# PII (names, email, phone, street, IP, MAC, private hosts) only when pii_mode is on. Nothing else content-based.
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+")
_IP = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b|(?<![\w:])(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}(?![\w:])|[0-9a-f]{1,4}::|::[0-9a-f]{1,4}")
_HOST = re.compile(r"\.(lan|local|internal|home\.arpa|localdomain)\b|\blocalhost\b")
_PHONE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")
_PHONE_FLAT = re.compile(r"\b(?:\+?1)?\d{10}\b")
_ADDRESS = re.compile(r"\b\d{1,6}\s+\w+(?:\s\w+)?\s(?:st|street|ave|avenue|rd|road|blvd|ln|lane|dr|drive|ct|court|way|pl)\b")
_MAC = re.compile(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b|\b[0-9a-f]{2}(?:-[0-9a-f]{2}){5}\b|\b(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}\b")
_SECRET_VALUE = re.compile(r"\b(?:password|passcode|passwd|pin|token|api[ _-]?key|secret)\b\s*(?:is|=|:)\s*\S+")
_PREFIX = re.compile(
    r"(?<![a-z0-9])(?:sk|xai)-[a-z0-9_\-]{8,}"
    r"|(?<![a-z0-9])ghp_[a-z0-9]{8,}"
    r"|(?<![a-z0-9])github_pat_[a-z0-9_]{8,}"
    r"|(?<![a-z0-9])akia[a-z0-9]{16}"
    r"|(?<![a-z0-9])bearer\s+[a-z0-9._\-+/=]{8,}")
_PEM = re.compile(r"-----begin [a-z0-9 ]{0,48}private key-----")
_DIGIT_WORD = re.compile(r"\b(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)\b")
_DIGIT_MAP = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
              "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def _luhn(digits):
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = ord(ch) - 48
        if i % 2 == 1:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _norm_reply(text):
    """NFKC, lowercase, spelled-out digits, then a copy with spaces/dashes/dots removed between digits."""
    t = unicodedata.normalize("NFKC", str(text)).lower()
    t = _DIGIT_WORD.sub(lambda m: _DIGIT_MAP[m.group(0)], t)
    flat, prev = t, None
    while prev != flat:
        prev = flat
        flat = re.sub(r"(?<=\d)[ .\-]+(?=\d)", "", flat)
    return t, flat


def _entropy_hit(text):
    """A token of 32+ characters with a digit, a letter, and high entropy. Text is already lowercase."""
    for m in re.finditer(r"[a-z0-9+/_]{32,}={0,2}", text):
        s = m.group().rstrip("=")
        if len(s) < 32 or not any(c.isdigit() for c in s) or not any(c.isalpha() for c in s):
            continue
        if len(set(s)) < 8:
            continue
        freq = {}
        for c in s:
            freq[c] = freq.get(c, 0) + 1
        n = len(s)
        if -sum((c / n) * math.log2(c / n) for c in freq.values()) >= 3.5:
            return True
    return False


def _ssn(flat):
    for m in re.finditer(r"(?<!\d)\d{9}(?!\d)", flat):
        d = m.group()
        area, group, serial = int(d[:3]), int(d[3:5]), int(d[5:])
        if area not in (0, 666) and area < 900 and group != 0 and serial != 0:
            return True
    return False


def _card(flat):
    for m in re.finditer(r"(?<!\d)\d{13,19}(?!\d)", flat):
        if _luhn(m.group()):
            return True
    return False


def _name_hit(text, names):
    for n in names or ():
        s = " ".join(str(n).casefold().split())
        if len(s) < 2:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", text):
            return True
    return False


def clean(text, cap):
    """NFKC, drop control/format (zero-width, bidi) characters, collapse whitespace, cap."""
    t = unicodedata.normalize("NFKC", str(text))
    t = "".join(c for c in t if unicodedata.category(c) not in ("Cc", "Cf"))
    return " ".join(t.split())[:cap]


def strip_markup(t):
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    return " ".join(re.sub(r"[*_`~#>|]+", "", t).split())


def _lint(text, hosts, names, pii):
    """Reason code, or None. Caller catches exceptions (fail closed)."""
    norm, flat = _norm_reply(text)
    if _PEM.search(norm) or _PREFIX.search(norm) or _SECRET_VALUE.search(norm) or _entropy_hit(norm):
        return "secret"
    if _ssn(flat):
        return "ssn"
    if _card(flat):
        return "card"
    if not pii:
        return None
    if _EMAIL.search(norm):
        return "email"
    if _PHONE.search(norm) or _PHONE_FLAT.search(flat):
        return "phone"
    if _ADDRESS.search(norm) or _ADDRESS.search(flat):
        return "address"
    # A colon MAC is also an IPv6-shaped run. Check it first so the reason is the MAC, not the address.
    if _MAC.search(norm):
        return "mac"
    if _IP.search(norm):
        return "ip"
    if _HOST.search(norm):
        return "host"
    for h in hosts or ():
        if re.search(r"(?<![a-z0-9-])" + re.escape(str(h).casefold()) + r"(?![a-z0-9-])", norm):
            return "host"
    if _name_hit(norm, names):
        return "name"
    return None


def lint_reply(text, hosts=(), names=(), pii=None):
    """Reason code if a reply must not be spoken, else None.

    Secrets, SSNs and card numbers are always blocked. Email, phone, street address, IP, MAC,
    private host names and household names are blocked only when pii is true (default).
    An exception inside the checker blocks the reply. The code is safe to log. The reply text is not.
    """
    try:
        return _lint(text, hosts, names, True if pii is None else bool(pii))
    except Exception:
        return "error"


def new_request_id():
    return "vs_" + secrets.token_urlsafe(17)[:22].ljust(22, "A")


# Persona slugs the admin API and webhooks.json share. "grok" is the station voice, not a bot.
BOT_SLUG = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
WEBHOOK_HOST = "api2.cursor.sh"   # the only host the admin API may save or test


def bot_slug_ok(name):
    return isinstance(name, str) and name != "grok" and BOT_SLUG.fullmatch(name) is not None


def _webhook_ok(ent):
    """Allowed URL, non-empty token, enabled (default true). http is only the loopback the tests use."""
    return (isinstance(ent, dict) and re.match(r"https://|http://127\.0\.0\.1:", str(ent.get("url", "")))
            and isinstance(ent.get("token"), str) and ent["token"] and ent.get("enabled", True) is True)


def webhook_target_ok(url, key):
    """Admin allowlist for a webhook URL and its key. Never logs either.

    https only, host exactly WEBHOOK_HOST, no userinfo, no port other than 443, a non-empty path, at most 512
    characters. The raw authority has to be the host (or host:443), so a trailing dot, a percent-encoding, or a
    look-alike that a redirect or a second parser would send elsewhere does not pass. The key is 8..400 visible
    ASCII characters.
    """
    if not isinstance(url, str) or not isinstance(key, str):
        return False
    if not (1 <= len(url) <= 512) or not (8 <= len(key) <= 400):
        return False
    if any(ord(c) < 0x21 or ord(c) > 0x7e for c in url) or any(ord(c) < 0x21 or ord(c) > 0x7e for c in key):
        return False
    if "@" in url or "\\" in url:          # userinfo, or a slash a redirect-y parser would treat as a host
        return False
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or p.username is not None or p.password is not None or p.fragment:
        return False
    if p.hostname != WEBHOOK_HOST or p.port not in (None, 443):
        return False
    if p.netloc not in (WEBHOOK_HOST, WEBHOOK_HOST + ":443"):
        return False
    if not p.path or p.path == "/":
        return False
    return True


def load_webhooks(raw):
    """{"<bot>": {"url", "token"}} -> validated map. Never logged.

    Slugs match the persona pattern. A "_router" entry is dropped; router mode is retired.
    """
    try:
        doc = json.loads(raw or "{}")
    except ValueError:
        return {}
    out = {}
    if not isinstance(doc, dict):
        return {}
    for name, ent in doc.items():
        if name == "_router" or not bot_slug_ok(str(name)) or not _webhook_ok(ent):
            continue
        out[name] = {"url": ent["url"], "token": ent["token"]}
    return out


def route(hooks, bot):
    """This bot's own webhook, or None. There is no router fallback."""
    if not isinstance(hooks, dict):
        return None
    ent = hooks.get(bot)
    return ent if isinstance(ent, dict) else None


class _Wire(dict):
    """JSON object is the real keys. payload()["tier_max"] still returns the tier argument (callers and tests
    read it) without putting tier_max or pii_mode on the wire."""

    def __init__(self, body, tier_max):
        super().__init__(body)
        self._tier = tier_max

    def __getitem__(self, key):
        if key == "tier_max" and "tier_max" not in dict.keys(self):
            return self._tier
        return dict.__getitem__(self, key)


def payload(rid, bot, question, reply_by_iso, tier_max=None, test=False, include_avatar=False, kind="", release="",
            commit=""):
    """Webhook body v1: v, request_id, persona, the cleaned question, reply_by. test only when asked.

    include_avatar is accepted so older callers keep working and is not serialized. pii_mode is never a field.
    ["tier_max"] returns the tier argument (default 0) and is not on the wire.
    kind is sent for a list request (bot_list) or a picture ask (avatar). Answer webhooks omit it.
    """
    del include_avatar
    body = {"v": 1, "request_id": rid, "persona": bot, "question": clean(question, Q_MAX),
            "reply_by": str(reply_by_iso or "")}
    if test:
        body["test"] = True
    if kind == "bot_list":
        body["kind"] = "bot_list"
    elif kind == "avatar":
        body["kind"] = "avatar"
    elif kind == "update_helper":
        body["kind"] = "update_helper"
    elif kind == "update_bot":
        if not (re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", release or "")
                and re.fullmatch(r"[0-9a-f]{40}", commit or "")):
            raise ValueError("update_bot needs a release and a commit")
        body.update(kind="update_bot", release=release, commit=commit)
    return _Wire(body, MAX_TIER if tier_max is None else tier_max)


def _list_ask_norm(raw):
    """{"out": rid or "", "last": epoch}. A missing or odd value is no open ask and no previous time."""
    ask = raw if isinstance(raw, dict) else {}
    out = ask.get("out") if isinstance(ask.get("out"), str) else ""
    last = ask.get("last")
    if isinstance(last, bool) or not isinstance(last, (int, float)):
        last = 0.0
    return {"out": out, "last": float(last)}


class Limits:
    """B10, persisted per day: {"day": .., "bots": {bot: {"n": .., "last": ..}}, "total": .., "out": {bot: rid}}.

    list_ask is separate from the answer caps and survives the daily wipe: {"out": rid or "", "last": epoch}.
    """

    def __init__(self, path, day_fn, per_bot_day=None, global_day=None, gap_s=None):
        self.path, self.day = path, day_fn
        self.per_bot_day = PER_BOT_DAY if per_bot_day is None else per_bot_day
        self.global_day = GLOBAL_DAY if global_day is None else global_day
        self.gap_s = GAP_S if gap_s is None else gap_s

    def _load(self):
        try:
            with open(self.path) as f:
                s = json.load(f)
        except (OSError, ValueError):
            s = {}
        if not isinstance(s, dict):
            s = {}
        kept = _list_ask_norm(s.get("list_ask"))
        if s.get("day") != self.day():
            s = {"day": self.day(), "bots": {}, "total": 0, "out": {}, "test_out": {}}
        s.pop("guest", None)  # stale guest flag from an older file: ignored, not kept
        s["list_ask"] = kept
        return s

    def _save(self, s):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f)
        os.replace(tmp, self.path)

    def take(self, bot, rid, now=None, test=False, preempt=None):
        """Reserve one handoff; None if allowed, else the reason. Counted before the POST, so failures count.

        0.12.11: a slot held by an admin test (test=True when taken) gives way to a real question:
        preempt(bot) is called to cancel that test and the question takes the slot. A slot held by a
        real question still refuses with "outstanding". A test never preempts anything.
        """
        now = time.time() if now is None else now
        s = self._load()
        b = s["bots"].setdefault(bot, {"n": 0, "last": 0.0})
        tests = s.setdefault("test_out", {})
        if s["out"].get(bot) and (test or tests.get(bot) != s["out"][bot]):
            return "outstanding"
        # 0.12.12 (Nix L2): the caps and the gap are checked before a Test is preempted, so a question the
        # limits refuse leaves the Test running.
        if self.per_bot_day > 0 and b["n"] >= self.per_bot_day:
            return "bot daily cap"
        if self.global_day > 0 and s["total"] >= self.global_day:
            return "global daily cap"
        if self.gap_s > 0 and now - b["last"] < self.gap_s:
            return "too soon"
        if s["out"].get(bot):  # held by an admin Test: it gives way to this question
            if preempt is not None:
                try:
                    preempt(bot)
                except Exception:
                    pass
            s["out"].pop(bot, None)
            tests.pop(bot, None)
        b["n"] += 1
        b["last"] = now
        s["total"] += 1
        s["out"][bot] = rid
        if test:
            tests[bot] = rid
        else:
            tests.pop(bot, None)
        self._save(s)
        return None

    def block_reason(self, bot, now=None):
        """Why the next take() would refuse, or None. Does not reserve a slot."""
        now = time.time() if now is None else now
        s = self._load()
        b = (s.get("bots") or {}).get(bot) or {"n": 0, "last": 0.0}
        held = (s.get("out") or {}).get(bot)
        if held and (s.get("test_out") or {}).get(bot) != held:
            return "outstanding"  # an admin test's slot gives way to a voice question
        if self.per_bot_day > 0 and b.get("n", 0) >= self.per_bot_day:
            return "bot daily cap"
        if self.global_day > 0 and s.get("total", 0) >= self.global_day:
            return "global daily cap"
        if self.gap_s > 0 and now - b.get("last", 0.0) < self.gap_s:
            return "too soon"
        return None

    def release(self, bot, rid=None):
        """Free this bot's slot. With rid, only when that request still holds it (a preempted test
        must not free the question that took its place)."""
        s = self._load()
        if rid is not None and s["out"].get(bot) not in (None, rid):
            return
        s["out"].pop(bot, None)
        s.setdefault("test_out", {}).pop(bot, None)
        self._save(s)

    def list_open(self):
        """Request id of the open list ask, or ""."""
        return _list_ask_norm(self._load().get("list_ask"))["out"]

    def take_list(self, rid, now=None):
        """Reserve one list request. None if allowed, else "too soon" or "outstanding".

        Not counted against the answer caps. One open ask, and at most one per 60 seconds.
        A future last (clock skew) is too soon. last 0 means there has not been an ask.
        """
        now = time.time() if now is None else now
        s = self._load()
        ask = _list_ask_norm(s.get("list_ask"))
        out, last = ask["out"], ask["last"]
        if last and last > now:
            return "too soon"
        if last and 0 < (now - last) < 60:
            return "too soon"
        if out:
            return "outstanding"
        if last and now - last < 60:
            return "too soon"
        s["list_ask"] = {"out": rid, "last": float(now)}
        self._save(s)
        return None

    def release_list(self):
        """Drop the open list slot. The 60 second gap (last) stays."""
        s = self._load()
        ask = _list_ask_norm(s.get("list_ask"))
        s["list_ask"] = {"out": "", "last": ask["last"]}
        self._save(s)


def write_pending(pdir, rid, bot, nonce, expires_ms, question="", pii_mode=True, test=False, reply_by="", kind=""):
    """Pending request the reply must match. tier_max is always 0.

    expires_ms is the accept-until time (clock-skew window), which may be later than reply_by.
    kind is stored only for a list request. Answer pending files omit it.
    """
    os.makedirs(pdir, exist_ok=True)
    doc = {"v": 1, "request_id": rid, "persona": bot, "nonce": nonce, "expires_ms": int(expires_ms),
           "question": clean(question, Q_MAX), "pii_mode": True if pii_mode is None else bool(pii_mode),
           "tier_max": 0, "test": bool(test), "reply_by": str(reply_by or "")}
    if kind in ("bot_list", "update_helper", "update_bot"):
        doc["kind"] = kind
    tmp = os.path.join(pdir, f".{rid}.tmp")
    with open(tmp, "w") as f:
        json.dump(doc, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(pdir, rid + ".json"))


def remove_pending(pdir, rid):
    try:
        os.unlink(os.path.join(pdir, rid + ".json"))
    except OSError:
        pass


NOTE_OPS, NOTE_SCOPES = ("add", "remove", "remember", "undo"), ("own_notes", "shared")


def note_from(doc, hosts=(), names=(), pii=None):
    """T1 note proposal from a tile-ingest reply doc (already validated there; checked again here). dict or (None, reason)."""
    p, sha = doc.get("proposal"), doc.get("proposal_sha")
    if doc.get("tier") != 1 or not isinstance(p, dict) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None, "note-schema"
    if p.get("op") not in NOTE_OPS or p.get("scope", "own_notes") not in NOTE_SCOPES:
        return None, "note-schema"
    text = clean(p.get("text", ""), 121)
    if not text or len(text) > 120 or re.search(r"save\s*it", text, re.I):
        return None, "note-text"
    why = lint_reply(text, hosts, names, pii=pii)
    if why:
        return None, "lint:" + why
    lst = clean(p.get("list", ""), 25) if p.get("list") else ""
    return {"op": p["op"], "scope": p.get("scope", "own_notes"), "list": lst, "text": text, "sha": sha,
            "needs_tap": p.get("scope", "own_notes") != "own_notes"}, None


def take_reply(rdir, rid, bot, nonce, hosts=(), seen=None, note_ok=False, names=(), pii=None, info=None):
    """(text, None) for a reply that is bound, in scope and clean; (None, reason) for a bad one; (None, None) if none yet.
    The reply file is consumed (deleted) on read, so it can be spoken at most once; seen dedupes request_ids.
    A reply for a different request_id cannot satisfy this one (a test rid never resolves a real request)."""
    p = os.path.join(rdir, rid + ".json")
    try:
        with open(p) as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError):
        return None, "unreadable"
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass
    interim = isinstance(doc, dict) and doc.get("interim") is True  # 0.13.0: an acknowledgement; the answer follows
    if info is not None:
        info["interim"] = interim
        info["n"] = doc.get("n") if isinstance(doc, dict) and type(doc.get("n")) is int else 1
    if seen is not None:
        if rid in seen:
            return None, "duplicate"
        if not interim:
            seen.add(rid)
    if not isinstance(doc, dict) or doc.get("request_id") != rid or doc.get("agent") != bot:
        return None, "unbound"
    if not secrets.compare_digest(str(doc.get("nonce", "")), nonce):
        return None, "nonce"
    tier, ac = doc.get("tier"), doc.get("action_class")
    if note_ok and ac == "note":  # T1 (T1_ENABLED): a proposal for the on-station read-back, never spoken as an answer
        return note_from(doc, hosts, names, pii=pii)
    if not isinstance(tier, int) or tier > MAX_TIER or ac not in TIER_ACTIONS.get(tier, ()):
        return None, "above-scope"
    text = strip_markup(clean(doc.get("text", ""), REPLY_MAX + 1))
    if not text or len(text) > REPLY_MAX:
        return None, "length"
    why = lint_reply(text, hosts, names, pii=pii)
    if why:
        return None, "lint:" + why
    return text, None
