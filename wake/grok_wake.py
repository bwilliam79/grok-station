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
"""
import json, os, queue, re, subprocess, sys, threading, time, urllib.request, wave
import numpy as np

E = os.environ.get
MODEL = E("WAKE_MODEL", "hey_jarvis")          # pre-trained placeholder until a custom "Hey Grok" model exists
THRESH = float(E("WAKE_THRESHOLD", "0.5"))
COOLDOWN = float(E("WAKE_COOLDOWN", "2"))
DEVICE = E("WAKE_DEVICE_NAME", "Razer")        # matched against `arecord -l` / `aplay -l` card names
STATE_URL = E("WAKE_STATE_URL", "http://127.0.0.1:8787/state")
CHIME = E("WAKE_CHIME", "/opt/grok-station/wake/chime.wav")
LEVEL_EVERY = float(E("WAKE_LEVEL_EVERY", "30"))
GAIN = float(E("WAKE_GAIN", "4"))                # software gain; headset mics run quiet
CAP_RATE = int(E("WAKE_CAPTURE_RATE", "48000"))  # capture natively, decimate to 16 kHz (Razer USB card's 16 kHz mode is muffled)
DECIM = CAP_RATE // 16000
_K = np.sinc(np.arange(-32, 33) / DECIM) * np.hamming(65); _K /= _K.sum()  # low-pass FIR for decimation
CHUNK = 1280                                     # 80 ms at 16 kHz, what openWakeWord expects
RELAY_URL, RELAY_TOKEN = E("GROK_RELAY_URL", ""), E("GROK_RELAY_TOKEN", "")
VOICE_GAIN = float(E("VOICE_GAIN", str(GAIN)))   # gain on the audio sent to Grok
VOICE_SKIP = float(E("VOICE_SKIP_MS", "300")) / 1000  # drop this much post-wake audio (tail of the wake word)
PORTRAITS = E("GROK_STATION_PORTRAITS", "/opt/grok-station/portraits")
D24 = CAP_RATE // 24000
_K24 = np.sinc(np.arange(-24, 25) / max(1, D24)) * np.hamming(49); _K24 /= _K24.sum()


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
        try:
            sync_personas()
        except Exception as e:
            print(f"persona sync failed: {type(e).__name__}", flush=True)
        time.sleep(3600)


def load_model():
    from openwakeword.model import Model
    return Model(wakeword_models=[MODEL], inference_framework="onnx")


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
    return model.predict(pcm).get(next(iter(model.models)), 0.0)


def test(path):
    model = load_model()
    with wave.open(path) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, "need 16 kHz mono s16"
        pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2")
    pcm = np.concatenate([np.zeros(16000, "<i2"), pcm, np.zeros(16000, "<i2")])
    best, at = 0.0, 0.0
    for i in range(0, len(pcm) - CHUNK, CHUNK):
        s = scores(model, pcm[i:i + CHUNK])
        if s > best:
            best, at = s, i / 16000
    print(f"{os.path.basename(path)}: peak {MODEL} score {best:.3f} at {at:.2f}s -> {'DETECTED' if best >= THRESH else 'no'}")


class Session:
    """One question and answer through the relay. Feed it 24 kHz PCM16 bytes with feed(); runs in a thread."""

    def __init__(self, play_dev, save=None, skip=VOICE_SKIP, echo_text=False):
        self.q, self.feeding, self.active = queue.Queue(maxsize=400), True, True
        self.skip = int(skip * 24000) * 2  # bytes of 24 kHz PCM16 still to drop
        self.echo_text = echo_text         # print transcripts: only for the manual --ask check, never the service log
        self.play_dev, self.save, self.t_end_speech, self.t_first_audio = play_dev, save, None, None
        self.heard, self.reply, self.ready, self.responding = "", "", threading.Event(), False
        self.who = "Grok"  # the persona speaking; the relay's persona event changes it
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
                    ws = connect(RELAY_URL, additional_headers={"Authorization": f"Bearer {RELAY_TOKEN}"},
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
                            self.state("speaking" if self.t_first_audio else "thinking", heard=self.heard, text="…")
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
    model = load_model()
    print(f"model={MODEL} threshold={THRESH} cooldown={COOLDOWN}s gain={GAIN}", flush=True)
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
        last_hit, last_log, peak, rms_sum, n = 0.0, time.time(), 0.0, 0.0, 0
        tail = np.zeros(len(_K) - 1, np.float32)
        tail24 = np.zeros(len(_K24) - 1, np.float32)
        sess = None
        while True:
            buf = p.stdout.read(CHUNK * DECIM * 2)
            if not buf or len(buf) < CHUNK * DECIM * 2:
                break
            x = np.frombuffer(buf, "<i2").astype(np.float32)
            if sess and not sess.active:
                sess = None
                model.reset()  # drop audio features from the session before listening again
            if sess:
                if sess.feeding:
                    xx = np.concatenate([tail24, x]); tail24 = xx[-(len(_K24) - 1):]
                    y = np.convolve(xx, _K24, "valid")[::D24] if D24 > 1 else x
                    sess.feed(np.clip(y * VOICE_GAIN, -32768, 32767).astype("<i2").tobytes())
                continue  # wake detector paused while a session is open
            if DECIM > 1:
                xx = np.concatenate([tail, x]); tail = xx[-(len(_K) - 1):]
                x = np.convolve(xx, _K, "valid")[::DECIM]
            pcm = np.clip(x * GAIN, -32768, 32767).astype("<i2")
            s = scores(model, pcm)
            rms_sum += float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))); n += 1
            peak = max(peak, s)
            now = time.time()
            if s >= THRESH and now - last_hit >= COOLDOWN:
                last_hit = now
                print(f"WAKE {MODEL} score={s:.3f}", flush=True)
                subprocess.Popen(["aplay", "-q", "-D", play, CHIME], stderr=subprocess.DEVNULL)
                threading.Thread(target=post_state, args=("listening",), daemon=True).start()
                if RELAY_URL:
                    sess = Session(play)
            if now - last_log >= LEVEL_EVERY:
                avg = rms_sum / max(1, n)
                dbfs = 20 * np.log10(max(avg, 1) / 32768)
                print(f"level rms={avg:.0f} ({dbfs:.1f} dBFS) peak_score={peak:.3f} frames={n}", flush=True)
                last_log, peak, rms_sum, n = now, 0.0, 0.0, 0
        p.kill()
        print("capture ended; restarting", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        test(sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == "--sync":
        sync_personas()
    elif len(sys.argv) in (3, 4) and sys.argv[1] == "--ask":
        ask(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    else:
        main()
