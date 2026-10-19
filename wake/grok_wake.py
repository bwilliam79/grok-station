#!/usr/bin/env python3
"""grok-station wake listener + voice client (proof of concept).

Captures the USB headset mic at 48 kHz via `arecord`, decimates to 16 kHz for openWakeWord (ONNX).
On a wake it plays a chime, shows "listening", and (if GROK_RELAY_URL is set) opens one session to the
voice relay: mic audio goes up as PCM16 24 kHz, reply audio comes back and plays on the headset via
`aplay`, and the display follows listening -> thinking -> speaking -> idle through localhost POST /state.
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
import json, os, queue, re, subprocess, sys, threading, time, urllib.request, wave
import numpy as np

E = os.environ.get
MODEL = E("WAKE_MODEL", "hey_jarvis")          # pre-trained placeholder until a custom "Hey Grok" model exists
THRESH = float(E("WAKE_THRESHOLD", "0.5"))
COOLDOWN = float(E("WAKE_COOLDOWN", "2"))
DEVICE = E("WAKE_DEVICE_NAME", "EMEET")        # matched against `arecord -l` / `aplay -l` card names (capture + playback)
STATE_URL = E("WAKE_STATE_URL", "http://127.0.0.1:8787/state")
CHIME = E("WAKE_CHIME", "/opt/grok-station/wake/chime.wav")
LEVEL_EVERY = float(E("WAKE_LEVEL_EVERY", "30"))
GAIN = float(E("WAKE_GAIN", "4"))                # software gain; headset mics run quiet
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
        if img and img[:3] == b"\xff\xd8\xff" or img and img[:8] == b"\x89PNG\r\n\x1a\n":
            ext = "png" if img[:4] == b"\x89PNG" else "jpg"
            for old in ("png", "jpg"):
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
ENROLL_STATE = {"busy": False, "fast_until": 0}
PHRASE_RE = re.compile(r"^hey_[a-z]{2,16}$")
CLIP_S, LEAD_S = 2.4, 0.9  # recording window per clip, pause before the chime


def relay_post(path, body, ctype="application/octet-stream"):
    base = re.sub(r"^ws", "http", RELAY_URL).rsplit("/ws", 1)[0]
    req = urllib.request.Request(base + path, body, {"Authorization": f"Bearer {RELAY_TOKEN}", "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=15, context=relay_ssl()) as r:
        return r.status


def enroll_phrases():
    """phrase -> persona for every phrase that may be enrolled: configured words plus the config's "enroll" list."""
    try:
        with open(WAKE_CONFIG) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    out = {}
    for w in cfg.get("words", []) + cfg.get("enroll", []):
        n, ps = str(w.get("name", "")), str(w.get("persona", "grok"))
        if PHRASE_RE.match(n) and SLUG_RE.match(ps):
            out[n] = ps
    return out


def control_server():
    """127.0.0.1:CONTROL_PORT POST /enroll {"phrase","count"}: queue one enrollment (the display's LAN /enroll and
    `grok_wake.py --enroll` both land here). Never reachable from the LAN."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def _r(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def do_POST(self):
            if self.path != "/enroll":
                return self._r(404, {"error": "not found"})
            try:
                j = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 1024)) or b"{}")
                phrase, count = str(j.get("phrase", "")), int(j.get("count", 25))
            except Exception:
                return self._r(400, {"error": "bad json"})
            allowed = enroll_phrases()
            if phrase not in allowed:
                return self._r(400, {"error": "unknown phrase", "phrases": sorted(allowed)})
            if not 10 <= count <= 40:
                return self._r(400, {"error": "count must be 10-40"})
            if not RELAY_URL:
                return self._r(503, {"error": "no relay configured"})
            if ENROLL_STATE["busy"]:
                return self._r(409, {"error": "enrollment in progress"})
            try:
                ENROLL_Q.put_nowait({"phrase": phrase, "persona": allowed[phrase], "count": count})
            except queue.Full:
                return self._r(409, {"error": "an enrollment is already queued"})
            print(f"enroll queued: {phrase} x{count}", flush=True)
            return self._r(202, {"ok": True, "phrase": phrase, "persona": allowed[phrase], "count": count})

        def log_message(self, *a):
            pass

    ThreadingHTTPServer(("127.0.0.1", CONTROL_PORT), H).serve_forever()


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


def enroll(req, read16, play):
    """Record req["count"] clips with on-screen prompts and a count, upload each, then mark the session ready."""
    phrase, persona, n = req["phrase"], req["persona"], req["count"]
    words = phrase.replace("_", " ").title()
    say = f"Say “{words}”"
    ses = time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(ENROLL_DIR, phrase, ses)
    os.makedirs(d, exist_ok=True)
    ENROLL_STATE["busy"] = True
    print(f"enroll start {phrase} x{n} session {ses}", flush=True)
    sent, i, tries = 0, 1, 0
    try:
        post_state("enroll", text=say, heard=f"1/{n}|Speak naturally, about an arm’s length away")
        for _ in range(int(3 / 0.08)):
            read16()
        while i <= n:
            post_state("enroll", text=say, heard=f"{i}/{n}|Get ready…")
            for _ in range(int(LEAD_S / 0.08)):
                read16()
            subprocess.Popen(["aplay", "-q", "-D", play, CHIME], stderr=subprocess.DEVNULL)
            for _ in range(4):  # skip the chime itself
                read16()
            post_state("enroll", text=say, heard=f"{i}/{n}|Recording… speak now")
            pcm = np.concatenate([read16() for _ in range(int(CLIP_S / 0.08))])
            clip = trim_clip(pcm)
            clipped = np.mean(np.abs(pcm.astype(np.int32)) >= 32000) > 0.001
            if clip is None or clipped:
                tries += 1
                why = "Too loud, step back a little" if clipped else "Didn’t hear it, a bit louder please"
                print(f"enroll clip {i} rejected ({'clipped' if clipped else 'quiet'})", flush=True)
                post_state("enroll", text=say, heard=f"{i}/{n}|{why}")
                for _ in range(int(1.5 / 0.08)):
                    read16()
                if tries >= 3 * n:
                    break
                continue
            path = os.path.join(d, f"{i - 1:02d}.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(clip.astype("<i2").tobytes())
            try:
                with open(path, "rb") as f:
                    relay_post(f"/enroll/{phrase}/{ses}/{i - 1:02d}.wav", f.read(), "audio/wav")
                sent += 1
            except Exception as e:
                print(f"enroll upload {i} failed: {type(e).__name__}", flush=True)
            i += 1
        ok = False
        if sent >= 10:
            try:
                relay_post(f"/enroll/{phrase}/{ses}/done", json.dumps({"persona": persona, "count": sent}).encode(), "application/json")
                ok = True
            except Exception as e:
                print(f"enroll done failed: {type(e).__name__}", flush=True)
        print(f"enroll end {phrase}: {i - 1} recorded, {sent} uploaded, {'ready' if ok else 'NOT submitted'}", flush=True)
        if ok:
            ENROLL_STATE["fast_until"] = time.time() + 3 * 3600
            SYNC_NOW.set()
            post_state("enroll", text="Thanks! All done.",
                       heard=f"|{sent} clips sent. Training takes about an hour; the station loads the new model on its own.")
        else:
            post_state("error", text="Enrollment didn’t finish. Try again later.")
        time.sleep(6)
        post_state("idle")
    finally:
        ENROLL_STATE["busy"] = False


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
        if not (PHRASE_RE.match(name) and SLUG_RE.match(persona) and re.fullmatch(r"[0-9a-f]{64}", sha)):
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


def post_state(state, **kw):
    print(f"display -> {state}{' (' + kw['who'] + ')' if kw.get('who') else ''}", flush=True)
    try:
        req = urllib.request.Request(STATE_URL, json.dumps({"state": state, **kw}).encode(), {"Content-Type": "application/json"})
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
        self.persona = persona if SLUG_RE.match(persona or "") else "grok"
        self.who = persona_name(self.persona)  # the persona speaking; the relay's persona event changes it
        threading.Thread(target=self.run, daemon=True).start()

    def state(self, state, **kw):
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
            while self.feeding:
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
                    url = RELAY_URL + (f"?persona={self.persona}" if self.persona != "grok" else "")
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
                    elif t == "xai_error":
                        print("relay: xai error", flush=True)
                    elif t in ("done", "timeout", "error"):
                        print(f"relay: {t}", flush=True)
                        if t == "timeout" and not self.t_first_audio:
                            self.state("notice", text="Didn’t catch that.")
                        elif t == "error":
                            self.state("error", text="Can’t reach Grok right now.")
                        break
            self.feeding = False
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
            if self.save is not None and out:
                with wave.open(self.save, "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"".join(out))
            self.active = False


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
                def read16():
                    nonlocal tail
                    b = p.stdout.read(CHUNK * DECIM * 2)
                    if not b or len(b) < CHUNK * DECIM * 2:
                        raise EOFError
                    y = np.frombuffer(b, "<i2").astype(np.float32)
                    if DECIM > 1:
                        yy = np.concatenate([tail, y]); tail = yy[-(len(_K) - 1):]
                        y = np.convolve(yy, _K, "valid")[::DECIM]
                    return np.clip(y * GAIN, -32768, 32767).astype("<i2")
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
            rms_sum += float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))); n += 1
            peak = max(peak, s)
            for k, v in sc.items():
                peaks[k] = max(peaks.get(k, 0.0), v)
            now = time.time()
            if hit and now - last_hit >= COOLDOWN:
                last_hit = now
                slug = WORDS[hit]["persona"]
                print(f"WAKE {hit} score={sc.get(hit, 0.0):.3f} persona={slug}", flush=True)
                det.reset()
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
    elif len(sys.argv) in (3, 4) and sys.argv[1] == "--enroll":
        body = json.dumps({"phrase": sys.argv[2], "count": int(sys.argv[3]) if len(sys.argv) == 4 else 25}).encode()
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
