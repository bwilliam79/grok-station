"""Teammate handoff (real-bot pilot, Nix GO 2026-10-19): the station asks a teammate's own Grok Bot routine and speaks its
answer. Off unless HANDOFF_ENABLED=1 and the persona has a route (the "_router" webhook if set, else that bot's own
webhook); every bot ships at T0 (answer only).

Blockers covered here: B4 (one POST, never retried after the body is sent; the routine's step 0 checks source/v/request_id/
reply_by), B6 (question stripped of control/zero-width/bidi, capped at 300, sent as delimited untrusted data), B10 (per bot
8/day, >= 30 s apart, one outstanding; failed POSTs count; plus a global daily total), B13 (lint before speech), B14 (nonce
bound pending file, single use, expiry; the answer is spoken only while the session still waits). Logs carry request_id,
agent, length and verdict only.
"""
import json, os, re, secrets, time, unicodedata

REQUEST_ID = re.compile(r"^vs_[A-Za-z0-9_-]{22}$")
Q_MAX, REPLY_MAX = 300, 400
TIER_ACTIONS = {0: ("answer",), 1: ("answer", "note"), 2: ("answer", "note", "queue_approval")}
MAX_TIER = 0                                   # pilot: every bot answers only; a reply above T0 is dropped
PER_BOT_DAY, GAP_S, GLOBAL_DAY = 8, 30.0, 24
BEGIN, END = "<<<STATION_QUESTION untrusted>>>", "<<<END_STATION_QUESTION>>>"

# Python port of tile-ingest's B13 voice lint (second gate before anything is spoken).
RULES = [
    ("vin", re.compile(r"\b(?=[A-HJ-NPR-Z0-9]*\d)(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])[A-HJ-NPR-Z0-9]{17}\b", re.I)),
    ("digits", re.compile(r"\d{6,}")),
    ("url", re.compile(r"https?:|www\.|\b[a-z0-9-]+\.(com|net|org|io|dev|app|ai|co|us|me|tv|info|xyz|cloud|gov|edu)\b", re.I)),
    ("email", re.compile(r"[^\s@]+@[^\s@]+")),
    ("ip", re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b|(?<![\w:])(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}(?![\w:])|[0-9a-f]{1,4}::|::[0-9a-f]{1,4}", re.I)),
    ("host", re.compile(r"\.(lan|local|internal|home\.arpa|localdomain)\b|\blocalhost\b", re.I)),
    ("phone", re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")),
    ("secret", re.compile(r"\b(password|passcode|pin|token|api[ _-]?key|secret|ssn|social security|routing|account number)\b", re.I)),
    ("money", re.compile(r"[$€£]\s?\d|\b\d[\d,.]*\s?(dollars|usd|bucks)\b|\b(balance|net worth|salary|paycheck)\b", re.I)),
    ("medical", re.compile(r"\b(diagnos\w*|prescri\w*|medication|blood pressure|a1c|glucose|therapy|symptom\w*|dosage|mg)\b", re.I)),
    ("address", re.compile(r"\b\d{1,6}\s+\w+(\s\w+)?\s(st|street|ave|avenue|rd|road|blvd|ln|lane|dr|drive|ct|court|way|pl)\b", re.I)),
]


def clean(text, cap):
    """NFKC, drop control/format (zero-width, bidi) characters, collapse whitespace, cap."""
    t = unicodedata.normalize("NFKC", str(text))
    t = "".join(c for c in t if unicodedata.category(c) not in ("Cc", "Cf"))
    return " ".join(t.split())[:cap]


def strip_markup(t):
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    return " ".join(re.sub(r"[*_`~#>|]+", "", t).split())


def lint_reply(text, hosts=()):
    """Reason code if a reply must not be spoken, else None."""
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in str(text)):
        return "chars"
    t = unicodedata.normalize("NFKC", str(text))
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in t):
        return "chars"
    for code, rx in RULES:
        if rx.search(t):
            return code
    low = t.casefold()
    for h in hosts:
        if re.search(r"(?<![a-z0-9-])" + re.escape(h.casefold()) + r"(?![a-z0-9-])", low):
            return "host"
    return None


def new_request_id():
    return "vs_" + secrets.token_urlsafe(17)[:22].ljust(22, "A")


def _webhook_ok(ent):
    """Same bar for a bot entry and for "_router": allowed URL, non-empty token, enabled (default true)."""
    return (isinstance(ent, dict) and re.match(r"https://|http://127\.0\.0\.1:", str(ent.get("url", "")))
            and isinstance(ent.get("token"), str) and ent["token"] and ent.get("enabled", True) is True)


def load_webhooks(raw):
    """{"<bot>": {"url", "token"}, optional "_router": same shape} -> validated map. Never logged.

    Bot names must match [a-z]{2,16}. "_router" is kept under that key when it passes the same checks.
    """
    try:
        doc = json.loads(raw or "{}")
    except ValueError:
        return {}
    out = {}
    if not isinstance(doc, dict):
        return {}
    for name, ent in doc.items():
        if not _webhook_ok(ent):
            continue
        if name == "_router" or re.fullmatch(r"[a-z]{2,16}", str(name)):
            out[name] = {"url": ent["url"], "token": ent["token"]}
    return out


def route(hooks, bot):
    """Entry to POST: the "_router" entry when present, else this bot's webhook, else None."""
    if not isinstance(hooks, dict):
        return None
    return hooks["_router"] if "_router" in hooks else hooks.get(bot)


def payload(rid, bot, question, reply_by_iso, tier_max=None):
    """The webhook body. The question is untrusted data, delimited; the routine treats it as a question only."""
    return {"source": "grok-station", "v": 1, "request_id": rid, "persona": bot, "tier_max": MAX_TIER if tier_max is None else tier_max,
            "reply_by": reply_by_iso, "question": f"{BEGIN}\n{question}\n{END}"}


class Limits:
    """B10, persisted per day: {"day": .., "bots": {bot: {"n": .., "last": ..}}, "total": .., "out": {bot: rid}}."""

    def __init__(self, path, day_fn):
        self.path, self.day = path, day_fn

    def _load(self):
        try:
            with open(self.path) as f:
                s = json.load(f)
        except (OSError, ValueError):
            s = {}
        if s.get("day") != self.day():
            s = {"day": self.day(), "bots": {}, "total": 0, "out": {}}
        return s

    def _save(self, s):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f)
        os.replace(tmp, self.path)

    def take(self, bot, rid, now=None):
        """Reserve one handoff; None if allowed, else the reason. Counted before the POST, so failures count."""
        now = time.time() if now is None else now
        s = self._load()
        b = s["bots"].setdefault(bot, {"n": 0, "last": 0.0})
        if s["out"].get(bot):
            return "outstanding"
        if b["n"] >= PER_BOT_DAY:
            return "bot daily cap"
        if s["total"] >= GLOBAL_DAY:
            return "global daily cap"
        if now - b["last"] < GAP_S:
            return "too soon"
        b["n"] += 1
        b["last"] = now
        s["total"] += 1
        s["out"][bot] = rid
        self._save(s)
        return None

    def release(self, bot):
        s = self._load()
        s["out"].pop(bot, None)
        self._save(s)


def write_pending(pdir, rid, bot, nonce, expires_ms):
    os.makedirs(pdir, exist_ok=True)
    tmp = os.path.join(pdir, f".{rid}.tmp")
    with open(tmp, "w") as f:
        json.dump({"v": 1, "request_id": rid, "persona": bot, "nonce": nonce, "expires_ms": int(expires_ms)}, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(pdir, rid + ".json"))


def remove_pending(pdir, rid):
    try:
        os.unlink(os.path.join(pdir, rid + ".json"))
    except OSError:
        pass


NOTE_OPS, NOTE_SCOPES = ("add", "remove", "remember", "undo"), ("own_notes", "shared")


def note_from(doc, hosts=()):
    """T1 note proposal from a tile-ingest reply doc (already validated there; checked again here). dict or (None, reason)."""
    p, sha = doc.get("proposal"), doc.get("proposal_sha")
    if doc.get("tier") != 1 or not isinstance(p, dict) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None, "note-schema"
    if p.get("op") not in NOTE_OPS or p.get("scope", "own_notes") not in NOTE_SCOPES:
        return None, "note-schema"
    text = clean(p.get("text", ""), 121)
    if not text or len(text) > 120 or re.search(r"save\s*it", text, re.I):
        return None, "note-text"
    why = lint_reply(text, hosts)
    if why:
        return None, "lint:" + why
    lst = clean(p.get("list", ""), 25) if p.get("list") else ""
    return {"op": p["op"], "scope": p.get("scope", "own_notes"), "list": lst, "text": text, "sha": sha,
            "needs_tap": p.get("scope", "own_notes") != "own_notes"}, None


def take_reply(rdir, rid, bot, nonce, hosts=(), seen=None, note_ok=False):
    """(text, None) for a reply that is bound, in scope and clean; (None, reason) for a bad one; (None, None) if none yet.
    The reply file is consumed (deleted) on read, so it can be spoken at most once; seen dedupes request_ids."""
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
    if seen is not None:
        if rid in seen:
            return None, "duplicate"
        seen.add(rid)
    if not isinstance(doc, dict) or doc.get("request_id") != rid or doc.get("agent") != bot:
        return None, "unbound"
    if not secrets.compare_digest(str(doc.get("nonce", "")), nonce):
        return None, "nonce"
    tier, ac = doc.get("tier"), doc.get("action_class")
    if note_ok and ac == "note":  # T1 (T1_ENABLED): a proposal for the on-station read-back, never spoken as an answer
        return note_from(doc, hosts)
    if not isinstance(tier, int) or tier > MAX_TIER or ac not in TIER_ACTIONS.get(tier, ()):
        return None, "above-scope"
    text = strip_markup(clean(doc.get("text", ""), REPLY_MAX + 1))
    if not text or len(text) > REPLY_MAX:
        return None, "length"
    why = lint_reply(text, hosts)
    if why:
        return None, "lint:" + why
    return text, None
