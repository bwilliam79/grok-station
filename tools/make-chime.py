#!/usr/bin/env python3
"""Write the wake chime (two 100 ms tones, 880 then 1320 Hz, 10 ms fades, 16 kHz mono 16-bit) to PATH. Stdlib only.

0.13.14 (Nix PI-01): wake/ is root-owned, so the listener can no longer make wake/chime.wav at its first start.
install.sh and install-files.sh run this as root and install the result root:root 0644. Same samples as
grok_wake.make_chime. Usage: python3 -I tools/make-chime.py PATH
"""
import math
import struct
import sys
import wave


def samples():
    sr = 16000
    n = int(sr * 0.1)
    out = []
    for hz in (880, 1320):
        for i in range(n):
            t, back = i / sr, (n - 1 - i) / sr
            env = min(1.0, min(t, back) / 0.01)
            out.append(int(math.sin(2 * math.pi * hz * t) * env * 0.35 * 32767))
    return out


def write(path):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<%dh" % len(samples()), *samples()))


if __name__ == "__main__":
    write(sys.argv[1])
