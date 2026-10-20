#!/usr/bin/env python3
"""grok-station wake listener + voice client (proof of concept).

Captures the USB headset mic at 48 kHz via `arecord`, decimates to 16 kHz for openWakeWord (ONNX).
On a wake it plays a chime, shows "listening", and (if GROK_RELAY_URL is set) opens one session to the
voice relay: mic audio goes up as PCM16 24 kHz, reply audio comes back and plays on the headset via
`aplay`, and the display follows listening -> thinking -> speaking -> idle through localhost POST /state.
A tool card (`{"type":"card",...}`) is forwarded on that same POST. Timers post their full list
(`{"timers":[{id,label,ends_at_epoch,ringing}]}`) when the set changes — set, cancel, ring, clear —
and the display counts down locally. Logs name the event type only, never a temperature, label or duration.
The wake detector is paused during a session so the reply can't re-trigger it.
`grok_wake.py --test file.wav` scores a 16 kHz mono WAV offline and prints the peak score.
`grok_wake.py --ask file.wav [out.wav]` sends a 24 kHz mono WAV question through the relay path.
A wss:// GROK_RELAY_URL uses mutual TLS: the server must chain to GROK_RELAY_CA (only that CA is trusted) and the
client presents GROK_RELAY_CERT / GROK_RELAY_KEY. Each defaults to a systemd credential (relay-ca.crt,
relay-client.crt, relay-client.key in $CREDENTIALS_DIRECTORY; see systemd/grok-station-wake-tls.conf).
Personas: the relay may switch the session to a teammate ({"type":"persona","slug","name"}); the display then shows
that name and portrait. At startup (and hourly) the client caches GET /personas and each GET /portrait/<slug> into
GROK_STATION_PORTRAITS (default /opt/grok-station/portraits, never in git); the display reads personas.json there to
keep /show from posting as any persona.
Wake words: WAKE_CONFIG (default /opt/grok-station/wake/wakewords.json, repo copy wake/wakewords.json) lists custom
openWakeWord models (models_dir/<name>.onnx), each with a persona slug, threshold and patience (consecutive 80 ms
frames at or above the threshold). A wake opens the session as that persona (relay /ws?persona=<slug>). The optional
"fallback" entry loads a built-in openWakeWord model (hey_jarvis). Without the file: single WAKE_MODEL as before.
`grok_wake.py --test file.wav` reports which word/persona the clip triggers (never opens a session);
`grok_wake.py --bench [seconds]` times the loaded models on noise.
"""
import base64, shlex, json, os, queue, re, subprocess, sys, threading, time, urllib.parse, urllib.request, wave
import numpy as np

E = os.environ.get
MODEL = E("WAKE_MODEL", "hey_jarvis")          # pre-trained placeholder until a custom "Hey Grok" model exists
THRESH = float(E("WAKE_THRESHOLD", "0.5"))
COOLDOWN = float(E("WAKE_COOLDOWN", "2"))
def default_gain(device):
    return "2" if "emeet" in device.lower() else "4"


DEVICE = E("WAKE_DEVICE_NAME", "EMEET")        # matched against `arecord -l` / `aplay -l` card names (capture + playback)
STATE_URL = E("WAKE_STATE_URL", "http://127.0.0.1:8787/state")
CHIME = E("WAKE_CHIME", "/opt/grok-station/wake/chime.wav")
LEVEL_EVERY = float(E("WAKE_LEVEL_EVERY", "30"))
GAIN = float(E("WAKE_GAIN", default_gain(DEVICE)))  # software gain: 2 for the EMEET, 4 for the quieter Razer
CAP_RATE = int(E("WAKE_CAPTURE_RATE", "16000"))  # the device's native capture rate: 16000 (EMEET M0 Plus mic) or 48000
                                                 # (e.g. the Razer USB card, whose 16 kHz mode is muffled); 48k is decimated
DECIM = CAP_RATE // 16000
_K = np.sinc(np.arange(-32, 33) / DECIM) * np.hamming(65); _K /= _K.sum()  # low-pass FIR for decimation
CHUNK = 1280                                     # 80 ms at 16 kHz, what openWakeWord expects
RELAY_URL, RELAY_TOKEN = E("GROK_RELAY_URL", ""), E("GROK_RELAY_TOKEN", "")
VOICE_GAIN = float(E("VOICE_GAIN", str(GAIN)))   # gain on the audio sent to Grok
VOICE_SKIP = float(E("VOICE_SKIP_MS", "300")) / 1000  # drop this much post-wake audio (tail of the wake word)
PORTRAITS = E("GROK_STATION_PORTRAITS", "/opt/grok-station/portraits")
D24 = CAP_RATE // 24000                          # 48k -> 24k decimation; 16k capture is upsampled instead (up24)
_K24 = np.sinc(np.arange(-24, 25) / max(1, D24)) * np.hamming(49); _K24 /= _K24.sum()


def up24(x, prev):
    """16 kHz chunk -> 24 kHz (x1.5, linear; speech content is below 8 kHz). prev is the previous chunk's last sample."""
    xs = np.concatenate([[prev], x])
    return np.interp(np.arange(len(x) * 3 // 2) / 1.5, np.arange(len(xs)), xs)


def relay_ssl():
    """TLS context for a wss:// relay: private-CA pin plus client certificate. None for plain ws://."""
    if not RELAY_URL.startswith("wss://"):
        return None
    import ssl
    cred = lambda env, name: E(env) or os.path.join(E("CREDENTIALS_DIRECTORY", "."), name)
    ctx = ssl.create_default_context(cafile=cred("GROK_RELAY_CA", "relay-ca.crt"))  # trusts only this CA
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(cred("GROK_RELAY_CERT", "relay-client.crt"), cred("GROK_RELAY_KEY", "relay-client.key"))
    return ctx


def relay_get(path):
    """GET a relay path (https with the same mTLS + token as the voice session). Returns bytes or None."""
    base = re.sub(r"^ws", "http", RELAY_URL).rsplit("/ws", 1)[0]
    try:
        req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {RELAY_TOKEN}"})
        with urllib.request.urlopen(req, timeout=8, context=relay_ssl()) as r:
            return r.read(2 << 20)
    except Exception as e:
        print(f"relay get {path.split('/')[1]} failed: {type(e).__name__}", flush=True)
        return None


def sync_personas():
    """Cache the persona list and portraits for the display. Files are written atomically; bad data is ignored."""
    raw = relay_get("/personas") if RELAY_URL else None
    try:
        items = json.loads(raw)["personas"] if raw else None
    except (ValueError, KeyError, TypeError):
        items = None
    if items is None:
        return
    os.makedirs(PORTRAITS, exist_ok=True)
    got = 0
    for p in items:
        slug = str(p.get("slug", ""))
        if not re.fullmatch(r"[a-z]{2,16}", slug) or slug == "grok":
            continue
        img = relay_get(f"/portrait/{slug}")
        if img and (img[:3] == b"\xff\xd8\xff" or img[:8] == b"\x89PNG\r\n\x1a\n"
                    or (len(img) >= 12 and img[:4] == b"RIFF" and img[8:12] == b"WEBP")):
            ext = "png" if img[:4] == b"\x89PNG" else "webp" if img[:4] == b"RIFF" else "jpg"
            for old in ("png", "jpg", "webp"):
                if old != ext and os.path.exists(os.path.join(PORTRAITS, f"{slug}.{old}")):
                    os.remove(os.path.join(PORTRAITS, f"{slug}.{old}"))
            tmp = os.path.join(PORTRAITS, f".{slug}.tmp")
            with open(tmp, "wb") as f:
                f.write(img)
            os.replace(tmp, os.path.join(PORTRAITS, f"{slug}.{ext}"))
            got += 1
    tmp = os.path.join(PORTRAITS, ".personas.tmp")
    with open(tmp, "w") as f:
        json.dump({"personas": [{"slug": str(p.get("slug", ""))[:16], "name": str(p.get("name", ""))[:20],
                                 "aliases": [str(a)[:30] for a in p.get("aliases", [])][:5]} for p in items]}, f)
    os.replace(tmp, os.path.join(PORTRAITS, "personas.json"))
    print(f"personas synced: {len(items)} names, {got} portraits", flush=True)


def persona_loop():
    while True:
        for f in (sync_personas, sync_wakewords):
            try:
                f()
            except Exception as e:
                print(f"{f.__name__} failed: {type(e).__name__}", flush=True)
        fast = time.time() < ENROLL_STATE.get("fast_until", 0)  # after an enrollment: poll for the new model
        SYNC_NOW.wait(300 if fast else 3600)
        SYNC_NOW.clear()


# ---------------------------------------------------------------- enroll mode (docs/enroll.md)
ENROLL_DIR = E("WAKE_ENROLL_DIR", "/opt/grok-station/wake/enroll")      # local copy of recorded clips
ENROLLED_DIR = E("WAKE_ENROLLED_DIR", "/opt/grok-station/wake/models/enrolled")  # models trained from real clips
CONTROL_PORT = int(E("WAKE_CONTROL_PORT", "8789"))                     # 127.0.0.1 only
ENROLL_Q, SYNC_NOW, RELOAD = queue.Queue(maxsize=1), threading.Event(), threading.Event()
ENROLL_STATE = {"busy": False, "fast_until": 0, "last": None}
# Admin page (admin/grok_admin.py): until setup is done the station connects no one. The admin service writes
# SETUP_FILE ({"mode": "setup"|"reset"|"hotspot", ...}) while setup is pending and removes it when a password is set.
SETUP_FILE = E("GROK_SETUP_FILE", "/run/grok-station-admin/setup.json")
OVERRIDES = E("WAKE_OVERRIDES", "/var/lib/grok-station/admin/wake-overrides.json")  # {name: {enabled, threshold}}
SETUP_LINES = E("WAKE_SETUP_LINES", "/opt/grok-station/wake/lines")  # setup.wav, reset.wav, hotspot.wav (optional)
LIVE = {"dbfs": None, "at": 0.0, "session": False, "play": None}
# Station timers (relay tool set_timer): they live here so they ring after the conversation ends and survive restarts.
TIMERS_FILE = E("WAKE_TIMERS", "/opt/grok-station/wake/timers.json")
TIMERS, TIMERS_LOCK = {}, threading.Lock()
RING_HOLD = 60.0  # ringing chip stays up this long after the chimes and the spoken notice
# T1 voice notes (relay t1.py): physical taps on the station (touchscreen or the speakerphone's call button) for
# "recent tap" and tap-to-confirm. A stale guest-mode flag in t1.json is honored for this one read, then removed.
# Taps are read here from evdev (grok is in input).
T1_FILE = E("WAKE_T1_FILE", "/var/lib/grok-station/admin/t1.json")
TAP_KEYS = {int(k) for k in E("WAKE_TAP_KEYS", "330,169,164,445").split(",") if k.strip().isdigit()}  # BTN_TOUCH, KEY_PHONE, KEY_PLAYPAUSE, KEY_PICKUP_PHONE
LAST_TAP = {"at": 0.0}
CURRENT = {"session": None}


def t1_query():
    """Station status for the relay. An explicit guest flag is returned, then deleted from the file.

    Missing file, or a file that no longer has the key, is guest off. A rewrite failure still returns the value read.
    """
    guest = False
    try:
        with open(T1_FILE) as f:
            doc = json.load(f)
        if isinstance(doc, dict) and "guest" in doc:
            guest = doc.get("guest") is True
            doc.pop("guest", None)
            tmp = f"{T1_FILE}.{os.getpid()}.tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(doc, f)
                os.replace(tmp, T1_FILE)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except (OSError, ValueError, AttributeError):
        guest = False
    return urllib.parse.quote(json.dumps({"guest": guest, "last_tap": int(LAST_TAP["at"])}, separators=(",", ":")))


def on_tap(code):
    LAST_TAP["at"] = time.time()
    print(f"tap key={code}", flush=True)
    ses = CURRENT["session"]
    if ses is not None and getattr(ses, "t1_card", None) and getattr(ses, "ws", None) is not None:
        try:
            ses.ws.send(json.dumps({"type": "t1_tap", "request_id": ses.t1_card["request_id"]}))
            print("t1 tap sent", flush=True)
        except Exception as e:
            print(f"t1 tap send failed: {type(e).__name__}", flush=True)


def tap_loop():
    """Read key presses from input devices that have one of TAP_KEYS (evdev, 24-byte input_event on 64-bit)."""
    import glob, select, struct
    fmt = "llHHi"
    size = struct.calcsize(fmt)
    fds = {}
    while True:
        for path in glob.glob("/dev/input/event*"):
            if path in fds.values():
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                fds[fd] = path
            except OSError:
                pass
        try:
            r, _, _ = select.select(list(fds), [], [], 5)
        except (OSError, ValueError):
            r = []
        for fd in r:
            try:
                data = os.read(fd, size * 64)
            except OSError:
                os.close(fd)
                fds.pop(fd, None)
                continue
            for i in range(0, len(data) - size + 1, size):
                _, _, typ, code, val = struct.unpack(fmt, data[i:i + size])
                if typ == 1 and val == 1 and code in TAP_KEYS:  # EV_KEY press
                    on_tap(code)


def timers_load():
    try:
        d = json.load(open(TIMERS_FILE))
        with TIMERS_LOCK:
            TIMERS.update({k: v for k, v in d.items() if re.fullmatch(r"[a-z0-9]{4,12}", k) and isinstance(v, dict)
                           and isinstance(v.get("due"), (int, float))})
    except (OSError, ValueError, AttributeError):
        pass


def timers_save():
    try:
        tmp = TIMERS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(TIMERS, f)
        os.replace(tmp, TIMERS_FILE)
    except OSError as e:
        print(f"timers save failed: {type(e).__name__}", flush=True)


def _clean_timer_label(s):
    return " ".join(re.sub(r"[^\w '\-]", " ", str(s or "")).split())[:24] or "timer"


def timer_rows():
    """Full timer list for the display. Ringing chips sort first, then soonest deadline."""
    with TIMERS_LOCK:
        rows = [{"id": k, "label": v.get("label") or "timer", "ends_at_epoch": float(v["due"]),
                 "ringing": bool(v.get("ringing"))} for k, v in TIMERS.items() if isinstance(v.get("due"), (int, float))]
    rows.sort(key=lambda r: (not r["ringing"], r["ends_at_epoch"]))
    return rows


def post_timers():
    """Push the whole set. The display counts down from ends_at; this is not called every second."""
    post_state(timers=timer_rows())


def timer_event(ev):
    """timer_set / timer_cancel from the relay. Labels are cleaned again here."""
    tid = str(ev.get("id", ""))
    if not re.fullmatch(r"[a-z0-9]{4,12}", tid):
        return
    changed = False
    with TIMERS_LOCK:
        if ev.get("type") == "timer_set":
            sec = ev.get("seconds")
            if isinstance(sec, int) and 1 <= sec <= 86400 and len(TIMERS) < 10:
                TIMERS[tid] = {"label": _clean_timer_label(ev.get("label")), "due": time.time() + sec}
                changed = True
                print("timer set", flush=True)
        elif TIMERS.pop(tid, None):
            changed = True
            print("timer cancelled", flush=True)
        if changed:
            timers_save()
    if changed:
        post_timers()


def timers_query():
    with TIMERS_LOCK:
        now = time.time()
        items = [{"id": k, "label": v["label"], "left": int(v["due"] - now)}
                 for k, v in TIMERS.items() if v["due"] > now and not v.get("ringing")]
    return urllib.parse.quote(json.dumps(items, separators=(",", ":"))) if items else ""


def _expire_rings(now):
    """Drop chips whose ringing hold has ended. A ring that never recorded its deadline gets one now."""
    removed = dirty = False
    with TIMERS_LOCK:
        for k, v in list(TIMERS.items()):
            if not v.get("ringing"):
                continue
            if not v.get("ring_until"):
                v["ring_until"] = now + RING_HOLD
                dirty = True
            elif now >= v["ring_until"]:
                TIMERS.pop(k, None)
                removed = dirty = True
        if dirty:
            timers_save()
    if removed:
        print("timer clear", flush=True)
        post_timers()


def _notice_and_chime(label):
    post_state("speaking", who="grok", text=("Your timer is done." if label == "timer" else f"Your {label} timer is done."), done=True)
    dev = LIVE.get("play")
    for _ in range(3):
        if dev:
            subprocess.run(["aplay", "-q", "-D", dev, CHIME], stderr=subprocess.DEVNULL, timeout=10)
        time.sleep(0.4)


def _ring_one(k, v):
    """Mark the chip ringing, chime, then keep it up for RING_HOLD. Late by over an hour: drop quietly."""
    late = time.time() - v["due"]
    with TIMERS_LOCK:
        cur = TIMERS.get(k)
        if not cur or cur.get("ringing"):
            return
        if late > 3600:
            TIMERS.pop(k, None)
            timers_save()
            dropped = True
        else:
            cur["ringing"] = True
            timers_save()
            dropped = False
    if dropped:
        print("timer dropped", flush=True)
        post_timers()
        return
    print("timer ringing", flush=True)
    post_timers()
    try:
        _notice_and_chime(v.get("label") or "timer")
    finally:
        with TIMERS_LOCK:
            cur = TIMERS.get(k)
            if cur and cur.get("ringing"):
                cur["ring_until"] = time.time() + RING_HOLD
                timers_save()


def timers_tick():
    """One pass: clear finished rings, start due ones when the speaker is free."""
    _expire_rings(time.time())
    with TIMERS_LOCK:
        due = [(k, dict(v)) for k, v in TIMERS.items() if not v.get("ringing") and v["due"] <= time.time()]
    if not due or LIVE["session"] or not LIVE["play"]:
        return
    for k, v in due:
        _ring_one(k, v)


def timers_loop():
    """Ring due timers: three chimes and a notice. Waits for a conversation to end so it doesn't talk over it."""
    while True:
        time.sleep(0.5)
        timers_tick()


_CARD_FIELDS = {"weather": ("icon", "now_temp", "units", "conditions", "today", "tomorrow"),
                "time": ("zone_label", "hhmm", "day", "offset"),
                "convert": ("value", "from_unit", "result", "to_unit")}


def take_card(ev):
    """Display card from a relay event, or None. Unknown keys (place, coordinates) are not copied. Capped at 600 bytes."""
    if not isinstance(ev, dict):
        return None
    kind = ev.get("kind")
    if kind not in _CARD_FIELDS:
        return None
    card = {"type": "card", "kind": kind}
    for k in _CARD_FIELDS[kind]:
        if k in ev:
            card[k] = ev[k]
    if kind == "weather":
        for name in ("today", "tomorrow"):
            hl = card.get(name)
            if not isinstance(hl, dict):
                card.pop(name, None)
                continue
            slim = {}
            for hk in ("high", "low"):
                v = hl.get(hk)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    slim[hk] = v
            if len(slim) == 2:
                card[name] = slim
            else:
                card.pop(name, None)
    try:
        if len(json.dumps(card, separators=(",", ":"))) > 600:
            return None
    except (TypeError, ValueError):
        return None
    return card


def setup_mode():
    try:
        with open(SETUP_FILE) as f:
            m = json.load(f).get("mode")
        return m if m in ("setup", "reset", "hotspot") else None
    except (OSError, ValueError, AttributeError):
        return None
PHRASE_RE = re.compile(r"^[a-z]+(?:_[a-z]+){1,3}$")  # 2–4 words; every shipped hey_<name> still matches
PHRASE_MAX = 32


def phrase_ok(name):
    return isinstance(name, str) and len(name) <= PHRASE_MAX and PHRASE_RE.fullmatch(name) is not None


def wake_name_ok(name):
    """phrase_ok, or hey_<bot id> so a slug with a digit or a hyphen can still be a wake entry."""
    if phrase_ok(name):
        return True
    return (isinstance(name, str) and name.startswith("hey_")
            and re.fullmatch(r"[a-z][a-z0-9-]{1,23}", name[4:]) is not None and name[4:] != "grok")


def persona_ok(slug):
    """Grok, a teammate slug, or a bot slug (letters, digits, dashes)."""
    return isinstance(slug, str) and (slug == "grok" or re.fullmatch(r"[a-z][a-z0-9-]{1,23}", slug) is not None)


def relay_persona(slug):
    """The training inbox wants ^[a-z]{2,16}$. The listener still opens the real slug from overrides."""
    return slug if isinstance(slug, str) and re.fullmatch(r"[a-z]{2,16}", slug) else "grok"


def enroll_stage(stage, message, phrase=None, session=None):
    """Record where this enrollment is, on the status the admin page polls. Ignore a stale session."""
    last = ENROLL_STATE.get("last")
    if not isinstance(last, dict):
        return
    if phrase is not None and last.get("phrase") != phrase:
        return
    if session is not None and last.get("session") != session:
        return
    last["stage"] = stage
    last["message"] = message
CLIP_S, LEAD_S, RETRY_S = 2.4, 0.9, 1.5  # recording window per clip, pause before the chime (the screen's get-ready
                                         # ring fills over it), too loud / didn't hear it hold before the retry
ENROLL_COUNT, TOPUP_COUNT = 25, 25       # default clips per session (allowed 20–30); top-up uses the same default
SESSION_OPEN = threading.Event()         # a voice session is on screen: enrollment result notices wait for it


def relay_post(path, body, ctype="application/octet-stream"):
    base = re.sub(r"^ws", "http", RELAY_URL).rsplit("/ws", 1)[0]
    req = urllib.request.Request(base + path, body, {"Authorization": f"Bearer {RELAY_TOKEN}", "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=15, context=relay_ssl()) as r:
        return r.status


def enroll_phrases():
    """phrase -> persona for configured words, the config's "enroll" list, and admin-page mappings.

    A custom phrase lives in the overrides file as {phrase: {persona}} — the same file the admin page writes.
    """
    try:
        with open(WAKE_CONFIG) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    out = {}
    for w in cfg.get("words", []) + cfg.get("enroll", []):
        n, ps = str(w.get("name", "")), str(w.get("persona", "grok"))
        if wake_name_ok(n) and persona_ok(ps):
            out[n] = ps
    try:
        with open(OVERRIDES) as f:
            ov = json.load(f)
    except (OSError, ValueError):
        ov = {}
    if isinstance(ov, dict):
        for name, o in ov.items():
            if isinstance(o, dict) and wake_name_ok(str(name)) and persona_ok(str(o.get("persona") or "")):
                out[str(name)] = str(o["persona"])
    return out


_PERSONA_SYNC = {"on": False}
_PERSONA_SYNC_LOCK = threading.Lock()
CONTROL_HTTPD = None


def begin_persona_sync():
    """Run sync_personas in a thread. False when one is already running."""
    with _PERSONA_SYNC_LOCK:
        if _PERSONA_SYNC["on"]:
            return False
        _PERSONA_SYNC["on"] = True

    def run():
        try:
            sync_personas()
        except Exception as e:
            print(f"sync_personas failed: {type(e).__name__}", flush=True)
        finally:
            with _PERSONA_SYNC_LOCK:
                _PERSONA_SYNC["on"] = False

    threading.Thread(target=run, daemon=True).start()
    return True


def parse_enroll(j):
    """The body of POST /enroll. None when the phrase is not one this station can record.

    persona on the body is only used for a phrase that isn't in the config or the overrides yet.
    """
    if not isinstance(j, dict):
        return None
    try:
        phrase, count = str(j.get("phrase", "")), int(j.get("count", ENROLL_COUNT))
    except (TypeError, ValueError):
        return None
    allowed = enroll_phrases()
    persona = allowed.get(phrase)
    if persona is None and wake_name_ok(phrase) and persona_ok(str(j.get("persona") or "")):
        persona = str(j["persona"])
    if persona is None or not wake_name_ok(phrase):
        return None
    if not 20 <= count <= 30:
        return {"error": "count"}
    return {"phrase": phrase, "persona": persona, "count": count, "topup": bool(j.get("topup"))}


def control_server():
    """127.0.0.1:CONTROL_PORT POST /enroll {"phrase","count"}: queue one enrollment (the display's LAN /enroll and
    `grok_wake.py --enroll` both land here). POST /personas/sync re-runs persona and portrait sync in a thread.
    Never reachable from the LAN."""
    global CONTROL_HTTPD
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def _r(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def do_GET(self):
            if self.path != "/status":
                return self._r(404, {"error": "not found"})
            return self._r(200, {"level_dbfs": LIVE["dbfs"], "level_at": LIVE["at"], "session": LIVE["session"],
                                 "enrolling": ENROLL_STATE["busy"], "last_enroll": ENROLL_STATE["last"],
                                 "words": {k: {"persona": w["persona"], "threshold": w["threshold"]} for k, w in WORDS.items()},
                                 "setup": setup_mode()})

        def do_POST(self):
            if self.path == "/personas/sync":
                try:
                    n = min(int(self.headers.get("Content-Length", 0) or 0), 1024)
                except ValueError:
                    n = 0
                if n:
                    self.rfile.read(n)
                if begin_persona_sync():
                    return self._r(202, {"started": True})
                return self._r(202, {"running": True})
            if self.path == "/reload":
                RELOAD.set()
                return self._r(202, {"ok": True})
            if self.path == "/test-sound":
                if LIVE["session"] or ENROLL_STATE["busy"] or not LIVE["play"]:
                    return self._r(409, {"error": "busy"})
                subprocess.Popen(["aplay", "-q", "-D", LIVE["play"], CHIME], stderr=subprocess.DEVNULL)
                return self._r(202, {"ok": True})
            if self.path != "/enroll":
                return self._r(404, {"error": "not found"})
            try:
                j = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 1024)) or b"{}")
            except Exception:
                return self._r(400, {"error": "bad json"})
            req = parse_enroll(j)
            if req is None:
                return self._r(400, {"error": "unknown phrase", "phrases": sorted(enroll_phrases())})
            if req.get("error") == "count":
                return self._r(400, {"error": "count must be 20-30"})
            if not RELAY_URL:
                return self._r(503, {"error": "no relay configured"})
            if setup_mode():
                return self._r(423, {"error": "setup needed"})
            if ENROLL_STATE["busy"]:
                return self._r(409, {"error": "enrollment in progress"})
            try:
                ENROLL_Q.put_nowait(req)
            except queue.Full:
                return self._r(409, {"error": "an enrollment is already queued"})
            print(f"enroll queued: {req['phrase']} x{req['count']}{' (top-up)' if req.get('topup') else ''}", flush=True)
            return self._r(202, {"ok": True, "phrase": req["phrase"], "persona": req["persona"], "count": req["count"]})

        def log_message(self, *a):
            pass

    CONTROL_HTTPD = ThreadingHTTPServer(("127.0.0.1", CONTROL_PORT), H)
    CONTROL_HTTPD.serve_forever()


def trim_clip(pcm):
    """Speech region by 20 ms frame energy (above max(-50 dBFS, peak - 30 dB)), padded 0.25 s. None if no speech."""
    f = 320
    n = len(pcm) // f
    e = 20 * np.log10(np.sqrt(np.mean(pcm[:n * f].astype(np.float32).reshape(n, f) ** 2, 1)) / 32768 + 1e-9)
    on = np.where(e > max(-50.0, e.max() - 30))[0]
    if not len(on) or e.max() < -45:
        return None
    a, b = max(0, on[0] * f - 4000), min(len(pcm), (on[-1] + 1) * f + 4000)
    return pcm[a:b] if b - a >= 8000 else None


def is_clipped(raw):
    """True if more than 0.1% of the pre-gain samples sit at full scale."""
    return bool(np.mean(np.abs(raw.astype(np.int32)) >= 32000) > 0.001)


def last_session(phrase):
    """Newest earlier local session of phrase (for a top-up), or None."""
    try:
        ses = sorted(x for x in os.listdir(os.path.join(ENROLL_DIR, phrase)) if re.fullmatch(r"\d{8}-\d{6}", x))
    except OSError:
        return None
    return ses[-1] if ses else None


def enroll(req, read16, play):
    """Record req["count"] clips with the enrollment screens (prompt, get ready, recording, too loud / didn't hear it,
    saved), upload each, mark the session ready, then follow its training to a pass/fail notice."""
    phrase, persona, n = req["phrase"], req["persona"], req["count"]
    words = phrase.replace("_", " ").title()
    say = f"“{words}”"
    prior = last_session(phrase) if req.get("topup") else None
    ses = time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(ENROLL_DIR, phrase, ses)
    os.makedirs(d, exist_ok=True)
    ENROLL_STATE["busy"] = True
    show = lambda i, stage, extra="": post_state("enroll", text=say, who=persona, heard=f"{i}/{n}|{stage}|{extra}")
    ENROLL_STATE["last"] = {"phrase": phrase, "session": ses, "count": n, "started": time.time(),
                             "persona": persona, "stage": "recording", "message": "Say it on the station."}
    print(f"enroll start {phrase} x{n} session {ses}{f' (top-up of {prior})' if prior else ''}", flush=True)
    sent, i, tries = 0, 1, 0
    try:
        show(1, "prompt")
        for _ in range(int(3 / 0.08)):
            read16()
        while i <= n:
            show(i, "ready")
            for _ in range(int(LEAD_S / 0.08)):
                read16()
            subprocess.Popen(["aplay", "-q", "-D", play, CHIME], stderr=subprocess.DEVNULL)
            for _ in range(4):  # skip the chime itself
                read16()
            show(i, "recording")
            raw = np.concatenate([read16(raw=True) for _ in range(int(CLIP_S / 0.08))])
            clipped = is_clipped(raw)  # judged on the mic signal before software gain
            pcm = np.clip(raw.astype(np.float32) * GAIN, -32768, 32767).astype("<i2")
            clip = trim_clip(pcm)
            if clip is None or clipped:
                tries += 1
                print(f"enroll clip {i} rejected ({'clipped' if clipped else 'quiet'})", flush=True)
                show(i, "loud" if clipped else "quiet")
                for _ in range(int(RETRY_S / 0.08)):
                    read16()
                if tries >= 3 * n:
                    break
                continue
            path = os.path.join(d, f"{i - 1:02d}.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(clip.astype("<i2").tobytes())
            try:
                enroll_stage("uploading", "Sending the clips.", phrase, ses)
                with open(path, "rb") as f:
                    relay_post(f"/enroll/{phrase}/{ses}/{i - 1:02d}.wav", f.read(), "audio/wav")
                sent += 1
            except Exception as e:
                print(f"enroll upload {i} failed: {type(e).__name__}", flush=True)
            if i < n:
                show(i, "saved")
                for _ in range(int(0.6 / 0.08)):
                    read16()
            i += 1
        ok = False
        if sent >= (5 if prior else 10):
            try:
                body = {"persona": relay_persona(persona), "count": sent, **({"prior": prior} if prior else {})}
                relay_post(f"/enroll/{phrase}/{ses}/done", json.dumps(body).encode(), "application/json")
                ok = True
            except Exception as e:
                print(f"enroll done failed: {type(e).__name__}", flush=True)
        print(f"enroll end {phrase}: {i - 1} recorded, {sent} uploaded, {'ready' if ok else 'NOT submitted'}", flush=True)
        if ok:
            ENROLL_STATE["fast_until"] = time.time() + 3 * 3600
            SYNC_NOW.set()
            enroll_stage("training", "Training.", phrase, ses)
            post_state("enroll", text=say, who=persona, heard=f"{sent}/{sent}|uploaded|")
            threading.Thread(target=follow_training, args=(phrase, ses, say, persona), daemon=True).start()
        else:
            enroll_stage("failed", "Couldn't send the clips.", phrase, ses)
            post_state("enroll", text=say, who=persona, heard=f"0/{n}|error|")
        time.sleep(6)
    finally:
        ENROLL_STATE["busy"] = False


def training_notice(phrase, published):
    """Display notice when a training job finishes. hey_finn → "Hey Finn is ready"."""
    words = str(phrase or "").replace("-", " ").replace("_", " ").split()
    label = " ".join((w[:1].upper() + w[1:]) for w in words if w) or "Hey"
    if published:
        return f"{label} is ready"
    return f"{label} needs more clips. Record 20-30 somewhere quiet."


def _train_outcome(j):
    """published, failed_quality, failed, or None while the job is still running.

    Legacy "done" plus passed is published. "done" without passed is a gate miss, never a success.
    """
    if not isinstance(j, dict):
        return None
    st = j.get("state")
    if st in ("published", "failed_quality", "failed"):
        return st
    if st == "done":
        return "published" if j.get("passed") else "failed_quality"
    return None


def follow_training(phrase, ses, say, persona, every=60, limit=4 * 3600):
    """Poll the relay until training finishes, then post a short notice (waiting out an open voice session).

    published → "Hey Finn is ready". failed_quality → more clips. A hard failure keeps the enroll error screen.
    """
    t0, shown_queue = time.time(), False
    while time.time() - t0 < limit:
        raw = relay_get(f"/enroll/{phrase}/{ses}/status")
        try:
            j = json.loads(raw) if raw else {}
        except ValueError:
            j = {}
        st = j.get("state")
        outcome = _train_outcome(j)
        if st == "queued" and not shown_queue and not SESSION_OPEN.is_set():
            enroll_stage("training", "Training.", phrase, ses)
            post_state("enroll", text=say, who=persona, heard=f"0/0|queued|{int(j.get('position', 1))}")
            shown_queue = True
        elif outcome in ("published", "failed_quality"):
            while SESSION_OPEN.is_set():
                time.sleep(2)
            ok = outcome == "published"
            print(f"enroll result {phrase} {ses}: {'passed' if ok else 'not passed'}", flush=True)
            enroll_stage("published" if ok else "failed_quality",
                         "Ready." if ok else training_notice(phrase, False), phrase, ses)
            post_state("notice", text=training_notice(phrase, ok), who=persona)
            if ok:
                SYNC_NOW.set()
            return
        elif outcome == "failed":
            while SESSION_OPEN.is_set():
                time.sleep(2)
            print(f"enroll result {phrase} {ses}: failed", flush=True)
            enroll_stage("failed", "Training didn't pass.", phrase, ses)
            post_state("enroll", text=say, who=persona, heard="0/0|error|")
            return
        time.sleep(every)


def sync_wakewords():
    """Fetch models published from real-voice enrollment (GET /wakewords, /wakeword/<name>.onnx). Each file is checked
    against its sha256; wakewords.enrolled.json lists the active ones. Sets RELOAD when anything changed."""
    if not RELAY_URL:
        return
    raw = relay_get("/wakewords")
    try:
        items = json.loads(raw)["wakewords"] if raw else None
    except (ValueError, KeyError, TypeError):
        items = None
    if items is None:
        return
    import hashlib
    os.makedirs(ENROLLED_DIR, exist_ok=True)
    words, changed = [], False
    for it in items:
        name, persona, sha = str(it.get("name", "")), str(it.get("persona", "")), str(it.get("sha256", ""))
        if not (phrase_ok(name) and persona_ok(persona) and re.fullmatch(r"[0-9a-f]{64}", sha)):
            continue
        path = os.path.join(ENROLLED_DIR, name + ".onnx")
        have = hashlib.sha256(open(path, "rb").read()).hexdigest() if os.path.isfile(path) else ""
        if have != sha:
            blob = relay_get(f"/wakeword/{name}.onnx")
            if not blob or hashlib.sha256(blob).hexdigest() != sha:
                print(f"wakeword {name}: download failed or checksum mismatch", flush=True)
                continue
            with open(path + ".tmp", "wb") as f:
                f.write(blob)
            os.replace(path + ".tmp", path)
            changed = True
            print(f"wakeword {name}: new enrolled model", flush=True)
        words.append({"name": name, "persona": persona, "threshold": float(it.get("threshold", 0.5)),
                      "patience": int(it.get("patience", 1)), "path": path, "enrolled": True})
    cfgp = os.path.join(ENROLLED_DIR, "wakewords.enrolled.json")
    old = open(cfgp).read() if os.path.isfile(cfgp) else ""
    new = json.dumps({"words": words}, indent=1)
    if new != old:
        with open(cfgp + ".tmp", "w") as f:
            f.write(new)
        os.replace(cfgp + ".tmp", cfgp)
        changed = True
    if changed:
        RELOAD.set()


WAKE_CONFIG = E("WAKE_CONFIG", "/opt/grok-station/wake/wakewords.json")
SLUG_RE = re.compile(r"^[a-z]{2,16}$")
WORDS = {}  # model key -> {"name", "persona", "threshold", "patience", "provisional"}


def _word(w):
    return {"name": str(w["name"]), "persona": str(w.get("persona", "grok")), "threshold": float(w.get("threshold", 0.5)),
            "patience": max(1, int(w.get("patience", 1))), "provisional": bool(w.get("provisional", False)),
            "enrolled": bool(w.get("enrolled", False))}


def apply_wake_overrides(entries, ov):
    """On/off and sensitivity, plus a custom phrase -> persona. The mapping wins over the enrolled persona."""
    if not isinstance(ov, dict):
        return entries
    for name, o in ov.items():
        if not isinstance(o, dict) or name not in entries:
            continue
        if o.get("enabled") is False:
            entries.pop(name, None)
            print(f"wake word {name}: turned off on the admin page", flush=True)
            continue
        if isinstance(o.get("threshold"), (int, float)) and 0.05 <= float(o["threshold"]) <= 0.99:
            entries[name]["threshold"] = float(o["threshold"])
        if persona_ok(str(o.get("persona") or "")):
            entries[name]["persona"] = str(o["persona"])
    return entries


def load_model():
    """One openWakeWord Model holding every configured word. Fills WORDS (model key -> word settings)."""
    from openwakeword.model import Model
    WORDS.clear()
    try:
        with open(WAKE_CONFIG) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = None
    except (OSError, ValueError) as e:
        print(f"wake config unreadable ({type(e).__name__}); using {MODEL}", flush=True)
        cfg = None
    if cfg is None:
        WORDS[MODEL] = _word({"name": MODEL, "threshold": THRESH})
        return Model(wakeword_models=[MODEL], inference_framework="onnx")
    paths = []
    entries = {w["name"]: dict(w) for w in cfg.get("words", []) if "name" in w}
    try:  # models trained from real-voice enrollment override (or add to) the repo config
        with open(os.path.join(E("WAKE_ENROLLED_DIR", "/opt/grok-station/wake/models/enrolled"), "wakewords.enrolled.json")) as f:
            for w in json.load(f).get("words", []):
                if os.path.isfile(str(w.get("path", ""))):
                    entries[w["name"]] = dict(w)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    try:  # admin page: per-word on/off, sensitivity, and custom phrase -> persona
        with open(OVERRIDES) as f:
            ov = json.load(f)
    except (OSError, ValueError):
        ov = {}
    apply_wake_overrides(entries, ov)
    for raw in entries.values():
        w = _word(raw)
        path = raw.get("path") or os.path.join(cfg.get("models_dir", ""), w["name"] + ".onnx")
        if not os.path.isfile(path):
            print(f"wake word {w['name']}: model missing, skipped", flush=True)
            continue
        paths.append(path); WORDS[w["name"]] = w
    fb = cfg.get("fallback")
    if fb or not paths:
        fb = _word(fb or {"name": "hey_jarvis"})
        paths.append(fb["name"]); WORDS[fb["name"]] = fb
    model = Model(wakeword_models=paths, inference_framework="onnx")
    for k in list(WORDS):
        if k not in model.models:
            print(f"wake word {k}: not in loaded models, skipped", flush=True)
            WORDS.pop(k)
    for k, w in WORDS.items():
        print(f"wake word {k} -> {w['persona']} threshold={w['threshold']} patience={w['patience']}"
              f"{' (provisional)' if w['provisional'] else ''}{' (enrolled)' if w['enrolled'] else ''}", flush=True)
    print(f"loaded {len(WORDS)} wake words", flush=True)
    return model


class Detector:
    """Per-word consecutive-frame counters. feed(scores) returns the triggered model key (highest score) or None."""

    def __init__(self):
        self.n = {k: 0 for k in WORDS}

    def reset(self):
        self.n = {k: 0 for k in WORDS}

    def feed(self, sc):
        hits = []
        for k, w in WORDS.items():
            s = sc.get(k, 0.0)
            self.n[k] = self.n[k] + 1 if s >= w["threshold"] else 0
            if self.n[k] >= w["patience"]:
                hits.append((s, k))
        return max(hits)[1] if hits else None


def persona_name(slug):
    """Display name for a persona slug from the cached personas.json; Grok when unknown."""
    if slug == "grok":
        return "Grok"
    try:
        with open(os.path.join(PORTRAITS, "personas.json")) as f:
            for p in json.load(f).get("personas", []):
                if p.get("slug") == slug:
                    return " ".join(str(p.get("name", "")).split())[:20] or "Grok"
    except (OSError, ValueError, AttributeError):
        pass
    return "Grok"


def find_card(tool):
    out = subprocess.run([tool, "-l"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        m = re.match(r"card (\d+): (\S+) \[(.*?)\], device (\d+)", line)
        if m and DEVICE.lower() in (m.group(2) + m.group(3)).lower():
            return f"plughw:CARD={m.group(2)},DEV={m.group(4)}"
    return None


def make_chime(path):
    if os.path.exists(path):
        return
    sr = 16000
    t = np.arange(int(sr * 0.1)) / sr
    env = np.minimum(1, np.minimum(t, t[::-1]) / 0.01)          # 10 ms fade in/out per tone
    tone = np.concatenate([np.sin(2 * np.pi * 880 * t) * env, np.sin(2 * np.pi * 1320 * t) * env])
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((tone * 0.35 * 32767).astype("<i2").tobytes())


def post_state(state=None, **kw):
    """POST /state. The log line is the state and the kinds of attachment (card / timer count), never the values."""
    body = {}
    if state:
        body["state"] = state
    body.update(kw)
    bits = [str(state)] if state else []
    if "card" in kw:
        kind = kw["card"].get("kind") if isinstance(kw.get("card"), dict) else ""
        bits.append("card " + kind if kind in ("weather", "time", "convert") else "card")
    if "timers" in kw:
        n = len(kw["timers"]) if isinstance(kw.get("timers"), list) else 0
        bits.append(f"timers {n}")
    who = kw.get("who") if isinstance(kw.get("who"), str) else ""
    print(f"display -> {' '.join(bits) or 'update'}{(' (' + who + ')') if who else ''}", flush=True)
    try:
        req = urllib.request.Request(STATE_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).read()
    except Exception as e:
        print(f"state post failed: {type(e).__name__}", flush=True)


def scores(model, pcm):
    return model.predict(pcm)


def test(path):
    """Offline: feed a 16 kHz mono WAV through the same detector; report the trigger. Never opens a session."""
    model = load_model()
    with wave.open(path) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, "need 16 kHz mono s16"
        pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2")
    pcm = np.concatenate([np.zeros(16000, "<i2"), pcm, np.zeros(16000, "<i2")])
    det, peak, hit = Detector(), {k: 0.0 for k in WORDS}, None
    for i in range(0, len(pcm) - CHUNK, CHUNK):
        sc = scores(model, pcm[i:i + CHUNK])
        for k in peak:
            peak[k] = max(peak[k], sc.get(k, 0.0))
        k = det.feed(sc)
        if k and not hit:
            hit = (k, i / 16000)
    others = " ".join(f"{k} {v:.3f}" for k, v in peak.items() if not hit or k != hit[0])
    if hit:
        print(f"{os.path.basename(path)}: TRIGGER {hit[0]} -> {WORDS[hit[0]]['persona']} at {hit[1]:.2f}s "
              f"(peak {peak[hit[0]]:.3f}); others: {others}")
    else:
        print(f"{os.path.basename(path)}: no trigger; peaks: {others}")


def bench(seconds=30):
    model = load_model()
    x = (np.random.randn(16000 * seconds) * 1000).astype("<i2")
    for i in range(0, 16000 * 2, CHUNK):
        model.predict(x[i:i + CHUNK])
    t, n = time.process_time(), 0
    for i in range(0, len(x) - CHUNK, CHUNK):
        model.predict(x[i:i + CHUNK]); n += 1
    ms = (time.process_time() - t) / n * 1000
    print(f"bench: {len(WORDS)} words, {ms:.2f} ms CPU per 80 ms frame = {ms / 80 * 100:.1f}% of one core")


class Session:
    """One question and answer through the relay. Feed it 24 kHz PCM16 bytes with feed(); runs in a thread."""

    def __init__(self, play_dev, save=None, skip=VOICE_SKIP, echo_text=False, persona="grok"):
        self.q, self.feeding, self.active = queue.Queue(maxsize=400), True, True
        self.skip = int(skip * 24000) * 2  # bytes of 24 kHz PCM16 still to drop
        self.echo_text = echo_text         # print transcripts: only for the manual --ask check, never the service log
        self.play_dev, self.save, self.t_end_speech, self.t_first_audio = play_dev, save, None, None
        self.heard, self.reply, self.ready, self.responding = "", "", threading.Event(), False
        self.turns = 0  # finished turns; a multi-turn relay keeps the session open for a follow-up
        SESSION_OPEN.set()
        self.persona = persona if persona_ok(persona or "") else "grok"
        self.who = persona_name(self.persona)  # the persona speaking; the relay's persona event changes it
        self.card = None  # last tool card; attached to later state posts until the session ends
        self.ws, self.t1_card = None, None
        CURRENT["session"] = self
        threading.Thread(target=self.run, daemon=True).start()

    def state(self, state, **kw):
        if getattr(self, "card", None) and "card" not in kw:
            kw["card"] = self.card
        if self.t1_card and state in ("speaking", "thinking", "listening"):  # keep the read-back card up until decided
            c = self.t1_card
            post_state("confirm", who=c["name"], text=c["text"], heard=f"{c['op']}|{c['list']}|{1 if c['needs_tap'] else 0}")
            return
        post_state(state, who=self.who, **kw)

    def feed(self, pcm24):
        if self.skip > 0:
            n = min(self.skip, len(pcm24))
            self.skip -= n
            pcm24 = pcm24[n:]
            if not pcm24:
                return
        if self.feeding:
            try:
                self.q.put_nowait(pcm24)
            except queue.Full:
                pass

    def _sender(self, ws):
        self.ready.wait(5)  # hold buffered mic audio until the xAI session is configured
        try:
            while self.active:  # multi-turn: feeding pauses during a reply and resumes for the follow-up
                try:
                    ws.send(self.q.get(timeout=0.2))
                except queue.Empty:
                    pass
        except Exception:
            pass

    def run(self):
        from websockets.sync.client import connect
        player, out, t0 = None, [], time.time()
        try:
            for attempt in (1, 2):  # one retry: a cold mDNS lookup can stall the first connect
                try:
                    tls = {"ssl": relay_ssl()} if RELAY_URL.startswith("wss://") else {}
                    q = [f"persona={self.persona}"] if self.persona != "grok" else []
                    tq = timers_query()
                    if tq:
                        q.append("timers=" + tq)
                    q.append("t1=" + t1_query())
                    url = RELAY_URL + ("?" + "&".join(q) if q else "")
                    ws = connect(url, additional_headers={"Authorization": f"Bearer {RELAY_TOKEN}"},
                                 open_timeout=4, max_size=1 << 22, legacy=True, **tls)
                    break
                except Exception as e:
                    err = e
            else:
                e = err
                print(f"relay unreachable: {type(e).__name__}", flush=True)
                self.state("error", text="Can’t reach Grok right now.")
                return
            print(f"relay connected in {time.time() - t0:.2f}s", flush=True)
            with ws:
                self.ws = ws
                threading.Thread(target=self._sender, args=(ws,), daemon=True).start()
                for m in ws:
                    if isinstance(m, bytes):
                        if self.t_first_audio is None:
                            self.feeding, self.t_first_audio = False, time.time()
                            lat = self.t_first_audio - (self.t_end_speech or self.t_first_audio)
                            print(f"first audio {lat:.2f}s after end of speech", flush=True)
                            self.state("speaking", heard=self.heard, text=self.reply or "…")
                            player = subprocess.Popen(["aplay", "-q", "-D", self.play_dev, "-f", "S16_LE", "-r", "24000",
                                                       "-c", "1", "-t", "raw"], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                        if self.save is not None:
                            out.append(m)
                        try:
                            player.stdin.write(m)
                        except (BrokenPipeError, AttributeError):
                            pass
                        continue
                    ev = json.loads(m)
                    t = ev.get("type")
                    if t == "ready":
                        self.ready.set()
                    elif t == "persona":
                        name = " ".join(str(ev.get("name", "")).split())[:20]
                        if name:
                            self.who, self.reply = name, ""
                            print(f"persona: {name}", flush=True)
                            # keep the current phase: a wake-word persona arrives while the person is still talking
                            if self.t_first_audio:
                                self.state("speaking", heard=self.heard, text="…")
                            elif self.responding:
                                self.state("thinking", heard=self.heard, text="…")
                            else:
                                self.state("listening")
                    elif t == "speech_stopped":
                        # keep streaming: a short noise may get no response, and the real question follows
                        self.t_end_speech = time.time()
                        print("relay: speech stopped", flush=True)
                        if not self.responding:
                            self.state("listening")  # refreshes the listening screen's timeout
                    elif t == "speech_started":
                        print("relay: speech started", flush=True)
                        if not self.responding:
                            self.state("listening")
                    elif t == "response_started":
                        self.feeding, self.responding = False, True  # one answer per wake: mic stops here
                        print("relay: response started", flush=True)
                        if not self.t_first_audio:
                            self.state("thinking", heard=self.heard)
                    elif t == "heard":
                        self.heard = " ".join(ev.get("text", "").split())
                        print(f"heard: {self.heard!r}" if self.echo_text else f"heard: {len(self.heard)} chars", flush=True)
                        if self.heard or self.responding:
                            self.state("speaking" if self.t_first_audio else "thinking", heard=self.heard, text=self.reply or "…")
                    elif t == "reply":
                        self.reply = ev.get("text", "").strip()
                        print(f"reply: {self.reply!r}" if self.echo_text else f"reply: {len(self.reply)} chars", flush=True)
                        if self.t_first_audio:
                            self.state("speaking", heard=self.heard, text=self.reply)
                    elif t == "reply_delta":
                        self.reply += ev.get("text", "")
                    elif t == "handoff_wait":
                        # teammate handoff: the persona is asking its own Grok Bot; keep the thinking screen up
                        name = str(ev.get("name", ""))[:24] or "a teammate"
                        print("relay: handoff wait", flush=True)
                        self.state("thinking", heard=self.heard, text=f"Checking with {name}…")
                    elif t == "t1_confirm":
                        lst = " ".join(re.sub(r"[^A-Za-z ']", " ", str(ev.get("list") or "")).split())[:24]
                        self.t1_card = {"request_id": str(ev.get("request_id", ""))[:40], "name": str(ev.get("name", ""))[:24] or self.who,
                                        "op": str(ev.get("op", ""))[:10], "list": lst, "text": str(ev.get("text", ""))[:120],
                                        "needs_tap": bool(ev.get("needs_tap"))}
                        print("t1: read-back card", flush=True)  # no text in the log
                        self.state("speaking")
                    elif t == "t1_result":
                        self.t1_card = None
                        print(f"t1: {'confirmed' if ev.get('ok') else 'not saved'}", flush=True)
                        self.state("thinking", heard=self.heard, text="Saving…" if ev.get("ok") else "Not saved.")
                    elif t == "handoff_still":
                        # "Still asking <bot>": play the short line, keep the thinking/wait screen as it is
                        print("relay: handoff still", flush=True)
                        try:
                            pcm = base64.b64decode(str(ev.get("audio", ""))[:400000], validate=True)
                        except ValueError:
                            pcm = b""
                        if pcm:
                            try:
                                p = subprocess.Popen(["aplay", "-q", "-D", self.play_dev, "-f", "S16_LE", "-r", "24000", "-c", "1",
                                                      "-t", "raw"], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                                threading.Thread(target=p.communicate, args=(pcm,), daemon=True).start()
                            except OSError:
                                pass
                    elif t in ("timer_set", "timer_cancel"):
                        timer_event(ev)
                    elif t == "card":
                        card = take_card(ev)
                        if not card:
                            print("relay: card ignored", flush=True)
                        else:
                            self.card = card
                            print(f"relay: card {card['kind']}", flush=True)
                            phase = "speaking" if self.t_first_audio else ("thinking" if self.responding else "listening")
                            kw = {"heard": self.heard}
                            if self.reply:
                                kw["text"] = self.reply
                            elif phase != "listening":
                                kw["text"] = "…"
                            self.state(phase, **kw)
                    elif t == "handoff_done":
                        print("relay: handoff done", flush=True)
                    elif t == "xai_error":
                        print("relay: xai error", flush=True)
                    elif t == "turn_done":
                        # multi-turn: let the reply finish playing, then listen for a follow-up (no wake word)
                        if player:
                            player.stdin.close(); player.wait(timeout=90); player = None
                        self.turns += 1
                        print(f"relay: turn {self.turns} done, listening for a follow-up", flush=True)
                        while not self.q.empty():  # mic audio queued during playback is the reply's echo
                            self.q.get_nowait()
                        self.heard, self.reply, self.t_first_audio, self.t_end_speech = "", "", None, None
                        self.responding, self.feeding = False, True
                        self.state("listening")
                        ws.send(json.dumps({"type": "listening"}))
                    elif t in ("done", "timeout", "error"):
                        print(f"relay: {t}", flush=True)
                        if t != "error" and self.turns and not self.t_first_audio:
                            post_state("idle")  # follow-up window closed quietly (silence or "thanks")
                        elif t == "timeout" and not self.t_first_audio:
                            self.state("notice", text="Didn’t catch that.")
                        elif t == "error":
                            self.state("error", text="Can’t reach Grok right now.")
                        break
            self.feeding = False
            self.t1_card = None  # session over: the card goes (the relay records an undecided read-back as declined)
            if player:
                player.stdin.close()
                player.wait(timeout=90)
                self.state("speaking", heard=self.heard, text=self.reply or "…", done=True)
            print(f"session over in {time.time() - t0:.1f}s", flush=True)
        except Exception as e:
            print(f"session failed: {type(e).__name__}", flush=True)
            self.state("error", text="Can’t reach Grok right now.")
        finally:
            self.feeding = False
            self.ws, self.t1_card = None, None
            if CURRENT["session"] is self:
                CURRENT["session"] = None
            if self.save is not None and out:
                with wave.open(self.save, "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"".join(out))
            self.active = False
            SESSION_OPEN.clear()


def ask(path, save=None):
    """Offline end-to-end check: stream a 24 kHz mono WAV through the relay as if it were the mic."""
    with wave.open(path) as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1 and w.getsampwidth() == 2, "need 24 kHz mono s16"
        pcm = w.readframes(w.getnframes())
    post_state("listening")
    s = Session(find_card("aplay") or "default", save, echo_text=True)
    step = 1920 * 2  # 80 ms
    for i in range(0, len(pcm), step):
        s.feed(pcm[i:i + step]); time.sleep(0.08)
    t_spoken = time.time()
    while s.feeding:  # trailing silence so server VAD can end the turn
        s.feed(b"\0" * step); time.sleep(0.08)
    while s.active:
        time.sleep(0.05)
    if s.t_first_audio:
        print(f"RESULT end-of-file->first audio {s.t_first_audio - t_spoken:.2f}s, "
              f"VAD end->first audio {s.t_first_audio - (s.t_end_speech or t_spoken):.2f}s", flush=True)


def main():
    make_chime(CHIME)
    if RELAY_URL:
        threading.Thread(target=persona_loop, daemon=True).start()
    threading.Thread(target=control_server, daemon=True).start()
    timers_load()
    threading.Thread(target=timers_loop, daemon=True).start()
    threading.Thread(target=tap_loop, daemon=True).start()
    model = load_model()
    det = Detector()
    print(f"cooldown={COOLDOWN}s gain={GAIN}", flush=True)
    while True:
        cap = find_card("arecord")
        if not cap:
            print(f"no capture device matching {DEVICE!r}; retrying", flush=True)
            time.sleep(5)
            continue
        play = find_card("aplay") or cap
        LIVE["play"] = play
        print(f"capture {cap} playback {play}", flush=True)
        p = subprocess.Popen(["arecord", "-q", "-D", cap, "-f", "S16_LE", "-r", str(CAP_RATE), "-c", "1", "-t", "raw"],
                             stdout=subprocess.PIPE)  # buffered: read(n) returns full chunks
        last_hit, last_log, peak, rms_sum, n, peaks = 0.0, time.time(), 0.0, 0.0, 0, {}
        tail = np.zeros(len(_K) - 1, np.float32)
        tail24 = np.zeros(len(_K24) - 1, np.float32)
        sess = None
        while True:
            buf = p.stdout.read(CHUNK * DECIM * 2)
            if not buf or len(buf) < CHUNK * DECIM * 2:
                break
            x = np.frombuffer(buf, "<i2").astype(np.float32)
            if not sess and not ENROLL_Q.empty():
                def read16(raw=False):
                    nonlocal tail
                    b = p.stdout.read(CHUNK * DECIM * 2)
                    if not b or len(b) < CHUNK * DECIM * 2:
                        raise EOFError
                    y = np.frombuffer(b, "<i2").astype(np.float32)
                    if DECIM > 1:
                        yy = np.concatenate([tail, y]); tail = yy[-(len(_K) - 1):]
                        y = np.convolve(yy, _K, "valid")[::DECIM]
                    return np.clip(y * (1.0 if raw else GAIN), -32768, 32767).astype("<i2")
                try:
                    enroll(ENROLL_Q.get_nowait(), read16, play)
                except EOFError:
                    break
                except Exception as e:
                    print(f"enroll failed: {type(e).__name__}", flush=True)
                    post_state("error", text="Enrollment stopped.")
                model.reset(); det.reset()
                continue
            if RELOAD.is_set() and not sess:
                RELOAD.clear()
                try:
                    model = load_model(); det = Detector()
                except Exception as e:
                    print(f"wake model reload failed: {type(e).__name__}", flush=True)
                continue
            LIVE["session"] = bool(sess and sess.active)
            if sess and not sess.active:
                sess = None
                model.reset()  # drop audio features from the session before listening again
                det.reset()
            if sess:
                if sess.feeding:
                    xx = np.concatenate([tail24, x]); tail24 = xx[-(len(_K24) - 1):]
                    y = np.convolve(xx, _K24, "valid")[::D24] if D24 > 1 else up24(x, xx[-len(x) - 1]) if CAP_RATE == 16000 else x
                    sess.feed(np.clip(y * VOICE_GAIN, -32768, 32767).astype("<i2").tobytes())
                continue  # wake detector paused while a session is open
            if DECIM > 1:
                xx = np.concatenate([tail, x]); tail = xx[-(len(_K) - 1):]
                x = np.convolve(xx, _K, "valid")[::DECIM]
            pcm = np.clip(x * GAIN, -32768, 32767).astype("<i2")
            sc = scores(model, pcm)
            s = max(sc.values(), default=0.0)
            hit = det.feed(sc)
            r = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
            rms_sum += r; n += 1
            LIVE["dbfs"], LIVE["at"] = round(float(20 * np.log10(max(r, 1) / 32768)), 1), time.time()  # admin mic meter
            peak = max(peak, s)
            for k, v in sc.items():
                peaks[k] = max(peaks.get(k, 0.0), v)
            now = time.time()
            if hit and now - last_hit >= COOLDOWN:
                last_hit = now
                slug = WORDS[hit]["persona"]
                print(f"WAKE {hit} score={sc.get(hit, 0.0):.3f} persona={slug}", flush=True)
                det.reset()
                mode = setup_mode()
                if mode:  # not set up (or password reset / no network): chime + spoken line, no relay session
                    print(f"wake ignored: {mode} pending", flush=True)
                    line = os.path.join(SETUP_LINES, mode + ".wav")
                    subprocess.Popen(f"aplay -q -D {shlex.quote(play)} {shlex.quote(CHIME)}" +
                                     (f"; aplay -q -D {shlex.quote(play)} {shlex.quote(line)}" if os.path.isfile(line) else ""),
                                     shell=True, stderr=subprocess.DEVNULL)
                    continue
                subprocess.Popen(["aplay", "-q", "-D", play, CHIME], stderr=subprocess.DEVNULL)
                threading.Thread(target=post_state, args=("listening",), kwargs={"who": persona_name(slug)}, daemon=True).start()
                if RELAY_URL:
                    sess = Session(play, persona=slug)
            if now - last_log >= LEVEL_EVERY:
                avg = rms_sum / max(1, n)
                dbfs = 20 * np.log10(max(avg, 1) / 32768)
                top = " ".join(f"{k}={v:.3f}" for k, v in sorted(peaks.items(), key=lambda kv: -kv[1]) if v >= 0.05)
                print(f"level rms={avg:.0f} ({dbfs:.1f} dBFS) peak_score={peak:.3f} frames={n}{' peaks ' + top if top else ''}", flush=True)
                last_log, peak, rms_sum, n, peaks = now, 0.0, 0.0, 0, {}
        p.kill()
        print("capture ended; restarting", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        test(sys.argv[2])
    elif len(sys.argv) in (2, 3) and sys.argv[1] == "--bench":
        bench(int(sys.argv[2]) if len(sys.argv) == 3 else 30)
    elif len(sys.argv) == 2 and sys.argv[1] == "--sync":
        sync_personas()
        sync_wakewords()
    elif len(sys.argv) in (3, 4, 5) and sys.argv[1] == "--enroll":
        args = [a for a in sys.argv[2:] if a != "--topup"]
        top = "--topup" in sys.argv
        n = int(args[1]) if len(args) > 1 else (TOPUP_COUNT if top else ENROLL_COUNT)
        body = json.dumps({"phrase": args[0], "count": n, "topup": top}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{CONTROL_PORT}/enroll", body,
                                                               {"Content-Type": "application/json"}), timeout=5) as r:
                print(r.read().decode())
        except urllib.error.HTTPError as e:
            print(e.read().decode()); sys.exit(1)
    elif len(sys.argv) in (3, 4) and sys.argv[1] == "--ask":
        ask(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    else:
        main()
