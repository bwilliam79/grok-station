#!/usr/bin/env python3
"""grok-station wake listener + voice client (proof of concept).

Captures the USB headset mic at 48 kHz via `arecord`, decimates to 16 kHz for openWakeWord (ONNX).
On a wake it plays the listen tone, shows "listening", and opens one session to the voice relay on
127.0.0.1: mic audio goes up as PCM16 24 kHz, reply audio comes back and plays on the headset via
`aplay`, and the display follows listening -> thinking -> speaking -> idle through localhost POST /state.
The thinking phase is also the waiting chime: one soft two-note tone every 2.5 s, after a short delay, until speech.
A tool card (`{"type":"card",...}`) is forwarded on that same POST. Timers post their full list
(`{"timers":[{id,label,ends_at_epoch,ringing,duration?}]}`) when the set changes — set, cancel, ring, clear —
and the display counts down locally. While this process is up the deadline is monotonic, so a wall-clock
step does not fire a timer early; the file stores a wall deadline so a restart still knows when it ends.
Logs name the event type only, never a temperature, label or duration.
The wake detector is paused during a session so the reply can't re-trigger it.
If no full chunk arrives for 3 seconds the capture is killed and opened again. A stream of
digital silence while nothing is playing is reopened too. A stuck speakerphone is reset
over USB only through the root helper (never by this process), and never during a session.
`grok_wake.py --test file.wav` scores a 16 kHz mono WAV offline and prints the peak score.
`grok_wake.py --ask file.wav [out.wav]` sends a 24 kHz mono WAV question through the relay path.
The relay is ws://127.0.0.1:<RELAY_PORT>/ws (default port 8790). The bearer token is read from
GROK_LOCAL_TOKEN_FILE (default /etc/grok-station/secrets/local_token); RELAY_TOKEN is only a local fallback.
Personas: the relay may switch the session to a teammate ({"type":"persona","slug","name"}); the display then shows
that name and portrait. At startup (and hourly) the client caches GET /personas and each GET /portrait/<slug> into
GROK_STATION_PORTRAITS (default /opt/grok-station/portraits, never in git); the display reads personas.json there to
keep /show from posting as any persona.
Wake words: WAKE_CONFIG (default /opt/grok-station/wake/wakewords.json, repo copy wake/wakewords.json) lists custom
openWakeWord models (models_dir/<name>.onnx), each with a persona slug, threshold and patience (consecutive 80 ms
frames at or above the threshold). A wake opens the session as that persona (relay /ws?persona=<slug>). The optional
Hey Grok (models/hey_grok.onnx) is the only built-in word and is always on. Without the file: hey_grok alone.
`grok_wake.py --test file.wav` reports which word/persona the clip triggers (never opens a session);
`grok_wake.py --bench [seconds | file.wav]` times the loaded models on noise or on recorded audio.
Recorded wake words (wake/fewshot.py) are built on this station from the user's 5 recordings, with no training
step, and scored on the same embedding stream as Hey Grok.
"""
import base64, datetime, hashlib, hmac, json, os, queue, re, shutil, subprocess, sys, threading, time, urllib.parse, urllib.request, wave
import numpy as np
import wait_tone


def _train_flow_file_ok(path, root_uid=0):
    """Same rule as relay.code_file_ok: a real root-owned 0644 file, every parent a real
    root-owned directory that is not group- or other-writable. A symlink is refused."""
    import stat
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return False
    if st.st_uid != root_uid or stat.S_IMODE(st.st_mode) != 0o644:
        return False
    p = os.path.dirname(os.path.abspath(path))
    while True:
        try:
            dst = os.lstat(p)
        except OSError:
            return False
        if stat.S_ISLNK(dst.st_mode) or not stat.S_ISDIR(dst.st_mode) or dst.st_uid != root_uid or dst.st_mode & 0o022:
            return False
        parent = os.path.dirname(p)
        if parent == p:
            return True
        p = parent


def _load_train_flow(path=None):
    """The one copy lives beside the relay, which is root-owned when installed.

    This listener must not keep a second copy under wake/: grok can edit that tree,
    and the relay used to exec it. Repo and install share the same layout
    (wake/ and relay/ are siblings).
    Fails closed: a missing, symlinked or untrusted file (or one that will not load) logs one
    line and returns None. The listener keeps running with wake-word training disabled.
    """
    import importlib.util
    import stat
    if path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.normpath(os.path.join(os.path.dirname(here), "relay", "train_flow.py"))
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise RuntimeError("not a regular file")
        if os.path.abspath(path).startswith("/opt/grok-station" + os.sep) and not _train_flow_file_ok(path):
            raise RuntimeError("untrusted")
        spec = importlib.util.spec_from_file_location("gs_train_flow", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        print(f"wake-word training disabled: train_flow unavailable ({type(e).__name__})", flush=True)
        return None


train_flow = _load_train_flow()
TRAINING_OFF = {"error": "Wake-word training is unavailable on this station."}

voice_train_plan = getattr(train_flow, "voice_train_plan", None)
train_steps = getattr(train_flow, "train_steps", None)
silent_clip = getattr(train_flow, "silent_clip", None)
mic_stopped_line = getattr(train_flow, "mic_stopped_line", None)
CANCEL_SAY = getattr(train_flow, "CANCEL_SAY", "")

E = os.environ.get
MODEL = E("WAKE_MODEL", "/opt/grok-station/wake/models/hey_grok.onnx")  # the built-in word; used if the config is unreadable
BUILTIN = "hey_grok"                             # always on: the admin page can't turn it off
THRESH = float(E("WAKE_THRESHOLD", "0.5"))
COOLDOWN = float(E("WAKE_COOLDOWN", "2"))
def _station_gain():
    """gain.py next to this file. The admin helper loads the same module (no numpy)."""
    mod = getattr(_station_gain, "mod", None)
    if mod is None:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gain.py")
        spec = importlib.util.spec_from_file_location("grok_station_gain", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _station_gain.mod = mod
    return mod


def default_gain(device):
    return _station_gain().default_gain(device)


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
RELAY_URL = "ws://127.0.0.1:%s/ws" % E("RELAY_PORT", "8790")


def relay_token():
    """Bearer for the loopback relay. The file is group-readable by this user. Never logged."""
    path = E("GROK_LOCAL_TOKEN_FILE", "/etc/grok-station/secrets/local_token")
    try:
        with open(path) as f:
            tok = f.read().strip()
        if tok and "\n" not in tok and "\r" not in tok:
            return tok
    except OSError:
        pass
    return E("RELAY_TOKEN", "") or E("GROK_RELAY_TOKEN", "")
VOICE_GAIN = float(E("VOICE_GAIN", str(GAIN)))   # gain on the audio sent to Grok
VOICE_SKIP = float(E("VOICE_SKIP_MS", "300")) / 1000  # drop this much post-wake audio (tail of the wake word)
PORTRAITS = E("GROK_STATION_PORTRAITS", "/opt/grok-station/portraits")
D24 = CAP_RATE // 24000                          # 48k -> 24k decimation; 16k capture is upsampled instead (up24)
_K24 = np.sinc(np.arange(-24, 25) / max(1, D24)) * np.hamming(49); _K24 /= _K24.sum()


def up24(x, prev):
    """16 kHz chunk -> 24 kHz (x1.5, linear; speech content is below 8 kHz). prev is the previous chunk's last sample."""
    xs = np.concatenate([[prev], x])
    return np.interp(np.arange(len(x) * 3 // 2) / 1.5, np.arange(len(xs)), xs)


def relay_get(path, missing_ok=False):
    """GET a loopback relay path with the local bearer token. Returns bytes or None.
    missing_ok: a 404 is normal (a bot with no portrait uses its initials) and is not logged."""
    base = re.sub(r"^ws", "http", RELAY_URL).rsplit("/ws", 1)[0]
    try:
        req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {relay_token()}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.read(2 << 20)
    except urllib.error.HTTPError as e:
        if not (missing_ok and e.code == 404):
            print(f"relay get {path.split('/')[1]} failed: HTTP {e.code}", flush=True)
        return None
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
        img = relay_get(f"/portrait/{slug}", missing_ok=True)
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
        json.dump({"personas": [{"slug": str(p.get("slug", ""))[:24], "name": str(p.get("name", ""))[:20],
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
        SYNC_NOW.wait(DROP_POLL_S)
        SYNC_NOW.clear()


# ---------------------------------------------------------------- enroll mode (docs/enroll.md)
ENROLL_DIR = E("WAKE_ENROLL_DIR", "/var/lib/grok-station/enroll/inbox")  # clips stay on this Pi
ENROLLED_DIR = E("WAKE_ENROLLED_DIR", "/opt/grok-station/wake/models/enrolled")  # models trained from real clips
DROP_DIR = E("WAKE_DROP_DIR", "/var/lib/grok-station/wakewords")  # onnx + json dropped for the listener
DROP_POLL_S = 60
CONTROL_PORT = int(E("WAKE_CONTROL_PORT", "8789"))                     # 127.0.0.1 only
WAKE_TOKEN_FILE = "/etc/grok-station/secrets/wake_control_token"       # group grok reads it; the value is never logged
_WAKE_TOKEN = {"raw": b""}
_WAKE_CLIENT_WARNED = False
ENROLL_Q, SYNC_NOW, RELOAD = queue.Queue(maxsize=1), threading.Event(), threading.Event()
ENROLL_STATE = {"busy": False, "fast_until": 0, "last": None, "pause": False, "discard": False}
# Admin page (admin/grok_admin.py): until setup is done the station connects no one. The admin service writes
# SETUP_FILE ({"mode": "setup"|"reset"|"hotspot", ...}) while setup is pending and removes it when a password is set.
SETUP_FILE = E("GROK_SETUP_FILE", "/run/grok-station-admin/setup.json")
OVERRIDES = E("WAKE_OVERRIDES", "/var/lib/grok-station/admin/wake-overrides.json")  # {name: {enabled, threshold}}
SETUP_LINES = E("WAKE_SETUP_LINES", "/opt/grok-station/wake/lines")  # setup.wav, reset.wav, hotspot.wav (optional)
LIVE = {"dbfs": None, "at": 0.0, "audio_at": 0.0, "session": False, "play": None, "capture": None, "reopen_by": None, "reset_until": None}  # capture: open input device; reopen_by: backoff deadline; reset_until: USB reset hold
CAPTURE = {"proc": None, "guard": None}  # the open arecord, so a USB reset can drop it
RECOVERY = None  # MicRecovery, set in main before the control server accepts /mic-reset
# Station timers (relay tool set_timer): they live here so they ring after the conversation ends and survive restarts.
TIMERS_FILE = E("WAKE_TIMERS", "/opt/grok-station/wake/timers.json")
TIMERS, TIMERS_LOCK = {}, threading.Lock()
RING_HOLD = 60.0  # ringing chip stays up this long after the chimes and the spoken notice
MAX_STATION_TIMERS = 8
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


def _timer_left(v):
    """Seconds left. A mono_due (set while this process is up) ignores wall-clock steps."""
    mono = v.get("mono_due")
    if isinstance(mono, (int, float)) and not isinstance(mono, bool):
        return mono - time.monotonic()
    due = v.get("due")
    if isinstance(due, (int, float)) and not isinstance(due, bool):
        return due - time.time()
    return 0.0


def _hold_done(v, now):
    mono = v.get("ring_mono")
    if isinstance(mono, (int, float)) and not isinstance(mono, bool):
        return time.monotonic() >= mono
    until = v.get("ring_until")
    if isinstance(until, (int, float)) and not isinstance(until, bool):
        return now >= until
    return False


def _persist_timer(v):
    """File record. due and ring_until are wall times matching the monotonic remaining, so a restart agrees."""
    due = v.get("due")
    if isinstance(v.get("mono_due"), (int, float)) and not isinstance(v.get("mono_due"), bool):
        due = time.time() + (v["mono_due"] - time.monotonic())
    out = {"label": v.get("label") or "timer", "due": due}
    dur = v.get("duration")
    if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0:
        out["duration"] = dur
    if v.get("ringing"):
        out["ringing"] = True
        if isinstance(v.get("ring_mono"), (int, float)) and not isinstance(v.get("ring_mono"), bool):
            out["ring_until"] = time.time() + (v["ring_mono"] - time.monotonic())
        elif isinstance(v.get("ring_until"), (int, float)) and not isinstance(v.get("ring_until"), bool):
            out["ring_until"] = v["ring_until"]
    return out


def _anchor_timer(v, now_w, now_m):
    """Re-base a loaded record onto this process's monotonic clock."""
    due = float(v["due"])
    rec = {"label": _clean_timer_label(v.get("label")), "due": due, "mono_due": now_m + (due - now_w)}
    dur = v.get("duration")
    if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0:
        rec["duration"] = float(dur)
    if v.get("ringing"):
        rec["ringing"] = True
        until = v.get("ring_until")
        if isinstance(until, (int, float)) and not isinstance(until, bool):
            rec["ring_until"] = float(until)
            rec["ring_mono"] = now_m + (float(until) - now_w)
    return rec


def timers_load():
    try:
        with open(TIMERS_FILE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(d, dict):
        return
    now_w, now_m = time.time(), time.monotonic()
    keep = {}
    for k, v in d.items():
        if not re.fullmatch(r"[a-z0-9]{4,12}", str(k)) or not isinstance(v, dict):
            continue
        due = v.get("due")
        if isinstance(due, bool) or not isinstance(due, (int, float)):
            continue
        keep[str(k)] = _anchor_timer(v, now_w, now_m)
        if len(keep) >= MAX_STATION_TIMERS:
            break
    with TIMERS_LOCK:
        TIMERS.clear()
        TIMERS.update(keep)


def timers_save():
    try:
        snap = {}
        for k, v in TIMERS.items():
            if re.fullmatch(r"[a-z0-9]{4,12}", str(k)) and isinstance(v, dict) and isinstance(v.get("due"), (int, float)) and not isinstance(v.get("due"), bool):
                snap[str(k)] = _persist_timer(v)
        parent = os.path.dirname(TIMERS_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = TIMERS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, TIMERS_FILE)
    except OSError as e:
        print(f"timers save failed: {type(e).__name__}", flush=True)


def _clean_timer_label(s):
    return " ".join(re.sub(r"[^\w '\-]", " ", str(s or "")).split())[:24] or "timer"


def timer_rows():
    """Full timer list for the display. Ringing chips sort first, then soonest deadline.

    ends_at_epoch is wall-now plus the monotonic remaining, so the display's first tick matches
    this process even if the wall clock stepped after the timer was set.
    """
    with TIMERS_LOCK:
        rows = []
        wall = time.time()
        for k, v in TIMERS.items():
            if not isinstance(v, dict) or isinstance(v.get("due"), bool) or not isinstance(v.get("due"), (int, float)):
                continue
            row = {"id": k, "label": v.get("label") or "timer", "ends_at_epoch": wall + _timer_left(v),
                   "ringing": bool(v.get("ringing"))}
            dur = v.get("duration")
            if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0:
                row["duration"] = float(dur)
            rows.append(row)
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
            # Same id replaces (extend, or a restored timer). A new id stops at the cap.
            # seconds 0 is a restored ringing timer: show it, do not start a countdown or chime again.
            room = tid in TIMERS or len(TIMERS) < MAX_STATION_TIMERS
            if isinstance(sec, int) and not isinstance(sec, bool) and room and sec == 0:
                now_w, now_m = time.time(), time.monotonic()
                TIMERS[tid] = {"label": _clean_timer_label(ev.get("label")), "due": now_w, "mono_due": now_m,
                               "duration": 1, "ringing": True, "ring_until": now_w + RING_HOLD,
                               "ring_mono": now_m + RING_HOLD}
                changed = True
                print("timer ringing", flush=True)
            elif isinstance(sec, int) and not isinstance(sec, bool) and room and 1 <= sec <= 86400:
                TIMERS[tid] = {"label": _clean_timer_label(ev.get("label")), "due": time.time() + sec,
                               "mono_due": time.monotonic() + sec, "duration": sec}
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
    """Quoted JSON for the relay. Ringing timers are included so 'stop' can dismiss them."""
    with TIMERS_LOCK:
        items = []
        for k, v in TIMERS.items():
            if not isinstance(v, dict):
                continue
            left = _timer_left(v)
            ringing = bool(v.get("ringing"))
            if not ringing and left <= 0:
                if left < -3600:  # the station drops a timer this late without chiming
                    continue
                # Still stored, chime not started (a conversation may be using the speaker).
                # Report it ringing so the next session can dismiss it instead of forgetting it.
                ringing = True
                left = 0
            item = {"id": k, "label": v.get("label") or "timer", "left": int(round(left))}
            if ringing:
                item["ringing"] = True
            dur = v.get("duration")
            if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0:
                item["duration"] = int(round(dur))
            items.append(item)
    return urllib.parse.quote(json.dumps(items, separators=(",", ":"))) if items else ""


def _expire_rings(now):
    """Drop chips whose ringing hold has ended. A ring that never recorded its deadline gets one now."""
    removed = dirty = False
    with TIMERS_LOCK:
        for k, v in list(TIMERS.items()):
            if not v.get("ringing"):
                continue
            if not v.get("ring_until") and not v.get("ring_mono"):
                v["ring_until"] = now + RING_HOLD
                v["ring_mono"] = time.monotonic() + RING_HOLD
                dirty = True
            elif _hold_done(v, now):
                TIMERS.pop(k, None)
                removed = dirty = True
        if dirty:
            timers_save()
    if removed:
        print("timer clear", flush=True)
        post_timers()


def station_sounds():
    """Catalog module. Next to this checkout, else the installed copy. None if it isn't there."""
    import importlib.util
    cached = getattr(station_sounds, "_mod", None)
    if cached:
        return cached
    cands = []
    if os.environ.get("GROK_STATION_SOUNDS_PY"):
        cands.append(os.environ["GROK_STATION_SOUNDS_PY"])
    here = globals().get("__file__")
    if here:
        cands.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(here))), "sounds.py"))
    cands.append("/opt/grok-station/sounds.py")
    for p in cands:
        if not p or not os.path.isfile(p):
            continue
        spec = importlib.util.spec_from_file_location("grok_station_sounds", p)
        if not spec or not spec.loader:
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            continue
        station_sounds._mod = mod
        return mod
    return None


def play_file(dev, path, block=False):
    """aplay one wav the catalog already resolved. No-op without a device or a file.

    Holds the speaker so the thinking chime stops first and stays stopped until aplay exits.
    A non-blocking call claims the speaker before it returns, so the chime cannot start in the gap.
    """
    if not dev or not path:
        return
    cmd = ["aplay", "-q", "-D", dev, path]

    def run():
        try:
            subprocess.run(cmd, stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass

    if block:
        with wait_tone.speech():
            run()
        return
    wait_tone.TONE.begin_speech()

    def go():
        try:
            run()
        finally:
            wait_tone.TONE.end_speech()

    threading.Thread(target=go, daemon=True).start()


def play_seq(dev, paths):
    """aplay files in order, off the capture thread. Skips missing ones.

    Claims the speaker before returning. Each file claims it again, so a gap cannot let the chime in.
    """
    files = [p for p in paths if p]
    if not dev or not files:
        return
    wait_tone.TONE.begin_speech()

    def go():
        try:
            for p in files:
                play_file(dev, p, block=True)
        finally:
            wait_tone.TONE.end_speech()

    threading.Thread(target=go, daemon=True).start()


def configured_wav(key):
    """Wav for ALARM_SOUND / LISTEN_TONE / END_TONE, or None for none / unknown."""
    mod = station_sounds()
    if mod is None:
        if key == "LISTEN_TONE" and os.path.isfile(CHIME):
            return CHIME
        return None
    raw = os.environ.get(key, "")
    if not mod.allowed(key, raw):
        raw = mod.default_value(key)
    return mod.file_for(raw)


def preview_wav(sid):
    """Allowlisted preview file, or None. The id is not joined onto a directory by the caller."""
    mod = station_sounds()
    if mod is None or not isinstance(sid, str) or not sid:
        return None
    return mod.file_for(sid)


def on_comms_fail(dev):
    """Fixed fail tone, before the relay speaks its failure line. The file is not chosen by the event."""
    print("relay: comms fail", flush=True)
    mod = station_sounds()
    play_file(dev, mod.fail_file() if mod else None, block=True)


def on_home_tone(dev, ok):
    """Local success or failure tone for a home command. Not spoken, and not a setting."""
    print("relay: home tone", flush=True)
    mod = station_sounds()
    sid = "home-ok" if ok else "home-fail"
    play_file(dev, mod.file_for(sid) if mod else None, block=False)


def _notice_and_chime(label):
    name = label or "timer"
    text = "Your timer is done." if str(name).strip().lower() == "timer" else f"Your {name} timer is done."
    post_state("speaking", who="grok", text=text, done=True)
    dev = LIVE.get("play")
    wav = configured_wav("ALARM_SOUND")
    with wait_tone.speech():
        for _ in range(3):
            if dev and wav:
                play_file(dev, wav, block=True)
            time.sleep(0.4)


def _ring_one(k, v):
    """Mark the chip ringing, chime, then keep it up for RING_HOLD. Late by over an hour: drop quietly."""
    late = -_timer_left(v)
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
            cur["tone_mono"] = time.monotonic()
            cur["tone_at"] = 0.0
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
                cur["ring_mono"] = time.monotonic() + RING_HOLD
                timers_save()


def alarm_repeat_due(elapsed, last):
    """True when a ringing timer should chime again.

    The ring itself is the tone at elapsed 0 (`last` 0). Then every 10 s through 2 minutes,
    then every 60 s. Nothing at or after the one-hour drop.
    """
    try:
        elapsed = float(elapsed)
        last = float(last)
    except (TypeError, ValueError):
        return False
    if elapsed >= 3600 or not elapsed > last:
        return False
    step = 10.0 if last < 120 else 60.0
    nxt = last + step
    if nxt >= 3600:
        return False
    return elapsed + 1e-9 >= nxt


def _tone_repeats(now_m):
    """Replay the alarm on the schedule above. The spoken notice stays the first ring only."""
    due = []
    with TIMERS_LOCK:
        for k, v in list(TIMERS.items()):
            if not isinstance(v, dict) or not v.get("ringing"):
                continue
            started = v.get("tone_mono")
            if isinstance(started, bool) or not isinstance(started, (int, float)):
                continue
            last = v.get("tone_at")
            if isinstance(last, bool) or not isinstance(last, (int, float)):
                last = 0.0
            elapsed = float(now_m) - float(started)
            if alarm_repeat_due(elapsed, last):
                step = 10.0 if float(last) < 120 else 60.0
                due.append((k, float(last) + step))
    if not due:
        return
    dev = LIVE.get("play")
    wav = configured_wav("ALARM_SOUND")
    for k, mark in due:
        if dev and wav:
            play_file(dev, wav, block=True)
        with TIMERS_LOCK:
            cur = TIMERS.get(k)
            if isinstance(cur, dict):
                cur["tone_at"] = mark


def timers_tick():
    """One pass: clear finished rings, start due ones when the speaker is free, then repeat the tone."""
    _expire_rings(time.time())
    with TIMERS_LOCK:
        due = [(k, dict(v)) for k, v in TIMERS.items() if not v.get("ringing") and _timer_left(v) <= 0]
    if due and not LIVE["session"] and LIVE["play"]:
        for k, v in due:
            _ring_one(k, v)
    if not LIVE["session"] and LIVE["play"]:
        _tone_repeats(time.monotonic())


def timers_loop():
    """Ring due timers: the alarm sound three times and a notice. Waits out a conversation so it doesn't talk over it."""
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
        return m if m in ("setup", "reset", "hotspot", "key") else None
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
ENROLL_COUNT, TOPUP_COUNT = 5, 5         # clips per session, fixed; "Train more" records another 5 and merges them
SESSION_OPEN = threading.Event()         # a voice session is on screen: enrollment result notices wait for it


def relay_post(path, body, ctype="application/octet-stream"):
    """Loopback POST. Enrollment no longer uses this; personas still can."""
    base = re.sub(r"^ws", "http", RELAY_URL).rsplit("/ws", 1)[0]
    req = urllib.request.Request(base + path, body, {"Authorization": f"Bearer {relay_token()}", "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=15) as r:
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
    A speaker field from an older client is ignored. Top-up is per phrase, not per voice.
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
    if count != ENROLL_COUNT:
        return {"error": "count"}
    voice = j.get("voice") is True
    topup = bool(j.get("topup"))
    if voice and phrase_trained(phrase):
        topup = True
    out = {"phrase": phrase, "persona": persona, "count": count, "topup": topup}
    if voice:
        out["voice"] = True
    return out


def phrase_trained(phrase):
    """True when this phrase already has a wake word (live map or the enrolled file)."""
    if not isinstance(phrase, str) or not phrase:
        return False
    if phrase in WORDS:
        return True
    for w in _enrolled_doc().get("words") or []:
        if isinstance(w, dict) and w.get("name") == phrase:
            return True
    return False


def _wake_token_path():
    return E("WAKE_CONTROL_TOKEN_FILE", WAKE_TOKEN_FILE)


def _read_wake_token_file():
    """Stripped file bytes, or None. Missing, empty, and non-ascii files are unreadable. Never logged."""
    try:
        with open(_wake_token_path(), "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    return text.encode("ascii")


# The server only trusts a token file that install.sh made: root:grok-wake 0440. Tests swap uid/gid.
WAKE_TOKEN_POLICY = {"uid": 0, "group": "grok-wake", "gid": None, "mode": 0o440}
_WAKE_POLICY_WARNED = False


def _wake_token_file_ok():
    """Owner, group and mode of the token file match WAKE_TOKEN_POLICY. Anything else fails closed."""
    global _WAKE_POLICY_WARNED
    try:
        st = os.stat(_wake_token_path())
        gid = WAKE_TOKEN_POLICY.get("gid")
        if gid is None:
            import grp
            gid = grp.getgrnam(WAKE_TOKEN_POLICY["group"]).gr_gid
        ok = (st.st_uid == WAKE_TOKEN_POLICY["uid"] and st.st_gid == gid
              and (st.st_mode & 0o7777) == WAKE_TOKEN_POLICY["mode"])
    except (OSError, KeyError, ImportError):
        ok = False
    if not ok and not _WAKE_POLICY_WARNED:
        _WAKE_POLICY_WARNED = True
        print("wake control: token file missing or wrong owner/mode; every route returns 403", flush=True)
    return ok


def _load_wake_token(force=False):
    """Token cached at server start. A request re-reads only while that read found nothing.

    A missing, empty, or wrongly owned file loads as empty, so every route is 403.
    """
    if not force and _WAKE_TOKEN["raw"]:
        return _WAKE_TOKEN["raw"]
    tok = (_read_wake_token_file() if _wake_token_file_ok() else None) or b""
    if tok or force:
        _WAKE_TOKEN["raw"] = tok
    return _WAKE_TOKEN["raw"]


def _wake_client_headers(base=None):
    """Header for a call this process makes to the control port. One generic line if the file cannot be read."""
    global _WAKE_CLIENT_WARNED
    headers = dict(base or {})
    tok = _read_wake_token_file()
    if not tok:
        if not _WAKE_CLIENT_WARNED:
            _WAKE_CLIENT_WARNED = True
            print("wake control: token unreadable", flush=True)
    else:
        headers["X-Grok-Wake-Token"] = tok.decode("ascii")
    return headers


def control_server():
    """127.0.0.1:CONTROL_PORT POST /enroll {"phrase","count"}: queue one enrollment (the display's LAN /enroll and
    `grok_wake.py --enroll` both land here). POST /personas/sync re-runs persona and portrait sync in a thread.
    POST /mic-reset asks for a speakerphone USB reset (deferred while a session is open).
    Every route requires X-Grok-Wake-Token. Never reachable from the LAN. The token is never logged."""
    global CONTROL_HTTPD
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    _load_wake_token(force=True)

    class H(BaseHTTPRequestHandler):
        def _r(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def _allow(self):
            """403 before the body is read. compare_digest on the stripped file bytes."""
            tok = _load_wake_token()
            got = self.headers.get("X-Grok-Wake-Token")
            try:
                ok = bool(tok) and isinstance(got, str) and hmac.compare_digest(tok, got.encode("utf-8"))
            except Exception:
                ok = False
            if not ok:
                self._r(403, {"error": "forbidden"})
                return False
            return True

        def do_GET(self):
            if not self._allow():
                return
            if self.path != "/status":
                return self._r(404, {"error": "not found"})
            return self._r(200, {"level_dbfs": LIVE["dbfs"], "level_at": LIVE["at"], "audio_at": LIVE.get("audio_at"), "session": LIVE["session"],
                                 "capture": LIVE["capture"], "reopen_by": LIVE.get("reopen_by"), "reset_until": LIVE.get("reset_until"),
                                 "enrolling": ENROLL_STATE["busy"], "enroll_queued": not ENROLL_Q.empty(), "last_enroll": ENROLL_STATE["last"],
                                 "words": {k: {"persona": w["persona"], "threshold": w["threshold"]} for k, w in WORDS.items()},
                                 "mic_reset": mic_reset_flags(),
                                 "setup": setup_mode()})

        def do_POST(self):
            if not self._allow():
                return
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
            if self.path == "/forget":
                try:
                    n = min(int(self.headers.get("Content-Length", 0) or 0), 1024)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._r(400, {"error": "bad json"})
                ok, err = forget_word(body.get("name") if isinstance(body, dict) else None)
                return self._r(200 if ok else (409 if err == "busy" else 400), {"ok": ok, "error": err})
            if self.path == "/test-sound":
                if LIVE["session"] or ENROLL_STATE["busy"] or not LIVE["play"]:
                    return self._r(409, {"error": "busy"})
                try:
                    n = min(int(self.headers.get("Content-Length", 0) or 0), 1024)
                except ValueError:
                    n = 0
                raw = self.rfile.read(n) if n else b""
                sid = ""
                if raw.strip():
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        return self._r(400, {"error": "bad json"})
                    if not isinstance(body, dict):
                        return self._r(400, {"error": "bad json"})
                    if body.get("id") not in (None, ""):
                        sid = body.get("id") if isinstance(body.get("id"), str) else ""
                        if not sid:
                            return self._r(400, {"error": "unknown sound"})
                if sid:
                    path = preview_wav(sid)
                    if not path:
                        return self._r(400, {"error": "unknown sound"})
                else:
                    path = CHIME
                play_file(LIVE["play"], path)
                return self._r(202, {"ok": True})
            if self.path == "/enroll/control":
                try:
                    n = min(int(self.headers.get("Content-Length", 0) or 0), 1024)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except (ValueError, TypeError):
                    return self._r(400, {"error": "bad json"})
                code, obj = enroll_control(body)
                return self._r(code, obj)
            if self.path == "/enroll/open-tile":
                if train_flow is None:
                    return self._r(503, TRAINING_OFF)
                try:
                    n = min(int(self.headers.get("Content-Length", 0) or 0), 1024)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except (ValueError, TypeError):
                    return self._r(400, {"error": "bad json"})
                code, obj = open_enroll_tile(body)
                return self._r(code, obj)
            if self.path == "/late-reply":
                try:
                    n = min(int(self.headers.get("Content-Length", 0) or 0), 1024)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except (ValueError, TypeError):
                    return self._r(400, {"error": "bad json"})
                if not isinstance(body, dict):
                    return self._r(400, {"error": "bad json"})
                late_id = body.get("id") if isinstance(body.get("id"), str) else ""
                job = load_late_job(late_id)
                action = late_reply_action(late_session_open(), job)
                if action == "busy":
                    return self._r(409, {"error": "busy"})
                if action == "missing":
                    return self._r(404, {"error": "not found"})
                start_late(job, late_id)
                return self._r(200, {"ok": True})
            if self.path == "/mic-reset":
                try:
                    n = min(int(self.headers.get("Content-Length", 0) or 0), 1024)
                except ValueError:
                    n = 0
                if n:
                    self.rfile.read(n)
                rec = RECOVERY
                if rec is None:
                    return self._r(503, {"error": "Couldn't reset the microphone."})
                try:
                    result = rec.request("button")
                except Exception:
                    return self._r(503, {"error": "Couldn't reset the microphone."})
                if result == "off":
                    return self._r(409, {"error": "Reset is turned off."})
                if result == "too_soon":
                    return self._r(409, {"error": "The microphone was just reset. Give it a minute, then try again."})
                if result == "deferred":
                    return self._r(202, {"ok": True, "deferred": True})
                if result == "ok":
                    return self._r(202, {"ok": True, "deferred": False})
                return self._r(503, {"error": "Couldn't reset the microphone."})
            if self.path != "/enroll":
                return self._r(404, {"error": "not found"})
            if train_flow is None:
                return self._r(503, TRAINING_OFF)
            try:
                j = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 1024)) or b"{}")
            except Exception:
                return self._r(400, {"error": "bad json"})
            req = parse_enroll(j)
            if req is None:
                return self._r(400, {"error": "unknown phrase", "phrases": sorted(enroll_phrases())})
            if req.get("error") == "count":
                return self._r(400, {"error": "count must be %d" % ENROLL_COUNT})
            if setup_mode():
                return self._r(423, {"error": "setup needed"})
            if ENROLL_STATE["busy"]:
                return self._r(409, {"error": "enrollment in progress"})
            try:
                ENROLL_Q.put_nowait(req)
            except queue.Full:
                return self._r(409, {"error": "an enrollment is already queued"})
            # Stamp recording before 202. The worker has not set busy yet, and the previous
            # last_enroll is often still failed_quality. The admin page would keep that screen.
            note_enroll_queued(req)
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


def pcm_extremes(samples):
    """(peak abs, clipped count, sample count) for one int16 chunk.

    Peak is the max absolute sample. A sample is clipped when abs >= 32767.
    int16 -32768 is widened before abs so it does not overflow.
    """
    a = np.asarray(samples)
    if a.size == 0:
        return 0, 0, 0
    mag = np.abs(a.astype(np.int32, copy=False))
    return int(mag.max()), int(np.count_nonzero(mag >= 32767)), int(mag.size)


def format_level_line(avg, peak_score, frames, peaks, sample_peak, clipped):
    """One periodic level line. clipped is a fraction, printed with 4 decimals (clipped=0.0012)."""
    dbfs = 20 * np.log10(max(float(avg), 1.0) / 32768)
    items = peaks.items() if isinstance(peaks, dict) else ()
    top = " ".join(f"{k}={v:.3f}" for k, v in sorted(items, key=lambda kv: -kv[1]) if v >= 0.05)
    tail = f" peaks {top}" if top else ""
    return (f"level rms={float(avg):.0f} ({dbfs:.1f} dBFS) peak_score={float(peak_score):.3f} "
            f"frames={int(frames)} peak_sample={int(sample_peak)} clipped={float(clipped):.4f}{tail}")


def last_session(phrase):
    """Newest earlier local session of this phrase, or None. Top-up merges those clips; voice is not tracked."""
    try:
        ses = sorted(x for x in os.listdir(os.path.join(ENROLL_DIR, phrase)) if re.fullmatch(r"\d{8}-\d{6}", x))
    except OSError:
        return None
    return ses[-1] if ses else None


TRAINER_LATER = "Saved. Checking the recordings."  # shown while the station builds the wake word from them
FEWSHOT_NEG = E("WAKE_FEWSHOT_NEG", "/opt/grok-station/wake/models/fewshot_negatives.npz")  # everyday-speech bank
FEWSHOT = {}  # name -> fewshot.Matcher for words built from recordings on this station
FEWSHOT_PATIENCE = 2
_FEAT = {}


def _features():
    """openWakeWord's melspectrogram + embedding models. Reuses the listener's loaded copy (the wake unit has a memory
    cap; a second copy plus a second Hey Grok model got the listener killed mid-build in 0.10.0)."""
    live = _FEAT.get("model")
    if live is not None and getattr(live, "preprocessor", None) is not None:
        return live.preprocessor
    if "af" not in _FEAT:
        from openwakeword.utils import AudioFeatures
        _FEAT["af"] = AudioFeatures(inference_framework="onnx")
    return _FEAT["af"]


def _embed(pcm):
    return _features()._get_embeddings(np.asarray(pcm, np.int16)).astype(np.float32)


def _read_wav(path):
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2")


def note_enroll_queued(req):
    """Report recording as soon as a session is queued, before the worker sets busy.

    session stays empty until enroll() opens the directory. A later stage from the
    previous attempt must not be what the admin page reads in that gap.
    """
    ENROLL_STATE["last"] = {
        "phrase": req["phrase"], "session": None, "count": int(req["count"]), "started": time.time(),
        "persona": req["persona"], "stage": "recording", "message": "Say it on the station.",
        "index": 0, "kept": 0, "phase": "prompt", "topup": bool(req.get("topup")), "recent": [],
    }


def _session_clip_names(directory):
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(fn for fn in names if re.fullmatch(r"\d{2}\.wav", fn))


def _session_fingerprint(directory, names):
    """sha256 of each clip, in name order. None if a clip cannot be read."""
    digest = hashlib.sha256()
    for fn in names:
        digest.update(fn.encode())
        digest.update(b"\0")
        try:
            with open(os.path.join(directory, fn), "rb") as f:
                digest.update(hashlib.sha256(f.read()).digest())
        except OSError:
            return None
    return digest.digest()


def finished_sessions(phrase):
    """Full, unique sessions for one phrase: (directory, clip names, noise path or None).

    A finished session is a YYYYMMDD-HHMMSS directory with a session.json object and at
    least ENROLL_COUNT clip files (NN.wav). A Train more session (prior set) needs 5, which matches
    ENROLL_COUNT, so an older top-up kept at 5 still counts, and a first session of 10 still does too.
    Empty directories, partial sessions, and
    directories with no completion marker are skipped. Two sessions with the same clip
    bytes count once; the earlier directory is kept. Room noise is only from those.
    """
    root = os.path.join(ENROLL_DIR, phrase)
    try:
        sessions = sorted(x for x in os.listdir(root) if re.fullmatch(r"\d{8}-\d{6}", x))
    except OSError:
        return []
    out, seen = [], set()
    for ses in sessions:
        d = os.path.join(root, ses)
        try:
            with open(os.path.join(d, "session.json")) as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        names = _session_clip_names(d)
        # First session: ENROLL_COUNT saved clips. Train more: 5 (prior set). At 5 those are the same,
        # so a word enrolled earlier from 10 clips still counts, and an old top-up of 5 still counts.
        if len(names) < (5 if doc.get("prior") else ENROLL_COUNT):
            continue
        fp = _session_fingerprint(d, names)
        if fp is None or fp in seen:
            continue
        seen.add(fp)
        noise = os.path.join(d, "noise.wav")
        out.append((d, names, noise if os.path.isfile(noise) else None))
    return out


def phrase_clips(phrase):
    """Kept recordings of this phrase (finished unique sessions only), and their room noise."""
    clips, noise = [], []
    for d, names, noise_path in finished_sessions(phrase):
        for fn in names:
            try:
                clips.append(_read_wav(os.path.join(d, fn)))
            except (OSError, EOFError, wave.Error):
                pass
        if noise_path:
            try:
                noise.append(_read_wav(noise_path))
            except (OSError, EOFError, wave.Error):
                pass
    return clips, noise


def _enrolled_doc():
    try:
        with open(os.path.join(ENROLLED_DIR, "wakewords.enrolled.json")) as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) and isinstance(doc.get("words"), list) else {"words": []}
    except (OSError, ValueError):
        return {"words": []}


def _write_enrolled(words):
    os.makedirs(ENROLLED_DIR, exist_ok=True)
    cfgp = os.path.join(ENROLLED_DIR, "wakewords.enrolled.json")
    with open(cfgp + ".tmp", "w") as f:
        f.write(json.dumps({"words": words}, indent=1))
    os.replace(cfgp + ".tmp", cfgp)


def hey_grok_hears(frames):
    """Highest Hey Grok score over these embedding streams, using the listener's loaded Hey Grok model on the frames
    already computed (no second model). A phrase that sets off Hey Grok would collide with it. 0.0 when unavailable."""
    live = _FEAT.get("model")
    sess = getattr(live, "models", {}).get(BUILTIN) if live is not None else None
    if sess is None:
        return 0.0
    top = 0.0
    try:
        name = sess.get_inputs()[0].name
        for f in frames:
            for i in range(16, len(f) + 1):
                out = sess.run(None, {name: f[None, i - 16:i].astype(np.float32)})[0]
                top = max(top, float(np.max(out)))
    except Exception as e:
        print(f"fewshot: Hey Grok check skipped ({type(e).__name__})", flush=True)
    return top


BUILD_STOPPED = "Building the wake word stopped partway. Press Record voice to try again, or delete it."


def _marker(phrase):
    return os.path.join(ENROLL_DIR, phrase, "building.json")


def stale_build():
    """At listener start: a build marker left behind means the listener died while building. Report it as failed."""
    try:
        names = sorted(os.listdir(ENROLL_DIR))
    except OSError:
        return None
    for phrase in names:
        mk = _marker(phrase)
        if not os.path.isfile(mk):
            continue
        try:
            doc = json.load(open(mk))
        except (OSError, ValueError):
            doc = {}
        try:
            os.remove(mk)
        except OSError:
            pass
        print(f"fewshot {phrase}: build did not finish before the listener restarted", flush=True)
        ENROLL_STATE["last"] = {"phrase": phrase, "session": doc.get("session"), "persona": doc.get("persona"),
                                "stage": "failed", "message": BUILD_STOPPED, "started": time.time()}
        return phrase
    return None


def build_fewshot(phrase, persona, session=None):
    """Turn this phrase's recordings into templates + a threshold. Returns the calibration result (ok, message, ...)."""
    mk = _marker(phrase)
    try:
        with open(mk, "w") as f:
            json.dump({"session": session, "persona": persona}, f)
    except OSError:
        mk = None
    try:
        return _build_fewshot(phrase, persona)
    finally:
        if mk:
            try:
                os.remove(mk)
            except OSError:
                pass


def _build_fewshot(phrase, persona):
    import fewshot as fs
    clips, noise = phrase_clips(phrase)
    room = np.concatenate(noise) if noise else None
    padded = [fs.pad_clip(c, room) for c in clips]
    frames = [_embed(x) for x in padded]
    lens = [len(c) / 16000 for c in clips]
    negs = [_embed(room)] if room is not None and len(room) > 16000 else []
    try:
        bank = fs.load(FEWSHOT_NEG)
        negs += bank
    except (OSError, ValueError, KeyError):
        print("fewshot: everyday-speech bank missing; calibrating on room noise only", flush=True)
    others = []
    for w in _enrolled_doc()["words"]:
        if isinstance(w, dict) and w.get("kind") == "fewshot" and w.get("name") != phrase:
            try:
                t = fs.load(w["path"])
                negs += t  # the other words must not set this one off
                om = fs.Matcher(t); om.threshold = float(w.get("threshold", 0.9))
                others.append((str(w["name"]).replace("_", " ").title(), om))
            except (OSError, ValueError, KeyError, TypeError):
                pass
    cal = fs.calibrate(frames, lens, negs, others)
    if cal.get("templates") and BUILTIN != phrase:
        hg = hey_grok_hears(frames)
        cal["hey_grok"] = round(hg, 3)
        if hg >= THRESH:
            cal["ok"] = False
            cal["message"] = "Not good enough yet. It sounds too much like Hey Grok. Try a different phrase."
    print(f"fewshot {phrase}: {'ok' if cal['ok'] else 'not good enough'} heard={cal.get('heard')}/{cal.get('count')} "
          f"threshold={cal.get('threshold')} neg_max={cal.get('neg_max')} templates={len(cal.get('templates') or [])}", flush=True)
    if not cal["ok"]:
        return cal
    path = os.path.join(ENROLLED_DIR, phrase + ".fewshot.npz")
    os.makedirs(ENROLLED_DIR, exist_ok=True)
    fs.save(path + ".tmp.npz", cal["templates"], {})
    os.replace(path + ".tmp.npz", path)
    words = [w for w in _enrolled_doc()["words"] if isinstance(w, dict) and w.get("name") != phrase]
    words.append({"name": phrase, "persona": persona, "threshold": cal["threshold"], "patience": FEWSHOT_PATIENCE,
                  "kind": "fewshot", "path": path, "enrolled": True, "built_at": int(time.time()),
                  "metrics": {"heard": cal["heard"], "count": cal["count"], "templates": len(cal["templates"])}})
    _write_enrolled(words)
    RELOAD.set()
    return cal


def _note_clip(i, kept, phase, why, topup, n):
    """Per-clip progress on the enroll status. Does not post a display state."""
    last = ENROLL_STATE.get("last")
    if not isinstance(last, dict):
        return
    recent = last.get("recent") if isinstance(last.get("recent"), list) else []
    recent = [{"n": int(i), "ok": why == "", "why": str(why)[:24]}] + recent
    last["recent"] = recent[:6]
    last["index"] = int(i)
    last["kept"] = int(kept)
    last["count"] = int(n)
    last["phase"] = phase
    last["topup"] = bool(topup)


_TRAIN_LOCK = threading.Lock()


PROMPT_TTL_S = 120   # prompt tile with nothing recorded closes on its own (Nix 0.13.5 L2)
PAUSE_MAX_S = 600    # a pause between clips longer than this discards the session
TRAIN_MAX_S = 1800   # whole training (prompt plus recording) ends here, however often it is paused and resumed
_mono = time.monotonic


def _training_expire():
    """Bounded listening window. An expired prompt closes; an over-cap pause takes the discard path.

    One log line each, with no words and no phrase.
    """
    now = _mono()
    closed = False
    with _TRAIN_LOCK:
        if ENROLL_STATE.get("prompt") and not ENROLL_STATE.get("busy"):
            until = ENROLL_STATE.get("prompt_until")
            if until is None or now >= until:
                ENROLL_STATE["prompt"] = None
                ENROLL_STATE["prompt_until"] = None
                closed = True
        if ENROLL_STATE.get("busy") and not ENROLL_STATE.get("discard"):
            until = ENROLL_STATE.get("train_until")
            if until is not None and now >= until:
                ENROLL_STATE["discard"] = True
                ENROLL_STATE["pause"] = False
                ENROLL_STATE["pause_until"] = None
                ENROLL_STATE["timed_out"] = True
                print("enroll training time limit reached; discarding", flush=True)
        if ENROLL_STATE.get("busy") and ENROLL_STATE.get("pause"):
            until = ENROLL_STATE.get("pause_until")
            if until is None or now >= until:
                ENROLL_STATE["discard"] = True
                ENROLL_STATE["pause"] = False
                ENROLL_STATE["pause_until"] = None
                ENROLL_STATE["timed_out"] = True
                print("enroll pause timed out; discarding", flush=True)
    if closed:
        print("enroll prompt timed out", flush=True)
        post_state("idle")


def training_tile_up():
    """The training tile is showing: the prompt is waiting or a recording is running."""
    with _TRAIN_LOCK:
        return bool(ENROLL_STATE.get("prompt") or ENROLL_STATE.get("busy"))


def _prompt_tick():
    """Listener loop: close the prompt tile once its TTL has passed."""
    if ENROLL_STATE.get("prompt") and not ENROLL_STATE.get("busy"):
        _training_expire()


def _training_holding():
    """Paused between clips, or the prompt tile is up and nothing is recording yet."""
    _training_expire()
    if ENROLL_STATE.get("discard"):
        return False
    if ENROLL_STATE.get("pause"):
        return True
    return bool(ENROLL_STATE.get("prompt")) and not ENROLL_STATE.get("busy")


def _enroll_hold(read16=None):
    """Pause between clips, or wait on the prompt tile. Stop on the tile or a time limit leaves this.

    The capture thread is blocked here, so the mic has to be drained or the next read stalls.
    """
    while _training_holding():
        if read16 is None:
            time.sleep(0.2)
        else:
            read16()


def _umask_077():
    """Process umask 077 for one file create, then the previous mask. umask is process-wide."""

    class _U:
        def __enter__(self):
            self.prev = os.umask(0o077)
            return self

        def __exit__(self, *exc):
            os.umask(self.prev)

    return _U()


def _mkdir_0700(path):
    with _umask_077():
        os.makedirs(path, mode=0o700, exist_ok=True)
    if not os.path.islink(path):
        os.chmod(path, 0o700)


def _write_wav(path, pcm):
    """One clip, mode 0600. The mode is set on create, not left to a loose umask."""
    data = pcm if isinstance(pcm, (bytes, bytearray)) else bytes(pcm)
    with _umask_077():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        raw = os.fdopen(fd, "wb")
        fd = None
        with raw:
            with wave.open(raw, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(data)
    finally:
        if fd is not None:
            os.close(fd)
    if not os.path.islink(path):
        os.chmod(path, 0o600)


def _write_json_0600(path, body):
    with _umask_077():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        raw = os.fdopen(fd, "w")
        fd = None
        with raw:
            json.dump(body, raw)
    finally:
        if fd is not None:
            os.close(fd)
    if not os.path.islink(path):
        os.chmod(path, 0o600)


def _session_kept(sdir):
    """A finished session: session.json plus enough clips. Partials are not kept."""
    meta = os.path.join(sdir, "session.json")
    if os.path.islink(meta) or not os.path.isfile(meta):
        return False
    try:
        doc = json.load(open(meta))
    except (OSError, ValueError):
        return False
    if not isinstance(doc, dict):
        return False
    try:
        names = [n for n in os.listdir(sdir)
                 if re.fullmatch(r"\d{2}\.wav", n) and os.path.isfile(os.path.join(sdir, n)) and not os.path.islink(os.path.join(sdir, n))]
    except OSError:
        return False
    need = 5 if doc.get("prior") else ENROLL_COUNT
    return len(names) >= need


def sweep_stale_sessions(root=None):
    """At listener start, drop session dirs that never reached a full set of clips.

    A complete session stays so a later top-up can use it. Nothing is logged by phrase.
    """
    root = root or ENROLL_DIR
    try:
        root_real = os.path.realpath(root)
    except OSError:
        return []
    active = None
    if ENROLL_STATE.get("busy"):
        last = ENROLL_STATE.get("last") or {}
        phrase, ses = last.get("phrase"), last.get("session")
        if isinstance(phrase, str) and isinstance(ses, str):
            active = os.path.realpath(os.path.join(root, phrase, ses))
    removed = []
    try:
        phrases = sorted(os.listdir(root))
    except OSError:
        return removed
    for phrase in phrases:
        pdir = os.path.join(root, phrase)
        if os.path.islink(pdir) or not os.path.isdir(pdir):
            continue
        try:
            if os.path.dirname(os.path.realpath(pdir)) != root_real:
                continue
            sessions = sorted(os.listdir(pdir))
        except OSError:
            continue
        parent_real = os.path.realpath(pdir)
        for ses in sessions:
            if not re.fullmatch(r"\d{8}-\d{6}", ses):
                continue
            sdir = os.path.join(pdir, ses)
            if os.path.islink(sdir) or not os.path.isdir(sdir):
                continue
            try:
                real = os.path.realpath(sdir)
            except OSError:
                continue
            if os.path.dirname(real) != parent_real:
                continue
            if active and real == active:
                continue
            if _session_kept(sdir):
                continue
            shutil.rmtree(sdir, ignore_errors=True)
            removed.append(ses)
    if removed:
        print(f"enroll: removed {len(removed)} unfinished sessions", flush=True)
    return removed


def _enroll_prompt_text(phrase, persona, name):
    """The quoted line the prompt tile shows. A phrase wins; otherwise Hey plus the label."""
    if isinstance(phrase, str) and phrase and wake_name_ok(phrase):
        words = phrase.replace("_", " ").title()
        return f"“{words}”"
    label = name if isinstance(name, str) and name.strip() else (persona if isinstance(persona, str) else "")
    label = " ".join(str(label).split())[:40]
    if not label:
        return None
    if not label.casefold().startswith("hey "):
        label = "Hey " + label[:1].upper() + label[1:]
    return f"“{label}”"


def open_enroll_tile(j):
    """Show the station tile at the prompt stage. Does not record and does not create a session dir.

    Body allow-list: persona, phrase, name. Busy enrollment is 409. The phrase is not logged.
    """
    if not isinstance(j, dict) or set(j) - {"persona", "phrase", "name"}:
        return 400, {"error": "bad json"}
    phrase, persona, name = j.get("phrase"), j.get("persona"), j.get("name")
    if phrase is not None and not isinstance(phrase, str):
        return 400, {"error": "bad json"}
    if persona is not None and not isinstance(persona, str):
        return 400, {"error": "bad json"}
    if name is not None and not isinstance(name, str):
        return 400, {"error": "bad json"}
    if not phrase and not persona:
        return 400, {"error": "bad json"}
    if phrase and not wake_name_ok(phrase):
        return 400, {"error": "unknown phrase"}
    if persona and not persona_ok(persona):
        return 400, {"error": "bad persona"}
    say = _enroll_prompt_text(phrase, persona, name)
    if not say:
        return 400, {"error": "bad json"}
    who = persona if persona and persona_ok(persona) else "grok"
    with _TRAIN_LOCK:
        if ENROLL_STATE.get("busy"):
            return 409, {"error": "enrollment in progress"}
        ENROLL_STATE["prompt"] = {"phrase": phrase or "", "persona": who, "text": say}
        ENROLL_STATE["prompt_until"] = _mono() + PROMPT_TTL_S
        ENROLL_STATE["train_until"] = _mono() + TRAIN_MAX_S
    post_state("enroll", text=say, who=who, heard=f"1/{ENROLL_COUNT}|prompt|")
    return 200, {"ok": True}


def enroll_control(body):
    """pause, resume, or discard. Extra keys are rejected. Discard of the prompt tile does not need a recording."""
    if not isinstance(body, dict) or set(body) != {"op"} or body.get("op") not in ("pause", "resume", "discard"):
        return 400, {"error": "bad op"}
    op = body["op"]
    with _TRAIN_LOCK:
        if not ENROLL_STATE.get("busy"):
            if op == "discard" and ENROLL_STATE.get("prompt"):
                ENROLL_STATE["prompt"] = None
                post = True
            else:
                return 409, {"error": "Nothing is recording."}
        elif op == "pause":
            if not ENROLL_STATE.get("pause"):
                ENROLL_STATE["pause_until"] = _mono() + PAUSE_MAX_S
            ENROLL_STATE["pause"] = True
            post = False
        elif op == "resume":
            ENROLL_STATE["pause"] = False
            ENROLL_STATE["pause_until"] = None
            post = False
        else:
            ENROLL_STATE["discard"] = True
            ENROLL_STATE["pause"] = False
            post = False
    if post:
        post_state("idle")
    return 200, {"ok": True, "op": op}


def chime_length_samples(path=None, rate=16000):
    """Samples of the enrollment chime at 16 kHz. The generated chime is two 0.1 s tones."""
    path = path or CHIME
    try:
        with wave.open(path) as w:
            n, sr = w.getnframes(), w.getframerate() or rate
            if sr != rate and sr:
                n = int(round(n * rate / sr))
            if n > 0:
                return n
    except (OSError, EOFError, wave.Error):
        pass
    return int(0.2 * rate)


def capture_enroll_clip(read16, play, chime_samples=None):
    """Mic audio from the moment the chime starts. Drop only the chime. Keep CLIP_S of what follows.

    aplay is waited on, so the cut is the audio captured while it ran (startup included). A player
    that returns immediately, or a test stub, falls back to the known chime length instead of throwing
    away a fixed number of frames. Speech that starts just after the chime stays in the clip.
    """
    speech_n = int(round(CLIP_S * 16000))
    known = chime_length_samples() if chime_samples is None else int(chime_samples)

    def take(n):
        frames, got = [], 0
        while got < n:
            fr = np.asarray(read16(raw=True), dtype=np.int16).reshape(-1)
            frames.append(fr)
            got += int(fr.shape[0])
        return frames, got

    with wait_tone.speech():
        proc = subprocess.Popen(["aplay", "-q", "-D", play, CHIME], stderr=subprocess.DEVNULL)
        if proc is None or not hasattr(proc, "poll"):
            frames, _got = take(speech_n)
            return np.asarray(np.concatenate(frames), dtype=np.int16).reshape(-1)[:speech_n]
        frames, got = [], 0
        while proc.poll() is None and got < 16000 * 3:
            fr = np.asarray(read16(raw=True), dtype=np.int16).reshape(-1)
            frames.append(fr)
            got += int(fr.shape[0])
        chime_end = got
        if chime_end < known:
            more, extra = take(known - chime_end)
            frames.extend(more)
            got += extra
            chime_end = known
        more, extra = take(speech_n)
        frames.extend(more)
        raw = np.asarray(np.concatenate(frames), dtype=np.int16).reshape(-1)
        return raw[chime_end:chime_end + speech_n]


def enroll(req, read16, play):
    """Record req["count"] clips onto this Pi. Training does not run here."""
    if train_flow is None:
        return
    phrase, persona, n = req["phrase"], req["persona"], req["count"]
    words = phrase.replace("_", " ").title()
    say = f"“{words}”"
    prior = last_session(phrase) if req.get("topup") else None
    ses = time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(ENROLL_DIR, phrase, ses)
    _mkdir_0700(os.path.join(ENROLL_DIR, phrase))
    _mkdir_0700(d)
    with _TRAIN_LOCK:
        # The cap starts once: at the prompt tile if it was up, otherwise now.
        started = ENROLL_STATE.get("train_until") if ENROLL_STATE.get("prompt") else None
        ENROLL_STATE["train_until"] = started if started is not None else _mono() + TRAIN_MAX_S
        ENROLL_STATE["prompt"] = None
        ENROLL_STATE["busy"] = True
        ENROLL_STATE["pause"] = False
        ENROLL_STATE["discard"] = False
        ENROLL_STATE["timed_out"] = False
        ENROLL_STATE["pause_until"] = None
    show = lambda i, stage, extra="": post_state("enroll", text=say, who=persona, heard=f"{i}/{n}|{stage}|{extra}")
    voice = req.get("voice") is True
    label = train_flow.wake_speak_label(phrase, persona)
    ENROLL_STATE["last"] = {"phrase": phrase, "session": ses, "count": n, "started": time.time(),
                             "persona": persona, "stage": "recording", "message": "Say it on the station.",
                             "index": 0, "kept": 0, "phase": "prompt", "topup": bool(prior), "recent": []}
    print(f"enroll start {phrase} x{n} session {ses}{f' (top-up of {prior})' if prior else ''}", flush=True)
    sent, i, tries, silent = 0, 1, 0, 0
    try:
        # A voice start is already past the prompt. A tap still shows it, then the same 3-2-1.
        if voice:
            show(1, "ready")
        else:
            show(1, "prompt")
            room = np.vstack([read16() for _ in range(int(3 / 0.08))]).ravel()  # room noise: a negative for calibration
            try:
                _write_wav(os.path.join(d, "noise.wav"), room.astype("<i2").tobytes())
            except OSError:
                pass
        while i <= n:
            if ENROLL_STATE.get("discard"):
                break
            _enroll_hold(read16)
            if ENROLL_STATE.get("discard"):
                break
            show(i, "ready")
            for _ in range(int(LEAD_S / 0.08)):
                read16()
            if i == 1:
                for num in (3, 2, 1):
                    if ENROLL_STATE.get("discard"):
                        break
                    show(i, "count", str(num))
                    for _ in range(int(1.0 / 0.08)):
                        read16()
            if ENROLL_STATE.get("discard"):
                break
            show(i, "recording")
            raw = capture_enroll_clip(read16, play)
            clipped = is_clipped(raw)  # judged on the mic signal before software gain
            pcm = np.clip(raw.astype(np.float32) * GAIN, -32768, 32767).astype("<i2")
            clip = trim_clip(pcm)
            if clip is None and not clipped:
                # One silent clip retries. A second silent clip in a row stops; nothing is saved.
                silent += 1
                decision = silent_clip(silent, label, voice=voice)
                print(f"enroll clip {i} rejected (quiet)", flush=True)
                show(i, "quiet")
                _note_clip(i, sent, "quiet", "quiet", bool(prior), n)
                if decision["action"] == "stop":
                    ENROLL_STATE["silent_stop"] = (decision["say"], decision["station"])
                    break
                for _ in range(int(RETRY_S / 0.08)):
                    read16()
                continue
            if clipped or clip is None:
                silent = 0
                tries += 1
                why = "loud" if clipped else "quiet"
                print(f"enroll clip {i} rejected ({'clipped' if clipped else 'quiet'})", flush=True)
                show(i, why)
                _note_clip(i, sent, why, why, bool(prior), n)
                for _ in range(int(RETRY_S / 0.08)):
                    read16()
                if tries >= 3 * n:
                    break
                continue
            silent = 0
            path = os.path.join(d, f"{i - 1:02d}.wav")
            _write_wav(path, clip.astype("<i2").tobytes())
            sent += 1
            _note_clip(i, sent, "saved", "", bool(prior), n)
            if i < n:
                show(i, "saved", "next")
                for _ in range(int(1.0 / 0.08)):
                    read16()
                show(i, "saved")
                for _ in range(int((RETRY_S - 1.0) / 0.08)):
                    read16()
            i += 1
        if ENROLL_STATE.get("silent_stop"):
            msg, station_msg = ENROLL_STATE.pop("silent_stop")
            shutil.rmtree(d, ignore_errors=True)
            enroll_stage("failed", msg, phrase, ses)  # admin dialog line
            post_state("notice", text=station_msg)  # station tile: the voice restart line
            return
        if ENROLL_STATE.get("discard"):
            timed_out = bool(ENROLL_STATE.pop("timed_out", False))
            shutil.rmtree(d, ignore_errors=True)
            enroll_stage("cancelled", CANCEL_SAY, phrase, ses)
            if timed_out:
                post_state("idle")  # the pause cap already logged its one line
            else:
                print(f"enroll discarded {phrase} session {ses}", flush=True)
            return
        ok = sent >= (5 if prior else ENROLL_COUNT)
        body = {"phrase": phrase, "persona": persona, "count": sent, **({"prior": prior} if prior else {})}
        try:
            _write_json_0600(os.path.join(d, "session.json"), body)
        except OSError as e:
            print(f"enroll save failed: {type(e).__name__}", flush=True)
            ok = False
        print(f"enroll end {phrase}: {i - 1} recorded, {sent} saved, {'kept' if ok else 'NOT kept'}", flush=True)
        if ok:
            enroll_stage("training", TRAINER_LATER, phrase, ses)
            post_state("enroll", text=say, who=persona, heard=f"{sent}/{sent}|saved|")
            try:
                cal = build_fewshot(phrase, persona, ses)
            except Exception as e:
                print(f"fewshot {phrase} build failed: {type(e).__name__}: {e}", flush=True)
                cal = {"ok": False, "error": True,
                       "message": "Couldn't build the wake word on the station. Press Record voice to try again."}
            msg = cal["message"] if cal.get("ok") or cal.get("error") or cal["message"].startswith("Not good enough") \
                else "Not good enough yet. " + cal["message"]
            enroll_stage("published" if cal.get("ok") else "failed" if cal.get("error") else "failed_quality", msg, phrase, ses)
            threading.Thread(target=follow_training, args=(phrase, ses, say, persona, bool(cal.get("ok"))), daemon=True).start()
        else:
            enroll_stage("failed", "Couldn't save the clips.", phrase, ses)
            post_state("enroll", text=say, who=persona, heard=f"0/{n}|error|")
        time.sleep(6)
    except Exception as e:
        # Capture ended or a read failed (mic unplugged): never leave the word saying "recording".
        # A partial session is not resumable from a crash here, so it does not stay on disk.
        print(f"enroll stopped: microphone {type(e).__name__}", flush=True)
        if (ENROLL_STATE.get("last") or {}).get("stage") in ("recording", "ready", "count"):
            enroll_stage("failed", mic_stopped_line(label), phrase, ses)
        shutil.rmtree(d, ignore_errors=True)
        if train_flow is not None and (ENROLL_STATE.get("last") or {}).get("stage") == "failed":
            post_state("notice", text=train_flow.mic_stopped_station_line(station_bot_name(persona, label)))  # leaves enroll mode
        else:
            post_state("idle")  # take the display out of enroll mode
        raise
    finally:
        ENROLL_STATE["busy"] = False


def training_notice(phrase, published):
    """Display notice when a training job finishes. hey_finn → "Hey Finn is ready"."""
    words = str(phrase or "").replace("-", " ").replace("_", " ").split()
    label = " ".join((w[:1].upper() + w[1:]) for w in words if w) or "Hey"
    if published:
        return f"{label} is ready"
    return f"{label} is not good enough yet. Train more to add 5 more recordings, somewhere quiet."


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


def follow_training(phrase, ses, say, persona, published=False, every=60, limit=4 * 3600):
    """Tell the screen how the wake word came out, once no voice session is on it."""
    while SESSION_OPEN.is_set():
        time.sleep(min(2, every or 2))
    post_state("notice", text=training_notice(phrase, published), who=persona)


def load_dropped_wakewords(directory=None):
    """onnx + json in the drop dir. A sha256 mismatch is rejected and not loaded."""
    import hashlib
    directory = directory or DROP_DIR
    accepted = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return accepted
    for fn in names:
        if not fn.endswith(".json"):
            continue
        stem = fn[:-5]
        try:
            with open(os.path.join(directory, fn)) as f:
                doc = json.load(f)
        except (OSError, ValueError):
            print(f"wakeword {stem}: checksum mismatch", flush=True)
            continue
        if not isinstance(doc, dict):
            print(f"wakeword {stem}: checksum mismatch", flush=True)
            continue
        name, persona = str(doc.get("name", stem)), str(doc.get("persona", ""))
        sha = str(doc.get("sha256", "")).lower()
        if name != stem or not (phrase_ok(name) or wake_name_ok(name)) or not persona_ok(persona):
            print(f"wakeword {stem}: checksum mismatch", flush=True)
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            print(f"wakeword {name}: checksum mismatch", flush=True)
            continue
        op = os.path.join(directory, name + ".onnx")
        try:
            blob = open(op, "rb").read()
        except OSError:
            print(f"wakeword {name}: checksum mismatch", flush=True)
            continue
        if hashlib.sha256(blob).hexdigest() != sha:
            print(f"wakeword {name}: checksum mismatch", flush=True)
            continue
        try:
            threshold = float(doc.get("threshold", 0.5))
            patience = int(doc.get("patience", 1))
        except (TypeError, ValueError):
            print(f"wakeword {name}: checksum mismatch", flush=True)
            continue
        accepted.append({"name": name, "persona": persona, "threshold": threshold, "patience": patience,
                         "path": op, "enrolled": True, "sha256": sha})
    return accepted


def forget_word(name):
    """Delete one wake word's recorded clips and its trained model on this Pi, then reload. Never hey_grok.

    Returns (ok, error). Shipped models under models_dir are not touched; the admin hides those.
    """
    if name == BUILTIN or not wake_name_ok(name):
        return False, "bad name"
    if ENROLL_STATE["busy"] and (ENROLL_STATE.get("last") or {}).get("phrase") == name:
        return False, "busy"
    if (ENROLL_STATE.get("last") or {}).get("phrase") == name:
        ENROLL_STATE["last"] = None  # or /status keeps reporting it and the admin re-creates the row
    shutil.rmtree(os.path.join(ENROLL_DIR, name), ignore_errors=True)
    for fn in (name + ".onnx", name + ".json", name + ".fewshot.npz"):
        try:
            os.remove(os.path.join(ENROLLED_DIR, fn))
        except OSError:
            pass
    cfgp = os.path.join(ENROLLED_DIR, "wakewords.enrolled.json")
    try:
        with open(cfgp) as f:
            doc = json.load(f)
        words = [w for w in doc.get("words", []) if isinstance(w, dict) and w.get("name") != name]
        if len(words) != len(doc.get("words", [])):
            with open(cfgp + ".tmp", "w") as f:
                f.write(json.dumps({"words": words}, indent=1))
            os.replace(cfgp + ".tmp", cfgp)
    except (OSError, ValueError, AttributeError):
        pass
    RELOAD.set()
    return True, None


def sync_wakewords(directory=None):
    """Load models dropped into the wakewords directory. Sets RELOAD when the accepted set changes."""
    words = load_dropped_wakewords(directory)
    try:
        os.makedirs(ENROLLED_DIR, exist_ok=True)
    except OSError:
        if words:
            RELOAD.set()
        return words
    cfgp = os.path.join(ENROLLED_DIR, "wakewords.enrolled.json")
    old = open(cfgp).read() if os.path.isfile(cfgp) else ""
    dropped = {w["name"] for w in words}  # an uploaded .onnx replaces a recorded word of the same name
    words = [w for w in _enrolled_doc()["words"] if isinstance(w, dict) and w.get("kind") == "fewshot"
             and w.get("name") not in dropped] + words
    new = json.dumps({"words": words}, indent=1)
    if new != old:
        try:
            with open(cfgp + ".tmp", "w") as f:
                f.write(new)
            os.replace(cfgp + ".tmp", cfgp)
        except OSError as e:
            print(f"wakeword list: {type(e).__name__}", flush=True)
        RELOAD.set()
    return words


WAKE_CONFIG = E("WAKE_CONFIG", "/opt/grok-station/wake/wakewords.json")
SLUG_RE = re.compile(r"^[a-z]{2,16}$")
WORDS = {}  # model key -> {"name", "persona", "threshold", "patience", "provisional"}


def _word(w):
    """Settings the detector uses. Extra stored fields, including an old speaker id, are not copied."""
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
        if o.get("enabled") is False and name != BUILTIN:
            entries.pop(name, None)
            print(f"wake word {name}: turned off on the admin page", flush=True)
            continue
        if (isinstance(o.get("threshold"), (int, float)) and 0.05 <= float(o["threshold"]) <= 0.99
                and entries[name].get("kind") != "fewshot"):  # a recorded word keeps its calibrated threshold
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
        name = os.path.basename(MODEL).rsplit(".", 1)[0]
        WORDS[name] = _word({"name": name, "persona": "grok", "threshold": THRESH})
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
    FEWSHOT.clear()
    for name in [n for n, raw in entries.items() if raw.get("kind") == "fewshot"]:
        raw = entries.pop(name)
        try:
            import fewshot as fs
            m = fs.Matcher(fs.load(str(raw["path"])))
        except Exception as e:
            print(f"wake word {name}: recordings unreadable ({type(e).__name__}), skipped", flush=True)
            continue
        FEWSHOT[name] = m
        WORDS[name] = _word(dict(raw, patience=raw.get("patience", FEWSHOT_PATIENCE)))
        WORDS[name]["kind"] = "fewshot"
    for raw in entries.values():
        w = _word(raw)
        path = raw.get("path") or os.path.join(cfg.get("models_dir", ""), w["name"] + ".onnx")
        if not os.path.isfile(path):
            print(f"wake word {w['name']}: model missing, skipped", flush=True)
            continue
        paths.append(path); WORDS[w["name"]] = w
    if not paths:  # nothing usable in the config: the built-in word alone
        w = _word({"name": BUILTIN, "persona": "grok", "threshold": THRESH})
        paths.append(os.path.join(cfg.get("models_dir", "") or os.path.dirname(MODEL), BUILTIN + ".onnx")); WORDS[BUILTIN] = w
    model = Model(wakeword_models=paths, inference_framework="onnx")
    for k in list(WORDS):
        if k not in model.models and k not in FEWSHOT:
            print(f"wake word {k}: not in loaded models, skipped", flush=True)
            WORDS.pop(k)
    for k, w in WORDS.items():
        print(f"wake word {k} -> {w['persona']} threshold={w['threshold']} patience={w['patience']}"
              f"{' (provisional)' if w['provisional'] else ''}{' (enrolled)' if w['enrolled'] else ''}", flush=True)
    print(f"loaded {len(WORDS)} wake words", flush=True)
    _FEAT["model"] = model
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


def station_bot_name(persona, label):
    """Display name for station copy; the wake word label on a lookup miss, never Grok for another persona."""
    name = persona_name(persona)
    if name == "Grok" and persona != "grok":
        return label or name
    return name


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


THINKING_BLANK = " "  # thinking screen: spinning ring only, no dots (truthy so the display keeps no fallback text)
HANDOFF_KEEPALIVE_S = 8  # well under the display's 20 s thinking hold; a teammate can take a few minutes
LATE_DIR = E("GROK_LATE_DIR", "/var/lib/grok-station/late")  # not voice/: that tree is grok-admin only
_LATE_ID = re.compile(r"^vs_[A-Za-z0-9_-]{22}$")
_LATE_PCM = re.compile(r"^vs_[A-Za-z0-9_-]{22}\.pcm$")
LATE_PLAYING = {"on": False}


def handoff_wait_text(ev):
    """Wait-screen label. Direct wake (a bot's own word) shows only the spinning ring; a bot-to-bot handoff names the bot.
    A relay that doesn't send `direct` gets the old label."""
    if ev.get("direct") is True:
        return THINKING_BLANK
    name = str(ev.get("name", ""))[:24] or "a teammate"
    return f"Checking with {name}…"


CUE_B64_MAX = 400000  # same cap the old still-asking clip used


def decode_cue_audio(ev):
    """PCM from an ack or handoff_still message, or empty bytes. Invalid base64 is empty."""
    try:
        return base64.b64decode(str(ev.get("audio", ""))[:CUE_B64_MAX], validate=True)
    except ValueError:
        return b""


def ack_played_line(t_end_speech, now):
    """Journal line. t_end_speech is time.time() when speech_stopped arrived."""
    if not t_end_speech:
        return None
    return f"ack played {now - t_end_speech:.2f}s after end of speech"


def handoff_keepalive_due(s):
    return bool(getattr(s, "active", False) and getattr(s, "handoff_text", None) and not getattr(s, "t_first_audio", None))


def late_session_open():
    """True while a conversation, an enrollment, or a late line is already using the speaker."""
    sess = CURRENT.get("session")
    return bool(LIVE["session"] or ENROLL_STATE["busy"] or LATE_PLAYING["on"]
                or (sess is not None and getattr(sess, "active", False)))


def load_late_job(late_id):
    """The job file for one request id, or None. A pcm name that is not that id is ignored."""
    if not _LATE_ID.fullmatch(str(late_id or "")):
        return None
    try:
        with open(os.path.join(LATE_DIR, late_id + ".json")) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("text"), str) or not doc["text"].strip():
        return None
    who = doc.get("who") if isinstance(doc.get("who"), str) else ""
    pcm = doc.get("pcm") if isinstance(doc.get("pcm"), str) else ""
    if pcm and not _LATE_PCM.fullmatch(pcm):
        pcm = ""
    return {"text": doc["text"].strip(), "who": " ".join(who.split())[:40], "pcm": pcm}


def late_reply_action(session_open, job):
    """'busy', 'missing', 'play', or 'show'. Play when a pcm file name came with the job."""
    if session_open:
        return "busy"
    if not job:
        return "missing"
    return "play" if job.get("pcm") else "show"


def _forget_late_job(late_id, pcm):
    for name in (late_id + ".json", pcm or ""):
        if not name:
            continue
        try:
            os.unlink(os.path.join(LATE_DIR, name))
        except OSError:
            pass


def play_late(job):
    """Show the line, play PCM when there is some, then go back to idle. TTS failure still shows the text."""
    post_state("speaking", text=job.get("text") or "", who=job.get("who") or "")
    pcm_name, dev = job.get("pcm") or "", LIVE.get("play")
    if pcm_name and dev and _LATE_PCM.fullmatch(pcm_name):
        try:
            with open(os.path.join(LATE_DIR, pcm_name), "rb") as f:
                pcm = f.read()
            if pcm:
                p = subprocess.Popen(["aplay", "-q", "-D", dev, "-f", "S16_LE", "-r", "24000", "-c", "1", "-t", "raw"],
                                     stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                p.communicate(pcm)
        except Exception:
            pass
    post_state("idle")


def start_late(job, late_id):
    """Speak on a thread so the control request can return. A second late reply gets 409 until this ends."""
    LATE_PLAYING["on"] = True

    def run():
        try:
            play_late(job)
        finally:
            LATE_PLAYING["on"] = False
            _forget_late_job(late_id, job.get("pcm"))

    threading.Thread(target=run, daemon=True).start()


def post_state(state=None, **kw):
    """POST /state. The log line is the state and the kinds of attachment (card / timer count), never the values.

    A voice state is the one thinking phase: the display ring and the waiting chime both follow it.
    """
    if state:
        try:
            wait_tone.TONE.note_phase(state)
        except Exception as exc:
            print(f"wait tone: {type(exc).__name__}", flush=True)
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


class SpeechClock:
    """0.12.16: how far one streamed reply has played, for the display's reply window.

    The relay streams 24 kHz mono PCM16 (48000 bytes a second) and aplay plays it from the first chunk, so the
    spoken position is the time since the first chunk, capped at the audio received so far. Progress is that
    over the audio received. There are no per-word timings in this pipeline."""

    BYTES_PER_S = 48000

    def __init__(self, t0):
        self.t0, self.bytes = float(t0), 0

    def add(self, n):
        self.bytes += int(n)

    def progress(self, now):
        total = self.bytes / self.BYTES_PER_S
        if total <= 0:
            return 0.0
        return max(0.0, min(1.0, (now - self.t0) / total))


SPEECH_HZ = 5  # the display asks for progress at 5 Hz or more


def post_speech(p):
    """POST {"speech": {"p": …}} to /state. Not logged: it runs five times a second while a reply plays."""
    try:
        req = urllib.request.Request(STATE_URL, json.dumps({"speech": {"p": round(float(p), 4)}}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=0.5).read()
    except Exception:
        pass


def speech_ticker(clock, stop, post=post_speech, hz=SPEECH_HZ):
    """Post the clock's progress hz times a second until stop is set. Runs in its own thread."""
    while not stop.wait(1.0 / hz):
        post(clock.progress(time.time()))


def scores(model, pcm):
    """openWakeWord scores, plus a score for each recorded word from the same 80 ms embedding Hey Grok just used."""
    sc = model.predict(pcm)
    if FEWSHOT:
        sc = dict(sc)
        e = model.preprocessor.feature_buffer[-1].astype(np.float32)
        e = e / (np.linalg.norm(e) + 1e-6)
        for k, m in FEWSHOT.items():
            sc[k] = m.feed(e)
    return sc


def reset_detect(model):
    model.reset()
    for m in FEWSHOT.values():
        m.reset()


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


def bench(seconds=30, path=None):
    model = load_model()
    x = (np.random.randn(16000 * seconds) * 1000).astype("<i2")
    if path:  # recorded audio instead of noise: a 16 kHz mono WAV
        with wave.open(path) as w:
            x = np.frombuffer(w.readframes(w.getnframes()), "<i2")
        while len(x) < 16000 * 4:
            x = np.concatenate([x, x])
    for i in range(0, 16000 * 2, CHUNK):
        scores(model, x[i:i + CHUNK])
    t, n = time.process_time(), 0
    for i in range(0, len(x) - CHUNK, CHUNK):
        scores(model, x[i:i + CHUNK]); n += 1
    ms = (time.process_time() - t) / n * 1000
    fs_ms = 0.0
    if FEWSHOT:  # the template match alone, on the same frames
        e = model.preprocessor.feature_buffer[-1].astype(np.float32)
        t = time.process_time()
        for _ in range(n):
            for m in FEWSHOT.values():
                m.feed(e)
        fs_ms = (time.process_time() - t) / n * 1000
    print(f"bench: {len(WORDS)} words ({len(FEWSHOT)} recorded), {ms:.2f} ms CPU per 80 ms frame = "
          f"{ms / 80 * 100:.1f}% of one core; recorded-word matching {fs_ms:.3f} ms of that")


class Session:
    """One question and answer through the relay. Feed it 24 kHz PCM16 bytes with feed(); runs in a thread."""

    def __init__(self, play_dev, save=None, skip=VOICE_SKIP, echo_text=False, persona="grok"):
        self.q, self.feeding, self.active = queue.Queue(maxsize=400), True, True
        self.handoff_text = None  # set while a teammate handoff wait screen is up
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
        self._connected, self._end_played = False, False
        self._reply_held = False  # thinking chime stays down for the whole reply playback
        self._cue_q, self._cue_thread, self.t_ack = None, None, None  # ack and handoff_still share one player
        self.speech, self._speech_stop = None, None  # playback clock for the display's reply window
        CURRENT["session"] = self
        threading.Thread(target=self.run, daemon=True).start()

    def start_speech_clock(self, t0):
        """First audio of a reply: report its playback progress to the display until playback ends."""
        self.stop_speech_clock()
        self.speech, self._speech_stop = SpeechClock(t0), threading.Event()
        threading.Thread(target=speech_ticker, args=(self.speech, self._speech_stop), daemon=True).start()

    def stop_speech_clock(self):
        if getattr(self, "_speech_stop", None) is not None:
            self._speech_stop.set()
        self.speech, self._speech_stop = None, None

    def state(self, state, **kw):
        if getattr(self, "card", None) and "card" not in kw:
            kw["card"] = self.card
        if self.t1_card and state in ("speaking", "thinking", "listening"):  # keep the read-back card up until decided
            c = self.t1_card
            post_state("confirm", who=c["name"], text=c["text"], heard=f"{c['op']}|{c['list']}|{1 if c['needs_tap'] else 0}")
            return
        post_state(state, who=self.who, **kw)

    def play_end_tone(self):
        """One end tone after the reply has finished. Default is silence."""
        if self._end_played or not self._connected:
            return
        self._end_played = True
        play_file(self.play_dev, configured_wav("END_TONE"), block=True)

    def play_cue(self, pcm):
        """Queue one 24 kHz PCM clip. ack and handoff_still share this player, so they never overlap.
        Does not mark the screen as speaking. The next clip starts only after this one finishes."""
        if not pcm:
            return
        if self._cue_q is None:
            self._cue_q = queue.Queue()
            self._cue_thread = threading.Thread(target=self._cue_loop, args=(self._cue_q,), daemon=True)
            self._cue_thread.start()
        self._cue_q.put(bytes(pcm))

    def _claim_reply(self):
        if self._reply_held:
            return
        self._reply_held = True
        wait_tone.TONE.begin_speech()

    def _release_reply(self):
        if not self._reply_held:
            return
        self._reply_held = False
        wait_tone.TONE.end_speech()

    def _cue_loop(self, q):
        while True:
            pcm = q.get()
            if not pcm:
                return
            # Asking clips and still-asking clips are speech. The ring stays on thinking.
            with wait_tone.speech():
                try:
                    p = subprocess.Popen(["aplay", "-q", "-D", self.play_dev, "-f", "S16_LE", "-r", "24000", "-c", "1",
                                          "-t", "raw"], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    p.communicate(pcm)
                except Exception:
                    pass

    def close_cue(self):
        q = self._cue_q
        if q is not None:
            try:
                q.put_nowait(None)
            except Exception:
                pass

    def start_handoff_keepalive(self):
        """The display drops a "thinking" screen after VOICE_HOLDS["thinking"] (20 s) without a new post,
        but a teammate can take a few minutes. Re-post the wait screen until audio, handoff_done or session end."""
        if getattr(self, "_ka", None) and self._ka.is_alive():
            return
        def loop():
            while True:
                time.sleep(HANDOFF_KEEPALIVE_S)
                if not handoff_keepalive_due(self):
                    return
                self.state("thinking", heard=self.heard, text=self.handoff_text)
        self._ka = threading.Thread(target=loop, daemon=True)
        self._ka.start()

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
        builtin_hold = False  # the built-in Grok line: notice for 4 s, not a reply hold and not idle
        try:
            for attempt in (1, 2):  # one retry: a cold mDNS lookup can stall the first connect
                try:
                    q = [f"persona={self.persona}"] if self.persona != "grok" else []
                    tq = timers_query()
                    # Always present, even when empty, so the relay can tell "none" from "not sent".
                    q.append("timers=" + (tq if tq else urllib.parse.quote("[]")))
                    q.append("t1=" + t1_query())
                    url = RELAY_URL + ("?" + "&".join(q) if q else "")
                    ws = connect(url, additional_headers={"Authorization": f"Bearer {relay_token()}"},
                                 open_timeout=4, max_size=1 << 22, legacy=True)
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
                self._connected = True
                self.ws = ws
                threading.Thread(target=self._sender, args=(ws,), daemon=True).start()
                for m in ws:
                    if isinstance(m, bytes):
                        if self.t_first_audio is None:
                            self.feeding, self.t_first_audio = False, time.time()
                            lat = self.t_first_audio - (self.t_end_speech or self.t_first_audio)
                            print(f"first audio {lat:.2f}s after end of speech", flush=True)
                            self.state("speaking", heard=self.heard, text=self.reply or "…")
                            self._claim_reply()  # chime is already stopped; hold it until this player exits
                            player = subprocess.Popen(["aplay", "-q", "-D", self.play_dev, "-f", "S16_LE", "-r", "24000",
                                                       "-c", "1", "-t", "raw"], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                            self.start_speech_clock(self.t_first_audio)
                        if self.speech is not None:
                            self.speech.add(len(m))
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
                                self.state("thinking", heard=self.heard, text=THINKING_BLANK)
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
                            self.state("speaking" if self.t_first_audio else "thinking", heard=self.heard, text=self.reply or ("…" if self.t_first_audio else THINKING_BLANK))
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
                        self.handoff_text = handoff_wait_text(ev)
                        self.state("thinking", heard=self.heard, text=self.handoff_text)
                        self.start_handoff_keepalive()
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
                    elif t == "ack":
                        # Cached "Asking <name>". Same player as handoff_still; the screen stays where it is.
                        line = ack_played_line(self.t_end_speech, time.time())
                        if line:
                            print(line, flush=True)
                        self.t_ack = time.monotonic()
                        self.play_cue(decode_cue_audio(ev))
                    elif t == "handoff_still":
                        # A still-asking clip waits until the ack on this player has finished.
                        print("relay: handoff still", flush=True)
                        self.play_cue(decode_cue_audio(ev))
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
                                kw["text"] = THINKING_BLANK if phase == "thinking" else "…"
                            self.state(phase, **kw)
                    elif t == "handoff_done":
                        self.handoff_text = None
                        print("relay: handoff done", flush=True)
                    elif t == "xai_error":
                        print("relay: xai error", flush=True)
                    elif t == "turn_done":
                        # multi-turn: let the reply finish playing, then listen for a follow-up (no wake word)
                        if player:
                            player.stdin.close(); player.wait(timeout=90); player = None
                            self._release_reply()
                        self.stop_speech_clock()
                        self.turns += 1
                        print(f"relay: turn {self.turns} done, listening for a follow-up", flush=True)
                        while not self.q.empty():  # mic audio queued during playback is the reply's echo
                            self.q.get_nowait()
                        self.heard, self.reply, self.t_first_audio, self.t_end_speech = "", "", None, None
                        self.responding, self.feeding = False, True
                        self.state("listening")
                        ws.send(json.dumps({"type": "listening"}))
                    elif t == "comms_fail":
                        on_comms_fail(self.play_dev)
                    elif t == "home_tone":
                        on_home_tone(self.play_dev, ev.get("ok") is True)
                    elif t in ("done", "timeout", "error"):
                        print(f"relay: {t}", flush=True)
                        if ev.get("local"):
                            if training_tile_up():
                                # "Train my wake word for <bot>": the relay opened the training tile, then ended
                                # this session. Leave the tile up and skip the end tone, the same as the Bots
                                # page button (0.13.7: idle here closed the tile with the hang-up tone).
                                self._end_played = True
                            elif ev.get("hold_s") == 4 and str(ev.get("text") or "").strip():
                                # Grok's wake word is built in. Show that one line for 4 s. Do not go idle
                                # and do not replace it with the finished-reply hold after playback.
                                builtin_hold = True
                                self.reply = str(ev.get("text") or "").strip()
                                self.state("notice", text=self.reply, hold_s=4)
                            else:
                                post_state("idle")  # local exit has no audio; do not leave thinking up
                            break
                        if t != "error" and self.turns and not self.t_first_audio:
                            post_state("idle")  # follow-up window closed quietly (silence or "thanks")
                        elif t == "timeout" and not self.t_first_audio:
                            self.state("notice", text="Didn’t catch that.")
                        elif t == "error":
                            self.state("error", text="Can’t reach Grok right now.")
                        break
            self.t1_card = None  # session over: the card goes (the relay records an undecided read-back as declined)
            if player:
                player.stdin.close()
                player.wait(timeout=90)
                self._release_reply()
                player = None
                self.stop_speech_clock()
                if not builtin_hold:
                    self.state("speaking", heard=self.heard, text=self.reply or "…", done=True)
            self.feeding = False
            print(f"session over in {time.time() - t0:.1f}s", flush=True)
        except Exception as e:
            print(f"session failed: {type(e).__name__}", flush=True)
            self.state("error", text="Can’t reach Grok right now.")
        finally:
            self.feeding = False
            self.stop_speech_clock()
            self.ws, self.t1_card = None, None
            if CURRENT["session"] is self:
                CURRENT["session"] = None
            if self.save is not None and out:
                with wave.open(self.save, "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"".join(out))
            if player is not None and player.poll() is None:
                try:
                    player.stdin.close()
                except OSError:
                    pass
                try:
                    player.wait(timeout=90)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._release_reply()
            try:
                self.play_end_tone()
            except Exception:
                pass
            self.close_cue()
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


def _env_float(name, default):
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name, default):
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name, default):
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "off", "no")


def _minutes(name, default):
    raw = (os.environ.get(name, "") or default).strip()
    m = re.fullmatch(r"([01][0-9]|2[0-3]):([0-5][0-9])", raw)
    if not m:
        m = re.fullmatch(r"([01][0-9]|2[0-3]):([0-5][0-9])", default)
    return int(m.group(1)) * 60 + int(m.group(2))


def night_window(local_dt):
    """True during the local night-reset window. Default 03:30 inclusive to 04:30 exclusive."""
    start = _minutes("WAKE_MIC_RESET_NIGHT_HM", "03:30")
    end = _minutes("WAKE_MIC_RESET_NIGHT_END", "04:30")
    cur = local_dt.hour * 60 + local_dt.minute
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end


def mic_reset_flags(path=None):
    """button / night / drought. A bool in the file wins; otherwise the env; otherwise on."""
    path = path or os.environ.get("WAKE_MIC_RESET_FILE", "/var/lib/grok-station/admin/mic-reset.json")
    data = {}
    try:
        with open(path) as f:
            got = json.load(f)
        if isinstance(got, dict):
            data = got
    except (OSError, ValueError):
        data = {}
    out = {}
    for key in ("button", "night", "drought"):
        if isinstance(data.get(key), bool):
            out[key] = data[key]
        else:
            out[key] = _env_bool("WAKE_MIC_RESET_" + key.upper(), True)
    return out


def mic_busy():
    """A conversation or enrollment is open. USB reset waits; capture silence does too."""
    if LIVE.get("session") or SESSION_OPEN.is_set() or ENROLL_STATE.get("busy"):
        return True
    return False


def card_spec(capture):
    """ALSA card id from plughw:CARD=<id>,DEV=<n>. Card-number aliases are refused."""
    if not isinstance(capture, str):
        return None
    m = re.fullmatch(r"plughw:CARD=([A-Za-z0-9][A-Za-z0-9_-]{0,31}),DEV=[0-9]{1,2}", capture)
    if not m or re.fullmatch(r"card[0-9]{1,2}", m.group(1)):
        return None
    return m.group(1)


def run_usb_reset(card, run=None):
    """sudo -n the root helper. None on success, otherwise a short reason with no path."""
    if not isinstance(card, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", card):
        return "refused: bad card"
    if re.fullmatch(r"card[0-9]{1,2}", card):
        return "refused: bad card"
    helper = os.environ.get("WAKE_USB_RESET", "/usr/local/lib/grok-station/gs-usb-reset")
    if (not isinstance(helper, str) or ".." in helper.split("/")
            or not re.fullmatch(r"/[A-Za-z0-9_./-]{1,160}", helper)):
        return "refused: bad helper"
    if run is None:
        def run(argv):
            return subprocess.run(argv, capture_output=True, text=True, timeout=8)
    try:
        got = run(["sudo", "-n", helper, card])
    except Exception:
        return "refused: reset failed"
    text = (getattr(got, "stdout", "") or "").strip()
    line = text.splitlines()[-1] if text else ""
    if getattr(got, "returncode", 1) == 0 and line == "ok":
        return None
    if line.startswith("refused: ") and "/" not in line and len(line) <= 80:
        return line
    return "refused: reset failed"


# Icon hold across a USB reset. The card is gone while it re-enumerates; the display adds the
# same 10 s grace it uses for a reopen. Kept within the 30 s stall window plus that grace so a
# bad deadline cannot hide an unplug for longer than a planned reopen.
MIC_RESET_ICON_HOLD_S = 20.0


def kick_capture():
    """Drop the open capture so the loop opens it again (after a USB reset)."""
    guard = CAPTURE.get("guard")
    proc = CAPTURE.get("proc")
    if guard is not None and not guard.reason:
        guard.reason = "reset"
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass


def chunk_stuck(buf):
    """Digital silence or a constant frame. A quiet room still has noise above this."""
    if not buf or len(buf) < 4 or len(buf) % 2:
        return False
    x = np.frombuffer(buf, "<i2")
    if int(np.max(np.abs(x.astype(np.int32)))) <= 1:
        return True
    return float(np.var(x.astype(np.float64))) == 0.0


class CaptureGuard:
    """Kill arecord when no full chunk arrives, and notice a stuck silent stream."""

    def __init__(self, proc, stall_s=3.0, silence_s=10.0, chunk_s=0.08, now=None):
        self.proc = proc
        self.stall_s = stall_s
        self.silence_s = silence_s
        self.chunk_s = chunk_s
        self.now = now or time.monotonic
        self.reason = None
        self._last = self.now()
        self._reading = False  # stall is only counted while we wait on arecord, not while we process
        self._silent = 0.0
        self._stop = threading.Event()
        self._th = None

    def attach(self):
        self._last = self.now()
        self._th = threading.Thread(target=self._watch, daemon=True)
        self._th.start()

    def _watch(self):
        while not self._stop.wait(0.05):
            if self.reason:
                return
            if self._reading and self.now() - self._last >= self.stall_s:
                self.reason = "stall"
                try:
                    self.proc.kill()
                except Exception:
                    pass
                return

    def read(self, n):
        if self.reason:
            return b""
        self._last = self.now()  # wait starts now: our own processing pauses are not a device stall
        self._reading = True
        try:
            buf = self.proc.stdout.read(n)
        except Exception:
            buf = b""
        finally:
            self._reading = False
        self._last = self.now()
        if buf:
            LIVE["audio_at"] = time.time()  # capture alive, in every mode (idle, session, enroll)
        return buf or b""

    def observe(self, buf, in_session):
        if self.reason:
            return True
        if in_session:
            self._silent = 0.0
            return False
        if chunk_stuck(buf):
            self._silent += self.chunk_s
            if self._silent + 1e-9 >= self.silence_s:
                self.reason = "silence"
                try:
                    self.proc.kill()
                except Exception:
                    pass
                return True
        else:
            self._silent = 0.0
        return False

    def stop(self):
        self._stop.set()
        th = self._th
        if th is not None and th is not threading.current_thread():
            th.join(timeout=1)


def serve_capture(proc, guard, handle, in_session, n=None):
    """Read until the capture should be opened again. Returns stall, silence, eof, or reset."""
    n = CHUNK * DECIM * 2 if n is None else n
    while True:
        buf = guard.read(n)
        if guard.reason:
            return guard.reason
        if not buf or len(buf) < n:
            return "eof"
        busy = in_session() if callable(in_session) else bool(in_session)
        if guard.observe(buf, busy):
            return guard.reason or "silence"
        try:
            stop = handle(buf, guard)
        except EOFError:
            return guard.reason or "eof"
        if guard.reason:
            return guard.reason
        if stop:
            return stop if isinstance(stop, str) else (guard.reason or "eof")


def reopen_delay(reason, now, last, gap=30.0, base=2.0):
    """First stall or silence reopens after the short base; a repeat within gap waits out the rest of gap.
    Anything else (eof, USB reset) waits the short base. (Before 0.12.6 the first stall also waited 30 s.)"""
    if reason not in ("stall", "silence"):
        return base
    if last > 0 and now - last < gap:
        return max(base, gap - max(0.0, now - last))
    return base


def run_capture_rounds(find_card, spawn, handle, in_session, *, rounds=None, stall_s=3.0, silence_s=10.0,
                       chunk_s=0.08, sleep=None, log=None, gap=30.0, now=None, n_bytes=None, on_open=None):
    """Open capture, read until it should reopen, repeat. find_card runs on every round."""
    sleep = sleep or time.sleep
    log = log or (lambda msg: print(msg, flush=True))
    now = now or time.time
    reasons, last_watch, n = [], 0.0, 0
    while rounds is None or n < rounds:
        cap = find_card()
        if not cap:
            LIVE["capture"] = None
            log(f"no capture device matching {DEVICE!r}; retrying")
            if rounds is not None:
                reasons.append("no-card")
                n += 1
                if n >= rounds:
                    break
            sleep(5)
            continue
        LIVE["reopen_by"] = None
        if on_open:
            on_open(cap, None)
        proc = spawn(cap)
        guard = CaptureGuard(proc, stall_s=stall_s, silence_s=silence_s, chunk_s=chunk_s)
        CAPTURE["proc"], CAPTURE["guard"] = proc, guard
        guard.attach()
        try:
            reason = serve_capture(proc, guard, handle, in_session, n=n_bytes)
        finally:
            guard.stop()
            CAPTURE["proc"] = None
            CAPTURE["guard"] = None
            try:
                proc.kill()
            except Exception:
                pass
        LIVE["capture"] = None
        log(f"capture reopen: {reason}")
        log("capture ended; restarting")
        reasons.append(reason)
        n += 1
        if rounds is not None and n >= rounds:
            break
        delay = reopen_delay(reason, now(), last_watch, gap=gap)
        if reason in ("stall", "silence"):
            last_watch = now()
        LIVE["reopen_by"] = now() + delay  # tells the display this gap is a planned wait, not a dead mic
        try:
            sleep(delay)
        finally:
            LIVE["reopen_by"] = None
    return reasons


class MicRecovery:
    """USB reset of the open speakerphone. Never during a session. Every reset is logged."""

    def __init__(self, reset, now=None, local=None, flags=None, card=None, busy=None, kick=None, log=None,
                 need=None, window_s=None, score_max=None, speech_db=None, coalesce_s=None):
        self.reset = reset
        self.now = now or time.time
        self.local = local or (lambda ts: datetime.datetime.fromtimestamp(ts).astimezone())
        self.flags = flags or mic_reset_flags
        self.card = card or (lambda: card_spec(LIVE.get("capture")))
        self.busy = busy or mic_busy
        self.kick = kick or kick_capture
        self.log = log or (lambda msg: print(msg, flush=True))
        self.need = _env_int("WAKE_MIC_RESET_SPEECH", 20) if need is None else need
        self.window_s = _env_float("WAKE_MIC_RESET_WINDOW_S", 7200.0) if window_s is None else window_s
        self.score_max = _env_float("WAKE_MIC_RESET_SCORE", 0.1) if score_max is None else score_max
        self.speech_db = _env_float("WAKE_MIC_RESET_SPEECH_DB", -45.0) if speech_db is None else speech_db
        self.coalesce_s = _env_float("WAKE_MIC_RESET_COALESCE_S", 2.0) if coalesce_s is None else coalesce_s
        self.pending = None
        self.last_night = None
        self.events = []
        self._last_poll = 0.0
        self._next_auto = 0.0
        self.min_gap_s = _env_float("WAKE_MIC_RESET_MIN_GAP_S", 45.0)
        self._last_reset = None

    def request(self, reason):
        if reason not in ("button", "night", "drought"):
            return "off"
        flags = self.flags()
        if not flags.get(reason):
            return "off"
        if reason == "night":
            loc = self.local(self.now())
            if self.last_night == loc.date() or not night_window(loc):
                return "off"
        if reason == "button" and self.too_soon():
            return "too_soon"
        if self.busy():
            if self.pending != reason:
                self.log(f"mic reset deferred (session): {reason}")
            self.pending = reason
            return "deferred"
        return self._run(reason)

    def poll(self):
        now = self.now()
        if now - self._last_poll < 1:
            return
        self._last_poll = now
        flags = self.flags()
        if self.pending and not flags.get(self.pending):
            self.pending = None
        if self.pending == "night" and not night_window(self.local(now)):
            self.pending = None
            self.log("mic reset skipped: night window ended")
        elif self.pending and not self.busy() and now >= self._next_auto:
            self._run(self.pending)
            return
        if self.pending or not flags.get("night") or now < self._next_auto:
            return
        loc = self.local(now)
        if night_window(loc) and self.last_night != loc.date():
            self.request("night")

    def note_level(self, dbfs, score, session=False):
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0
        if score > self.score_max:
            self.events.clear()
            return
        if session or self.busy():
            return
        if not self.flags().get("drought"):
            return
        try:
            db = float(dbfs)
        except (TypeError, ValueError):
            return
        if db < self.speech_db:
            return
        now = self.now()
        if self.events and now - self.events[-1] < self.coalesce_s:
            return
        self.events.append(now)
        if (len(self.events) >= self.need and self.events[-1] - self.events[0] >= self.window_s
                and now >= self._next_auto):
            self.events.clear()
            self.request("drought")

    def too_soon(self):
        """True within min_gap_s of the last reset attempt (any reason)."""
        return self._last_reset is not None and 0 <= self.now() - self._last_reset < max(45.0, self.min_gap_s)

    def _backoff(self, reason):
        if reason != "button":
            self._next_auto = self.now() + 60

    def _run(self, reason):
        flags = self.flags()
        if not flags.get(reason):
            self.pending = None
            return "off"
        if reason == "night":
            loc = self.local(self.now())
            if not night_window(loc):
                self.pending = None
                self.log("mic reset skipped: night window ended")
                return "skipped"
            if self.last_night == loc.date():
                self.pending = None
                return "off"
        if self.busy():
            if self.pending != reason:
                self.log(f"mic reset deferred (session): {reason}")
            self.pending = reason
            return "deferred"
        if self.too_soon():
            if reason == "button":
                self.pending = None
                return "too_soon"
            self.pending = reason
            self._next_auto = max(self._next_auto, self._last_reset + max(45.0, self.min_gap_s))
            return "deferred"
        self.pending = None
        card = self.card()
        if not card:
            self.log(f"mic reset failed: no card ({reason})")
            self._backoff(reason)
            return "failed"
        self._last_reset = self.now()
        # Publish the hold and drop the read before deauthorize. Killing arecord after
        # authorized is already 0 can leave the read stuck and audio_at stale, and the
        # card is absent from /proc/asound/cards for the whole re-enumeration.
        LIVE["reset_until"] = self.now() + MIC_RESET_ICON_HOLD_S
        try:
            self.kick()
        except Exception:
            pass
        try:
            err = self.reset(card)
        except Exception:
            err = "refused: reset failed"
        if err:
            LIVE["reset_until"] = None
            self.log(f"mic reset failed: {err} ({reason})")
            self._backoff(reason)
            return "failed"
        self.log(f"mic reset: {reason}")
        self.events.clear()
        if reason == "night":
            self.last_night = self.local(self.now()).date()
        return "ok"


def _mic_poll():
    while True:
        rec = RECOVERY
        if rec is not None:
            try:
                rec.poll()
            except Exception as exc:
                print(f"mic reset poll failed: {type(exc).__name__}", flush=True)
        time.sleep(1)


def main():
    global RECOVERY
    RECOVERY = MicRecovery(run_usb_reset)
    make_chime(CHIME)
    if RELAY_URL:
        threading.Thread(target=persona_loop, daemon=True).start()
    stale_build()
    sweep_stale_sessions()
    threading.Thread(target=control_server, daemon=True).start()
    threading.Thread(target=_mic_poll, daemon=True).start()
    wait_tone.TONE.start(lambda: LIVE.get("play"))
    timers_load()
    threading.Thread(target=timers_loop, daemon=True).start()
    threading.Thread(target=tap_loop, daemon=True).start()
    model = load_model()
    det = Detector()
    print(f"cooldown={COOLDOWN}s gain={GAIN}", flush=True)
    st = {"sess": None, "tail": None, "tail24": None, "last_hit": 0.0, "last_log": 0.0, "peak": 0.0,
          "rms_sum": 0.0, "n": 0, "peaks": {}, "sample_peak": 0, "clip_n": 0, "sample_n": 0}

    def on_open(cap, proc):
        play = find_card("aplay") or cap
        LIVE["play"], LIVE["capture"] = play, cap
        print(f"capture {cap} playback {play}", flush=True)
        st["last_hit"] = 0.0
        st["last_log"] = time.time()
        st["peak"] = 0.0
        st["rms_sum"] = 0.0
        st["n"] = 0
        st["peaks"] = {}
        st["sample_peak"] = 0
        st["clip_n"] = 0
        st["sample_n"] = 0
        st["tail"] = np.zeros(len(_K) - 1, np.float32)
        st["tail24"] = np.zeros(len(_K24) - 1, np.float32)
        st["sess"] = None

    def spawn(cap):
        # 500 ms ALSA buffer / 100 ms periods: a slow Pi (3B) can stall for a few hundred ms without an overrun
        return subprocess.Popen(["arecord", "-q", "-D", cap, "-f", "S16_LE", "-r", str(CAP_RATE), "-c", "1", "-t", "raw",
                                 "--buffer-time=" + E("WAKE_ALSA_BUFFER_US", "500000"),
                                 "--period-time=" + E("WAKE_ALSA_PERIOD_US", "100000")],
                                stdout=subprocess.PIPE)  # buffered: read(n) returns full chunks

    def handle(buf, guard):
        nonlocal model, det
        x = np.frombuffer(buf, "<i2").astype(np.float32)
        sess = st["sess"]
        if not sess and not ENROLL_Q.empty():
            def read16(raw=False):
                b = guard.read(CHUNK * DECIM * 2)
                if guard.reason or not b or len(b) < CHUNK * DECIM * 2:
                    raise EOFError
                y = np.frombuffer(b, "<i2").astype(np.float32)
                if DECIM > 1:
                    yy = np.concatenate([st["tail"], y]); st["tail"] = yy[-(len(_K) - 1):]
                    y = np.convolve(yy, _K, "valid")[::DECIM]
                return np.clip(y * (1.0 if raw else GAIN), -32768, 32767).astype("<i2")
            try:
                enroll(ENROLL_Q.get_nowait(), read16, LIVE["play"])
            except EOFError:
                return "eof"
            except Exception as e:
                print(f"enroll failed: {type(e).__name__}", flush=True)
                post_state("error", text="Enrollment stopped.")
            reset_detect(model); det.reset()
            return None
        if RELOAD.is_set() and not sess:
            RELOAD.clear()
            try:
                model = load_model(); det = Detector()
            except Exception as e:
                print(f"wake model reload failed: {type(e).__name__}", flush=True)
            return None
        LIVE["session"] = bool(sess and sess.active)
        if sess and not sess.active:
            sess = None
            st["sess"] = None
            reset_detect(model)  # drop audio features from the session before listening again
            det.reset()
        if sess:
            if sess.feeding:
                xx = np.concatenate([st["tail24"], x]); st["tail24"] = xx[-(len(_K24) - 1):]
                y = np.convolve(xx, _K24, "valid")[::D24] if D24 > 1 else up24(x, xx[-len(x) - 1]) if CAP_RATE == 16000 else x
                sess.feed(np.clip(y * VOICE_GAIN, -32768, 32767).astype("<i2").tobytes())
            return None  # wake detector paused while a session is open
        if DECIM > 1:
            xx = np.concatenate([st["tail"], x]); st["tail"] = xx[-(len(_K) - 1):]
            x = np.convolve(xx, _K, "valid")[::DECIM]
        pcm = np.clip(x * GAIN, -32768, 32767).astype("<i2")
        # The prompt tile closes on its own after PROMPT_TTL_S. Recording uses enroll's own read.
        _prompt_tick()
        sc = scores(model, pcm)
        s = max(sc.values(), default=0.0)
        hit = det.feed(sc)
        r = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
        st["rms_sum"] += r; st["n"] += 1
        LIVE["dbfs"], LIVE["at"] = round(float(20 * np.log10(max(r, 1) / 32768)), 1), time.time()  # admin mic meter
        if RECOVERY is not None:
            try:
                RECOVERY.note_level(LIVE["dbfs"], s, session=False)
            except Exception as e:
                print(f"mic reset note failed: {type(e).__name__}", flush=True)
        sp, cn, sn = pcm_extremes(pcm)
        st["sample_peak"] = max(st["sample_peak"], sp)
        st["clip_n"] += cn
        st["sample_n"] += sn
        st["peak"] = max(st["peak"], s)
        for k, v in sc.items():
            st["peaks"][k] = max(st["peaks"].get(k, 0.0), v)
        now = time.time()
        if hit and now - st["last_hit"] >= COOLDOWN:
            st["last_hit"] = now
            slug = WORDS[hit]["persona"]
            print(f"WAKE {hit} score={sc.get(hit, 0.0):.3f} persona={slug}", flush=True)
            det.reset()
            mode = setup_mode()
            if mode:  # not set up (or password reset / no network): listen tone + spoken line, no relay session
                print(f"wake ignored: {mode} pending", flush=True)
                line = os.path.join(SETUP_LINES, mode + ".wav")
                play_seq(LIVE["play"], [configured_wav("LISTEN_TONE"), line if os.path.isfile(line) else None])
                return None
            play_file(LIVE["play"], configured_wav("LISTEN_TONE"))
            threading.Thread(target=post_state, args=("listening",), kwargs={"who": persona_name(slug)}, daemon=True).start()
            if RELAY_URL:
                st["sess"] = Session(LIVE["play"], persona=slug)
            return None
        if now - st["last_log"] >= LEVEL_EVERY:
            avg = st["rms_sum"] / max(1, st["n"])
            ratio = (st["clip_n"] / st["sample_n"]) if st["sample_n"] else 0.0
            print(format_level_line(avg, st["peak"], st["n"], st["peaks"], st["sample_peak"], ratio), flush=True)
            st["last_log"], st["peak"], st["rms_sum"], st["n"], st["peaks"] = now, 0.0, 0.0, 0, {}
            st["sample_peak"], st["clip_n"], st["sample_n"] = 0, 0, 0
        return None

    run_capture_rounds(lambda: find_card("arecord"), spawn, handle, mic_busy, on_open=on_open,
                       stall_s=_env_float("WAKE_CAPTURE_STALL_S", 3.0),
                       silence_s=_env_float("WAKE_CAPTURE_SILENCE_S", 10.0),
                       chunk_s=CHUNK / 16000.0, gap=_env_float("WAKE_CAPTURE_REOPEN_S", 30.0))


def enroll_cli(phrase, count, topup):
    """POST /enroll on the loopback control port. Sends the token header when the file is readable."""
    body = json.dumps({"phrase": phrase, "count": count, "topup": bool(topup)}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{CONTROL_PORT}/enroll", body,
                                 _wake_client_headers({"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            print(r.read().decode())
            return 0
    except urllib.error.HTTPError as e:
        print(e.read().decode())
        return 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        test(sys.argv[2])
    elif len(sys.argv) in (2, 3) and sys.argv[1] == "--bench":
        a = sys.argv[2] if len(sys.argv) == 3 else "30"
        bench(30, a) if a.endswith(".wav") else bench(int(a))
    elif len(sys.argv) == 2 and sys.argv[1] == "--sync":
        sync_personas()
        sync_wakewords()
    elif len(sys.argv) in (3, 4, 5) and sys.argv[1] == "--enroll":
        args = [a for a in sys.argv[2:] if a != "--topup"]
        top = "--topup" in sys.argv
        n = int(args[1]) if len(args) > 1 else (TOPUP_COUNT if top else ENROLL_COUNT)
        if enroll_cli(args[0], n, top):
            sys.exit(1)
    elif len(sys.argv) in (3, 4) and sys.argv[1] == "--ask":
        ask(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    else:
        main()
