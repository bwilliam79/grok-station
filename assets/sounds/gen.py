#!/usr/bin/env python3
"""Original grok-station notification sounds, synthesized from scratch (no samples).
Output: 16-bit 44.1 kHz mono WAV, peak -6 dBFS. Requires numpy; ffmpeg for MP3."""
import numpy as np, wave, subprocess, os
SR = 44100
OUT = os.path.dirname(os.path.abspath(__file__))
PEAK = 10 ** (-6 / 20)

def note(f, dur, partials=((1, 1.0),), decay=6.0, attack=0.006):
    t = np.arange(int(SR * dur)) / SR
    s = sum(a * np.sin(2 * np.pi * f * m * t) * np.exp(-decay * m ** 0.5 * t) for m, a in partials)
    env = np.minimum(1, t / attack)
    rel = int(SR * 0.03); env[-rel:] *= np.linspace(1, 0, rel) ** 2  # no end click
    return s * env

MARIMBA = ((1, 1.0), (4, 0.25), (10, 0.05))
BELL = ((1, 1.0), (2.0, 0.45), (3.01, 0.2), (4.2, 0.1), (5.4, 0.05))
SOFT = ((1, 1.0), (2, 0.15))

def seq(events, total):
    buf = np.zeros(int(SR * total))
    for start, sig in events:
        i = int(SR * start); n = min(len(sig), len(buf) - i); buf[i:i + n] += sig[:n]
    return buf

def write(name, x):
    x = x / np.max(np.abs(x)) * PEAK
    path = os.path.join(OUT, name + ".wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-b:a", "128k",
                    path[:-4] + ".mp3"], check=True)
    return x

S = {}
# 1. comms-fail (descending, gentle)
S["comms-fail-1"] = seq([(0, note(659.25, .35, MARIMBA, 9)), (.2, note(523.25, .5, MARIMBA, 7))], .7)
S["comms-fail-2"] = seq([(0, note(587.33, .4, SOFT, 6)), (.22, note(440.0, .55, SOFT, 5))], .8)
S["comms-fail-3"] = seq([(0, note(783.99, .3, MARIMBA, 10)), (.14, note(659.25, .3, MARIMBA, 10)),
                         (.28, note(523.25, .3, MARIMBA, 8))], .6)
# 2. alarm (loopable, ends in silence/decayed tails)
S["alarm-1"] = seq([(i * .25, note(f, .6, BELL, 5)) for i, f in enumerate([523.25, 659.25, 783.99, 659.25] * 2)] , 2.5)
S["alarm-2"] = seq([(i * .4, note(f, 1.0, BELL, 3.5)) for i, f in enumerate([392.0, 493.88, 587.33, 783.99, 987.77])] , 2.8)
S["alarm-3"] = seq([(b + d, note(880.0 if d == 0 else 659.25, .45, MARIMBA, 6))
                    for b in (0, .75, 1.5) for d in (0, .15)], 2.25)
# 3. notify/ack
S["notify-1"] = note(1046.5, .6, BELL, 6)
S["notify-2"] = seq([(0, note(783.99, .5, SOFT, 7)), (.09, note(1174.66, .5, SOFT, 7))], .6)


def loopable(x, fade=0.04):
    """Fade both edges so the loop seam is silent (no click)."""
    n = int(SR * fade); x = x.copy()
    x[:n] *= np.linspace(0, 1, n) ** 2; x[-n:] *= np.linspace(1, 0, n) ** 2
    return x

KALIMBA = ((1, 1.0), (5.4, 0.12), (8.9, 0.04))
MUSICBOX = ((1, 1.0), (3.0, 0.3), (5.2, 0.12), (7.8, 0.05))
t2 = np.arange(int(SR * 2.4)) / SR
# alarm-4: soft pad (A3+E4+A4 detuned) pulsing at 1.25 Hz
pad = sum(np.sin(2*np.pi*f*t2) + 0.5*np.sin(2*np.pi*f*1.003*t2) for f in (220.0, 329.63, 440.0))
S["alarm-4"] = loopable(pad * (0.5 - 0.5*np.cos(2*np.pi*1.25*t2)))
# alarm-5: kalimba arpeggio D major, up and back
S["alarm-5"] = loopable(seq([(i*.2, note(f, .7, KALIMBA, 6)) for i, f in enumerate(
    [293.66, 369.99, 440.0, 587.33, 739.99, 587.33, 440.0, 369.99, 293.66, 440.0, 587.33, 440.0])], 2.4))
# alarm-6: sunrise swell, Fmaj7 chord swelling with a rising shimmer
t3 = np.arange(int(SR * 3.0)) / SR
sw = sum(np.sin(2*np.pi*f*t3) for f in (174.61, 220.0, 261.63, 329.63)) * np.sin(np.pi*t3/3.0)**2
sh = np.sin(2*np.pi*(523.25 + 120*t3)*t3) * np.sin(np.pi*t3/3.0)**4 * 0.5
S["alarm-6"] = loopable(sw + sh)
# alarm-7: gentle music-box phrase in G
S["alarm-7"] = loopable(seq([(i*.3, note(f, .9, MUSICBOX, 4)) for i, f in enumerate(
    [783.99, 987.77, 1174.66, 987.77, 880.0, 1174.66, 783.99, 0])][:-1], 2.4))
# session-end tones (resolve downward, low register vs high notify chimes)
S["end-1"] = seq([(0, note(659.25, .3, SOFT, 8)), (.15, note(392.0, .35, SOFT, 7))], .5)
S["end-2"] = seq([(0, note(587.33, .28, KALIMBA, 9)), (.12, note(493.88, .3, KALIMBA, 9)),
                  (.24, note(392.0, .26, KALIMBA, 8))], .5)
S["end-3"] = note(329.63, .5, BELL, 7) + 0.6*note(261.63, .5, BELL, 7)

def loop_check(name, x):
    x = x / np.max(np.abs(x)) * PEAK
    y = np.tile(x, 3); L = len(x)
    seam = max(abs(y[k*L] - y[k*L-1]) for k in (1, 2))
    typical = np.max(np.abs(np.diff(x)))
    write(name + "-loop3x", y)
    return seam, typical

if __name__ == "__main__":
    gap = np.zeros(SR)
    def preview(name, keys):
        parts = []
        for k in keys: parts += [write(k, S[k]), gap]
        write(name, np.concatenate(parts[:-1]))
    old = [k for k in S if not (k in ("alarm-4","alarm-5","alarm-6","alarm-7") or k.startswith("end-"))]
    preview("preview-all", old)
    preview("preview-new", ["alarm-4","alarm-5","alarm-6","alarm-7","end-1","end-2","end-3"])
    for k in [k for k in S if k.startswith("alarm")]:
        seam, typ = loop_check(k, S[k])
        print(f"{k}: seam jump {seam:.5f} (max in-file step {typ:.5f}) {'OK' if seam < 0.01 else 'CLICK?'}")
