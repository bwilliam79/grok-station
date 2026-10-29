#!/usr/bin/env python3
"""Home-command tones. Stdlib only (wave + math). Does not replace gen.py.

16-bit mono, 16 kHz. home-ok is a rising two-note about 0.25 s.
home-fail is a low descending buzz about 0.40 s. Peak is about half of full scale.
"""
import math, os, struct, wave

SR = 16000
PEAK = 16000  # about half of 32767


def _i16(x):
    n = int(round(x))
    if n > 32767:
        return 32767
    if n < -32768:
        return -32768
    return n


def _scale(raw):
    peak = max(abs(s) for s in raw) or 1.0
    gain = PEAK / peak
    return [_i16(s * gain) for s in raw]


def _note(freq, dur):
    n = int(round(SR * dur))
    attack = int(SR * 0.008)
    release = int(SR * 0.018)
    out = []
    for i in range(n):
        s = math.sin(2.0 * math.pi * freq * (i / SR))
        env = 1.0
        if attack and i < attack:
            env = i / attack
        tail = n - 1 - i
        if release and tail < release:
            env *= tail / release
        out.append(s * env)
    return out


def render_ok():
    """C5, a short gap, then G5. 0.10 + 0.02 + 0.13 = 0.25 s."""
    gap = [0.0] * int(round(SR * 0.02))
    return _scale(_note(523.25, 0.10) + gap + _note(783.99, 0.13))


def render_fail():
    """Descend from 196 Hz to 98 Hz with a third harmonic and a slow buzz. 0.40 s."""
    n = int(round(SR * 0.40))
    attack = int(SR * 0.015)
    release = int(SR * 0.04)
    phase = 0.0
    out = []
    for i in range(n):
        frac = i / (n - 1)
        freq = 196.0 + (98.0 - 196.0) * frac
        phase += 2.0 * math.pi * freq / SR
        s = math.sin(phase) + 0.45 * math.sin(3.0 * phase)
        s *= 0.75 + 0.25 * math.sin(2.0 * math.pi * 28.0 * (i / SR))
        env = 1.0
        if attack and i < attack:
            env = i / attack
        tail = n - 1 - i
        if release and tail < release:
            env *= tail / release
        out.append(s * env)
    return _scale(out)


def write_wav(path, samples):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))


def render(out_dir=None):
    """Write home-ok.wav and home-fail.wav. Returns {name: samples}. Same bytes every run."""
    out = out_dir or os.path.dirname(os.path.abspath(__file__))
    os.makedirs(out, exist_ok=True)
    files = {"home-ok": render_ok(), "home-fail": render_fail()}
    for name, samples in files.items():
        write_wav(os.path.join(out, name + ".wav"), samples)
    return files


if __name__ == "__main__":
    render()
