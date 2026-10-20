"""Release manifests and staged updates.

Pure logic. Callers inject the clock, the disk check, the manifest fetcher,
and every disk or process operation. This module does not open a socket.

A release is a JSON manifest fetched over HTTPS from GitHub (MANIFEST_URL).
There is no signature. Relay images are pinned by digest from the manifest.
The station tarball is checked by sha256. issued_at and expires span at most
30 days. An expired manifest is "Can't check". A future-dated manifest is
rejected. Signing may be added later.
"""

import json, os, re, ssl
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

MANIFEST_URL = (
    "https://github.com/bwilliam79/grok-station/releases/latest/download/manifest.json"
)
_MANIFEST_HOSTS = frozenset({"github.com", "objects.githubusercontent.com"})

BUSY_WAIT_S = 600
DISK_MIN_MB = 500
SOAK_S = 600
MAX_VALIDITY = timedelta(days=30)
FUTURE_SKEW = timedelta(minutes=5)
MAX_MANIFEST_BYTES = 64 * 1024
MAX_REQUEST_CHARS = 1024

EXPIRED_TITLE = "Can't check"
EXPIRED_MESSAGE = "Update info is out of date. Updates may be hidden."
EXPIRED_AUDIT = "update-check-expired"
_REASONS = ("expired", "future", "bad-format", "fetch-failed")

_VERSION = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TARBALL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_IMAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TOKEN = re.compile(r"^[A-Za-z0-9._:-]+$")
_REQUEST_BODY = re.compile(
    r'\A\s*\{\s*"version"\s*:\s*"(v[0-9]+\.[0-9]+\.[0-9]+)"\s*\}\s*\Z')
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_SHA_PREFIXED = re.compile(r"\bsha256:[0-9a-fA-F]{16,}\b", re.IGNORECASE)
_IPV4 = re.compile(r"\b[0-9]{1,3}(?:\.[0-9]{1,3}){3}\b")
_RELEASE_KEYS = {"version", "min_version_floor", "issued_at", "expires", "components", "compat"}
_REQUIRED_OPS = ("stage", "switch", "health", "rollback", "is_busy", "disk_free_mb", "clock")

__all__ = [
    "ManifestError", "MANIFEST_URL", "fetch_manifest",
    "parse_manifest", "load_trusted_manifest",
    "check_update", "check_now", "scheduled_check", "present_check_error",
    "validate_update_request", "Decision", "plan_update", "Plan", "Updater",
    "load_settings", "set_autocheck", "format_audit",
    "EXPIRED_TITLE", "EXPIRED_MESSAGE", "EXPIRED_AUDIT",
    "BUSY_WAIT_S", "DISK_MIN_MB", "SOAK_S", "MAX_MANIFEST_BYTES",
]


class ManifestError(Exception):
    """A manifest cannot be used. `reason` is expired, future, bad-format, or fetch-failed."""

    def __init__(self, reason, detail=None):
        if reason not in _REASONS:
            raise ValueError("reason")
        self.reason = reason
        self.detail = detail
        super().__init__(reason if not detail else f"{reason}: {detail}")


class Decision:
    """Result of validate_update_request. Equals the audit string on rejection."""

    def __init__(self, ok, reason, version):
        self.ok = bool(ok)
        self.reason = reason
        self.version = version
        self.audit = None if ok else f"update-rejected {reason}"

    def __eq__(self, other):
        if isinstance(other, str):
            return self.audit == other
        if isinstance(other, Decision):
            return (self.ok, self.reason, self.version) == (other.ok, other.reason, other.version)
        return NotImplemented

    def __repr__(self):
        return f"Decision(ok={self.ok!r}, reason={self.reason!r}, audit={self.audit!r})"


class CheckResult:
    def __init__(self, **kw):
        self.ok = kw.get("ok")
        self.update = kw.get("update")
        self.installed = kw.get("installed")
        self.version = kw.get("version")
        self.floor = kw.get("floor")
        self.reason = kw.get("reason")
        self.audit = kw.get("audit")
        self.display = kw.get("display")
        self.title = kw.get("title")
        self.message = kw.get("message")
        self.skipped = kw.get("skipped", False)

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def __repr__(self):
        return (f"CheckResult(ok={self.ok!r}, update={self.update!r}, "
                f"reason={self.reason!r}, audit={self.audit!r})")


class Plan(list):
    """Steps returned by plan_update. Equal to the step list; flag is an attribute."""

    def __init__(self, steps, station_only_allowed):
        super().__init__(steps)
        self.station_only_allowed = bool(station_only_allowed)

    @property
    def steps(self):
        return list(self)


class ApplyResult:
    def __init__(self, ok, status, reason, events, lines, kept, current):
        self.ok = ok
        self.status = status
        self.reason = reason
        self.events = events
        self.lines = lines
        self.kept = kept
        self.current = current

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def __repr__(self):
        return f"ApplyResult(ok={self.ok!r}, status={self.status!r}, reason={self.reason!r})"


def _json_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in obj:
            raise ManifestError("bad-format", "duplicate")
        obj[key] = value
    return obj


def _load_json(text):
    try:
        return json.loads(text, object_pairs_hook=_json_pairs)
    except ManifestError:
        raise
    except (json.JSONDecodeError, ValueError):
        raise ManifestError("bad-format", "json")


def _atomic_json(path, doc):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _require_manifest_url(url):
    """https only, and only github.com or objects.githubusercontent.com."""
    if not isinstance(url, str) or not url:
        raise ManifestError("fetch-failed", "url")
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in url) or "\\" in url or "@" in url:
        raise ManifestError("fetch-failed", "url")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ManifestError("fetch-failed", "url")
    if parts.scheme != "https":
        raise ManifestError("fetch-failed", "scheme")
    host = parts.hostname
    if host not in _MANIFEST_HOSTS:
        raise ManifestError("fetch-failed", "host")
    if parts.port not in (None, 443):
        raise ManifestError("fetch-failed", "port")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise ManifestError("fetch-failed", "url")
    netloc = parts.netloc.lower()
    if netloc not in (host, host + ":443"):
        raise ManifestError("fetch-failed", "host")
    return url


def _tls_context():
    """Certificate and hostname checks on. Never a context that skips them."""
    try:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
    except Exception:
        raise ManifestError("fetch-failed", "tls") from None
    if context.verify_mode != ssl.CERT_REQUIRED or context.check_hostname is not True:
        raise ManifestError("fetch-failed", "tls")
    return context


def fetch_manifest(fetcher, url=None):
    """Download a release manifest. `fetcher(url, context)` returns the body.

    `url` defaults to MANIFEST_URL. It must be https on github.com or
    objects.githubusercontent.com. `context` has TLS certificate and hostname
    verification on; the fetcher must use that context and must not follow a
    redirect onto another host. The body is capped at MAX_MANIFEST_BYTES.
    Raises ManifestError('fetch-failed') on a rejected URL, a transport error,
    or an oversized body. This function does not open a socket.
    """
    if url is None:
        url = MANIFEST_URL
    url = _require_manifest_url(url)
    if not callable(fetcher):
        raise ManifestError("fetch-failed", "fetcher")
    context = _tls_context()
    try:
        body = fetcher(url, context)
    except Exception:
        raise ManifestError("fetch-failed", "transport") from None
    if isinstance(body, str):
        body = body.encode("utf-8")
    elif isinstance(body, bytearray):
        body = bytes(body)
    if not isinstance(body, bytes):
        raise ManifestError("fetch-failed", "body")
    if len(body) > MAX_MANIFEST_BYTES:
        raise ManifestError("fetch-failed", "size")
    return body


def _parse_time(value):
    if isinstance(value, bool) or value is None:
        raise ManifestError("bad-format", "time")
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise ManifestError("bad-format", "time")
    if not isinstance(value, str):
        raise ManifestError("bad-format", "time")
    text = value.strip()
    if re.fullmatch(r"[0-9]+(\.[0-9]+)?", text):
        return _parse_time(float(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ManifestError("bad-format", "time")
    if parsed.tzinfo is None:
        raise ManifestError("bad-format", "time")
    return parsed.astimezone(timezone.utc)


def _coerce_now(now):
    if now is None:
        return datetime.now(timezone.utc)
    return _parse_time(now) if not isinstance(now, datetime) else (
        now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc))


def _version_tuple(value):
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ManifestError("bad-format", "version")
    return tuple(int(part) for part in value[1:].split("."))


def _release_times(doc):
    return _parse_time(doc["issued_at"]), _parse_time(doc["expires"])


def _validate_release_shape(doc):
    if not isinstance(doc, dict) or set(doc) != _RELEASE_KEYS:
        raise ManifestError("bad-format", "manifest")
    _version_tuple(doc["version"])
    _version_tuple(doc["min_version_floor"])
    issued, expires = _release_times(doc)
    if expires <= issued or expires - issued > MAX_VALIDITY:
        raise ManifestError("bad-format", "validity")
    components = doc["components"]
    if not isinstance(components, dict) or set(components) != {"station", "relay"}:
        raise ManifestError("bad-format", "components")
    station = components["station"]
    relay = components["relay"]
    if not isinstance(station, dict) or set(station) != {"tarball", "sha256"}:
        raise ManifestError("bad-format", "station")
    name, digest = station["tarball"], station["sha256"]
    if not isinstance(name, str) or _TARBALL.fullmatch(name) is None or ".." in name:
        raise ManifestError("bad-format", "tarball")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ManifestError("bad-format", "digest")
    if not isinstance(relay, dict) or set(relay) != {"images"}:
        raise ManifestError("bad-format", "relay")
    images = relay["images"]
    if not isinstance(images, dict) or not images:
        raise ManifestError("bad-format", "images")
    for image, image_digest in images.items():
        if not isinstance(image, str) or _IMAGE_NAME.fullmatch(image) is None or ".." in image:
            raise ManifestError("bad-format", "image")
        if not isinstance(image_digest, str) or _IMAGE_DIGEST.fullmatch(image_digest) is None:
            raise ManifestError("bad-format", "digest")
    compat = doc["compat"]
    if not isinstance(compat, dict) or set(compat) != {"station_min_relay", "relay_min_station"}:
        raise ManifestError("bad-format", "compat")
    _version_tuple(compat["station_min_relay"])
    _version_tuple(compat["relay_min_station"])
    return issued, expires


def _validate_release_time(doc, now):
    issued, expires = _release_times(doc)
    if issued > now + FUTURE_SKEW:
        raise ManifestError("future")
    if now > expires:
        raise ManifestError("expired")


def _decode_release(data):
    if isinstance(data, str):
        raw = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raise ManifestError("bad-format", "type")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError("bad-format", "size")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError("bad-format", "encoding")
    doc = _load_json(text)
    if not isinstance(doc, dict):
        raise ManifestError("bad-format", "json")
    return raw, doc


def parse_manifest(data, now=None):
    """Validate a release manifest. Raises ManifestError. There is no signature."""
    if isinstance(data, dict):
        doc = data
        _validate_release_shape(doc)
    else:
        _raw, doc = _decode_release(data)
        _validate_release_shape(doc)
    _validate_release_time(doc, _coerce_now(now))
    return doc


def load_trusted_manifest(data, now=None):
    """Validate a fetched manifest. Trust is the HTTPS fetch, not a signature."""
    return parse_manifest(data, now=now)


def present_check_error(err):
    """Caller-facing copy for an expired manifest. Other reasons return None."""
    if isinstance(err, ManifestError) and err.reason == "expired":
        return {
            "title": EXPIRED_TITLE,
            "message": EXPIRED_MESSAGE,
            "display": (EXPIRED_TITLE, EXPIRED_MESSAGE),
            "audit": EXPIRED_AUDIT,
        }
    return None


def _expired_result(installed):
    return CheckResult(
        ok=False, update=False, installed=installed, version=None, floor=None,
        reason="expired", audit=EXPIRED_AUDIT,
        display=(EXPIRED_TITLE, EXPIRED_MESSAGE),
        title=EXPIRED_TITLE, message=EXPIRED_MESSAGE, skipped=False)


def _compare_installed(installed, doc):
    inst = _version_tuple(installed)
    ver = _version_tuple(doc["version"])
    floor = _version_tuple(doc["min_version_floor"])
    if ver < inst or ver < floor:
        reason, update = "downgrade", False
    elif ver == inst:
        reason, update = "current", False
    else:
        reason, update = None, True
    return CheckResult(
        ok=True, update=update, installed=installed, version=doc["version"],
        floor=doc["min_version_floor"], reason=reason, audit=None, display=None,
        title=None, message=None, skipped=False)


def check_update(installed, manifest, *, now=None):
    """Compare `installed` with a release manifest.

    An expired manifest does not raise: the caller shows "Can't check" and
    "Update info is out of date. Updates may be hidden." and audits
    `update-check-expired`. A future or badly formed manifest raises
    ManifestError. Dict and bytes are parsed the same way.
    """
    if not isinstance(installed, str) or _VERSION.fullmatch(installed) is None:
        raise ManifestError("bad-format", "installed")
    now_dt = _coerce_now(now)
    try:
        doc = parse_manifest(manifest, now=now_dt)
    except ManifestError as err:
        if err.reason == "expired":
            return _expired_result(installed)
        raise
    return _compare_installed(installed, doc)


def check_now(installed, manifest, settings=None, **kwargs):
    """Manual check. The autocheck toggle is ignored (`settings` is not read)."""
    del settings
    return check_update(installed, manifest, **kwargs)


def scheduled_check(settings, installed, manifest, **kwargs):
    """Scheduled check. Does nothing unless settings['autocheck'] is true."""
    if not isinstance(settings, dict) or settings.get("autocheck") is not True:
        return CheckResult(
            ok=True, update=False, installed=installed, version=None, floor=None,
            reason="autocheck-off", audit=None, display=None, title=None,
            message=None, skipped=True)
    result = check_update(installed, manifest, **kwargs)
    result.skipped = False
    return result


def validate_update_request(body, installed, manifest):
    """Accept only `{"version":"vX.Y.Z"}` for the manifest version.

    Extra keys, a URL, a path, or a shell character -> audit `update-rejected bad-args`.
    Any other version -> `update-rejected not-in-manifest`.
    A version at or below the installed version, or below the floor ->
    `update-rejected downgrade`.
    """
    if not isinstance(manifest, dict):
        raise ManifestError("bad-format", "manifest")
    try:
        target = manifest["version"]
        floor = manifest["min_version_floor"]
    except KeyError:
        raise ManifestError("bad-format", "manifest")
    _version_tuple(target)
    floor_v = _version_tuple(floor)
    installed_v = _version_tuple(installed)

    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return Decision(False, "bad-args", None)
    if isinstance(body, str):
        if len(body) > MAX_REQUEST_CHARS:
            return Decision(False, "bad-args", None)
        matched = _REQUEST_BODY.fullmatch(body)
        if not matched:
            return Decision(False, "bad-args", None)
        version = matched.group(1)
    elif isinstance(body, dict):
        if set(body) != {"version"}:
            return Decision(False, "bad-args", None)
        version = body.get("version")
        if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
            return Decision(False, "bad-args", None)
    else:
        return Decision(False, "bad-args", None)

    if version != target:
        return Decision(False, "not-in-manifest", version)
    got = _version_tuple(version)
    if got <= installed_v or got < floor_v:
        return Decision(False, "downgrade", version)
    return Decision(True, None, version)


def plan_update(installed_station, installed_relay, manifest):
    """Relay first, then the station.

    When compat.relay_min_station <= the installed station, the new relay can
    soak alone for 600s: ["relay", "soak:600", "station"]. Otherwise voice
    pauses across both switches and station_only_allowed is false.
    A station-only update is allowed only when the installed relay is already
    >= compat.station_min_relay (and the soak path applies).
    """
    _validate_release_shape(manifest)
    station_v = _version_tuple(installed_station)
    relay_v = _version_tuple(installed_relay)
    relay_ok = _version_tuple(manifest["compat"]["relay_min_station"]) <= station_v
    station_only = _version_tuple(manifest["compat"]["station_min_relay"]) <= relay_v
    if relay_ok:
        return Plan(["relay", f"soak:{SOAK_S}", "station"], station_only)
    return Plan(["pause-voice", "relay", "station", "resume-voice"], False)


def _redact_detail(detail):
    def last4(match):
        return match.group(0).split(":")[-1][-4:].lower()
    return _LONG_HEX.sub(last4, _SHA_PREFIXED.sub(last4, detail))


def format_audit(ts, actor, side, event, detail="-"):
    """`<ts> <actor> <side> <event> <detail>`. Key material is reduced to the last 4."""
    for name, value in (("ts", ts), ("actor", actor), ("side", side), ("event", event)):
        if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
            raise ValueError(name)
    if not isinstance(detail, str) or detail == "" or "\n" in detail or "\r" in detail:
        raise ValueError("detail")
    detail = _redact_detail(detail)
    if "@" in detail or "://" in detail or _IPV4.search(detail):
        raise ValueError("detail")
    if "-----BEGIN" in detail.upper():
        raise ValueError("detail")
    return f"{ts} {actor} {side} {event} {detail}"


def _fn(ops, name):
    if isinstance(ops, dict):
        if name not in ops:
            raise AttributeError(name)
        return ops[name]
    return getattr(ops, name)


def _call(ops, name, *args):
    return _fn(ops, name)(*args)


def _maybe(ops, name):
    if isinstance(ops, dict):
        return ops.get(name)
    return getattr(ops, name, None)


def _norm_sha(value):
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    if _SHA256.fullmatch(text) is None:
        return None
    return text


class Updater:
    """Stage, switch, and health-check. Ops are injected (no disk or clock of our own).

    ops.stage(component, spec) -> {"slot", "digest"} for the station, or
    {"slot", "digests": {image: "sha256:..."}} for the relay.
    ops.switch(component, slot), ops.health(component), ops.rollback(component) -> version,
    ops.is_busy(), ops.disk_free_mb(), ops.clock(). Optional: sleep, pause_voice,
    resume_voice, current_slot, discard, retain, mark_good.
    """

    def __init__(self, ops=None, **kwargs):
        if ops is not None and kwargs:
            raise TypeError("pass an ops object or keywords, not both")
        self.ops = ops if ops is not None else kwargs
        missing = [name for name in _REQUIRED_OPS if _maybe(self.ops, name) is None]
        if missing:
            raise TypeError("missing ops: " + ",".join(missing))
        self.current = {}
        self.previous = {}
        self.kept = {}
        self._events = []
        self._lines = []
        self._marked_now = set()
        self._rolled = False
        self._reason = None

    def apply(self, installed_station, installed_relay, manifest, steps=None, *, now=None):
        """Run a plan. Relay failure does not start the station. Returns ApplyResult."""
        self._events = []
        self._lines = []
        self._marked_now = set()
        self._rolled = False
        self._reason = None
        _validate_release_shape(manifest)
        _validate_release_time(manifest, _coerce_now(now))
        installed = {"station": installed_station, "relay": installed_relay}
        for version in installed.values():
            _version_tuple(version)
        plan = plan_update(installed_station, installed_relay, manifest)
        steps = self._resolve_steps(plan, steps)
        if steps is None:
            return self._finish(False, "aborted", self._reason)
        if not self._wait_idle():
            return self._finish(False, "deferred", "busy")
        free = _call(self.ops, "disk_free_mb")
        try:
            free = float(free)
        except (TypeError, ValueError):
            free = 0
        if free < DISK_MIN_MB:
            self._emit("-", "update-aborted", "disk")
            return self._finish(False, "aborted", "disk")
        coupled = "pause-voice" in steps
        paused = False
        relay_marked = False
        try:
            for step in steps:
                if step == "pause-voice":
                    _call(self.ops, "pause_voice")
                    paused = True
                elif step == "resume-voice":
                    paused = False
                    _call(self.ops, "resume_voice")
                elif step.startswith("soak:"):
                    self._soak(int(step.split(":", 1)[1]))
                elif step in ("relay", "station"):
                    ok = self._component(step, installed[step], manifest)
                    if ok and step == "relay":
                        relay_marked = True
                    if not ok:
                        if coupled and step == "station" and relay_marked:
                            self._rollback_component("relay", installed_relay)
                        status = "rolled-back" if self._rolled else "aborted"
                        return self._finish(False, status, self._reason)
                else:
                    self._reason = "plan"
                    self._emit("-", "update-aborted", "plan")
                    return self._finish(False, "aborted", "plan")
            return self._finish(True, "applied", None)
        finally:
            if paused:
                resume = _maybe(self.ops, "resume_voice")
                if resume:
                    resume()

    def _resolve_steps(self, plan, steps):
        if steps is None:
            return list(plan)
        wanted = [str(step) for step in steps]
        if wanted == list(plan):
            return wanted
        if wanted == ["station"]:
            if plan.station_only_allowed:
                return wanted
            self._reason = "station-only"
            self._emit("-", "update-aborted", "station-only")
            return None
        self._reason = "plan"
        self._emit("-", "update-aborted", "plan")
        return None

    def _now_dt(self):
        clock = _call(self.ops, "clock")
        if isinstance(clock, datetime):
            if clock.tzinfo is None:
                clock = clock.replace(tzinfo=timezone.utc)
            return clock.astimezone(timezone.utc)
        return datetime.fromtimestamp(float(clock), timezone.utc)

    def _now(self):
        return self._now_dt().timestamp()

    def _emit(self, side, event, detail):
        if detail in (None, ""):
            detail = "-"
        ts = self._now_dt().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = format_audit(ts, "release", side, event, str(detail))
        short = event if detail == "-" else f"{event} {detail}"
        self._events.append(short)
        self._lines.append(line)

    def _sleep_for(self, seconds):
        sleeper = _fn(self.ops, "sleep")
        end = self._now() + seconds
        while self._now() + 1e-6 < end:
            before = self._now()
            sleeper(end - before)
            if self._now() <= before:
                raise RuntimeError("clock did not advance")

    def _wait_idle(self):
        if not _call(self.ops, "is_busy"):
            return True
        if _maybe(self.ops, "sleep") is None:
            self._emit("-", "update-deferred", "busy")
            return False
        start = self._now()
        for _ in range(10000):
            if not _call(self.ops, "is_busy"):
                return True
            elapsed = self._now() - start
            if elapsed >= BUSY_WAIT_S:
                self._emit("-", "update-deferred", "busy")
                return False
            before = self._now()
            _call(self.ops, "sleep", min(1.0, BUSY_WAIT_S - elapsed))
            if self._now() <= before:
                self._emit("-", "update-deferred", "busy")
                return False
        self._emit("-", "update-deferred", "busy")
        return False

    def _soak(self, seconds):
        if seconds < 0 or seconds > 24 * 3600:
            raise ManifestError("bad-format", "soak")
        self._sleep_for(seconds)

    def _ensure_current(self, component):
        if component not in self.current:
            current = _maybe(self.ops, "current_slot")
            self.current[component] = current(component) if current else None

    def _digests_ok(self, component, manifest, staged):
        if not isinstance(staged, dict):
            return False
        if component == "station":
            got = staged.get("digest")
            if got is None and isinstance(staged.get("digests"), dict):
                digests = staged["digests"]
                got = digests.get("tarball", digests.get("station"))
            return _norm_sha(got) == manifest["components"]["station"]["sha256"]
        expected = manifest["components"]["relay"]["images"]
        got = staged.get("digests")
        if not isinstance(got, dict) or set(got) != set(expected):
            return False
        for name, want in expected.items():
            hashed = _norm_sha(got.get(name))
            if hashed is None or "sha256:" + hashed != want:
                return False
        return True

    def _gates(self, component):
        raw = _call(self.ops, "health", component)
        if isinstance(raw, bool):
            return [("ready", raw)]
        if isinstance(raw, dict) and isinstance(raw.get("ok"), bool):
            return [(_gate_name(raw.get("gate")), raw["ok"])]
        if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], bool):
            return [(_gate_name(raw[0]), raw[1])]
        if isinstance(raw, list) and raw:
            gates = []
            for item in raw:
                if not isinstance(item, dict) or not isinstance(item.get("ok"), bool):
                    return [("ready", False)]
                gates.append((_gate_name(item.get("gate")), item["ok"]))
            return gates
        return [("ready", False)]

    def _component(self, component, installed_version, manifest):
        target = manifest["version"]
        self._emit(component, "update-start", f"{installed_version} {target}")
        self._ensure_current(component)
        staged = _call(self.ops, "stage", component, manifest["components"][component])
        if not self._digests_ok(component, manifest, staged):
            self._reason = "digest"
            self._emit(component, "update-aborted", "digest")
            return False
        slot = staged.get("slot")
        if not isinstance(slot, str) or slot == "" or "\n" in slot or "\r" in slot:
            self._reason = "stage"
            self._emit(component, "update-aborted", "stage")
            return False
        self._emit(component, "update-stage-ok", "-")
        _call(self.ops, "switch", component, slot)
        self._emit(component, "update-switch", "-")
        for gate, ok in self._gates(component):
            if ok:
                self._emit(component, "update-health-ok", gate)
            else:
                self._reason = "health"
                self._emit(component, "update-health-fail", gate)
                self._rollback_component(component, installed_version)
                return False
        self._mark_good(component, slot)
        return True

    def _mark_good(self, component, slot):
        older = self.previous.get(component)
        prev = self.current.get(component)
        self.previous[component] = prev
        self.current[component] = slot
        if older and older not in (prev, slot):
            discard = _maybe(self.ops, "discard")
            if discard:
                discard(component, older)
        kept = [slot] if prev is None else [slot, prev]
        self.kept[component] = kept
        self._marked_now.add(component)
        retain = _maybe(self.ops, "retain")
        if retain:
            retain(component, list(kept))
        mark = _maybe(self.ops, "mark_good")
        if mark:
            mark(component, slot)

    def _rollback_component(self, component, installed_version):
        to = _call(self.ops, "rollback", component)
        if not isinstance(to, str) or _VERSION.fullmatch(to) is None:
            to = installed_version
        self._emit(component, "update-rolled-back", to)
        self._rolled = True
        if component not in self._marked_now:
            return
        new = self.current.get(component)
        prev = self.previous.get(component)
        self.current[component] = prev
        self.previous[component] = None
        self.kept[component] = [prev] if prev else []
        self._marked_now.discard(component)
        discard = _maybe(self.ops, "discard")
        if discard and new:
            discard(component, new)
        retain = _maybe(self.ops, "retain")
        if retain:
            retain(component, list(self.kept[component]))

    def _finish(self, ok, status, reason):
        return ApplyResult(
            ok=ok, status=status, reason=reason, events=list(self._events),
            lines=list(self._lines),
            kept={key: list(value) for key, value in self.kept.items()},
            current=dict(self.current))


def _gate_name(value):
    if isinstance(value, str) and _TOKEN.fullmatch(value):
        return value
    return "ready"


def load_settings(path):
    """Return {"autocheck": False} when the file is missing. Automatic checks default off."""
    if not os.path.exists(path):
        return {"autocheck": False}
    with open(path, "r", encoding="utf-8") as handle:
        try:
            doc = json.load(handle)
        except json.JSONDecodeError as err:
            raise ValueError("settings") from err
    if not isinstance(doc, dict):
        raise ValueError("settings")
    if "autocheck" not in doc:
        return {"autocheck": False}
    if not isinstance(doc["autocheck"], bool):
        raise ValueError("autocheck")
    return {"autocheck": doc["autocheck"]}


def set_autocheck(path, on):
    """Write the toggle. Returns `update-autocheck on` or `update-autocheck off`."""
    if not isinstance(on, bool):
        raise ValueError("autocheck")
    doc = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            doc = loaded
    doc["autocheck"] = on
    _atomic_json(path, doc)
    return "update-autocheck on" if on else "update-autocheck off"


def assert_tag(ref):
    """Tags only: vX.Y.Z. Branch names, raw commits and pre-release tags are refused."""
    if not isinstance(ref, str) or _VERSION.fullmatch(ref) is None:
        raise ManifestError("bad-format", "version")
    return ref


def assert_tag_on_main(ref, on_main):
    """on_main(ref) must be True when the tag commit is an ancestor of main. The callback is injected."""
    assert_tag(ref)
    if not callable(on_main) or on_main(ref) is not True:
        raise ManifestError("not-on-main", "version")
    return ref


def apply_decision(tag, manifest, file_sha, health_ok):
    """keep only when the tag is valid, the tarball sha256 matches, and the health check passed."""
    assert_tag(tag)
    try:
        want = manifest["components"]["station"]["sha256"]
    except (KeyError, TypeError):
        return "rollback"
    if _norm_sha(file_sha) != _norm_sha(want):
        return "rollback"
    if health_ok is not True:
        return "rollback"
    return "keep"
