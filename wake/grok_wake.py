#!/usr/bin/env python3
"""grok-station wake listener (proof of concept).

Reads 16 kHz mono S16 audio from the USB headset mic via `arecord`, runs openWakeWord (ONNX),
and on detection plays a chime on the headset and POSTs {"state": "listening"} to the display
app's localhost-only /state endpoint. The display returns to idle on its own after a timeout.
`grok_wake.py --test file.wav` scores a 16 kHz mono WAV offline and prints the peak score.
"""
import json, os, re, subprocess, sys, threading, time, urllib.request, wave
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


def post_state(state):
    try:
        req = urllib.request.Request(STATE_URL, json.dumps({"state": state}).encode(), {"Content-Type": "application/json"})
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


def main():
    make_chime(CHIME)
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
        while True:
            buf = p.stdout.read(CHUNK * DECIM * 2)
            if not buf or len(buf) < CHUNK * DECIM * 2:
                break
            x = np.frombuffer(buf, "<i2").astype(np.float32)
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
    else:
        main()
