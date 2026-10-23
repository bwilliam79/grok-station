#!/usr/bin/env python3
"""Make synthetic audio for tools/fewshot_eval.py with Piper (a local text-to-speech tool). Dev only, never on the Pi.

  python3 tools/fewshot_tts.py VOICE.onnx OUT_DIR
Writes OUT_DIR/<phrase>/<speaker>_<n>.wav (the wake phrases) and OUT_DIR/_speech/<n>.wav (everyday sentences),
as 16 kHz mono.
"""
import os, sys, wave, io
import numpy as np
from piper import PiperVoice, SynthesisConfig
from scipy.signal import resample_poly

PHRASES = ["hey rhea", "hey ash", "hey jules", "okay milo"]
CONFUSE = ["hey ray", "hey grok", "hey josh", "hey you", "okay", "hey there", "hello", "hey Leah", "jewels",
           "a rhea", "rear", "the ash tray", "hey Julie", "my low", "okay Mike"]
SENTENCES = [
    "What's the weather going to be like tomorrow?", "Can you turn the lights off in the kitchen?",
    "I think we should leave around six thirty.", "Did anyone feed the dog this morning?",
    "The game starts at seven on channel four.", "Put the leftovers in the fridge please.",
    "We're out of milk again.", "Hey, can you hear me in the other room?", "Let's watch a movie tonight.",
    "I'll call you back in a few minutes.", "Where did I leave my keys?", "That was a really good dinner.",
    "Remind me to pay the electric bill.", "The kids have practice after school.", "It's way too cold out there.",
    "Okay, I'm heading out now.", "Have you seen the new episode yet?", "Turn the music down a little.",
    "I need to stop by the store on the way home.", "Happy birthday, we love you!",
]
SPEAKERS = [3, 17, 40, 88, 120, 205, 333, 410, 512, 640, 777, 860]


def synth(v, text, spk, ls, ns):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        v.synthesize_wav(text, w, syn_config=SynthesisConfig(speaker_id=spk, length_scale=ls, noise_scale=ns))
    buf.seek(0)
    with wave.open(buf) as r:
        sr, x = r.getframerate(), np.frombuffer(r.readframes(r.getnframes()), "<i2").astype(np.float32)
    y = resample_poly(x, 16000, sr) * 0.5
    return np.clip(y, -32768, 32767).astype("<i2")


def put(path, x):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(x.tobytes())


def main(model, out):
    v = PiperVoice.load(model)
    rng = np.random.default_rng(7)
    for ph in PHRASES:
        for spk in SPEAKERS:
            for n in range(30):
                put(f"{out}/{ph.replace(' ', '_')}/{spk}_{n:02d}.wav",
                    synth(v, ph, spk, float(rng.uniform(0.8, 1.3)), float(rng.uniform(0.4, 0.9))))
    k = 0
    for spk in SPEAKERS:
        for t in SENTENCES + CONFUSE:
            put(f"{out}/_speech/{k:04d}.wav", synth(v, t, spk, float(rng.uniform(0.85, 1.2)), 0.667)); k += 1


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
