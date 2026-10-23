#!/usr/bin/env python3
"""Offline eval for few-shot wake words (wake/fewshot.py) on audio from tools/fewshot_tts.py. Dev only.

  python tools/fewshot_eval.py DATA_DIR [--bank OUT.npz]
Needs openwakeword + onnxruntime (the wake venv). For each phrase and each of 4 enrolling voices: enroll from
reps 0-9 with the station's own calibration, then score
  same-voice true accepts : reps 10-29 of that voice, room noise mixed in at 15 dB SNR
  other-voice accepts     : the phrase from 8 other voices (shows it is tuned to the enrolling voice)
  false accepts           : a continuous stream of everyday sentences, near-miss phrases and the other wake
                            phrases from voices not used to build the generic-speech bank, in room noise.
--bank writes the generic-speech negative bank (voices 3..205 only) the station ships.
Synthetic voices are cleaner and more alike than real rooms; treat the numbers as a sanity check only.
"""
import glob, os, sys, time, wave
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "wake"))
import fewshot as fs
from grok_wake import trim_clip
from openwakeword.utils import AudioFeatures

BANK_SPK = {"3", "17", "40", "88", "120", "205"}
ENROLLERS = ["333", "410", "640", "860"]
rng = np.random.default_rng(1)
AF = AudioFeatures(inference_framework="onnx")


def rd(p):
    with wave.open(p) as w:
        z = np.zeros(4800, np.int16)  # TTS has no silence around the words; a real recording does
        return np.concatenate([z, np.frombuffer(w.readframes(w.getnframes()), "<i2"), z])


def room(n, db=-55):
    x = np.cumsum(rng.standard_normal(n)) ; x -= np.convolve(x, np.ones(400) / 400, "same")  # brown-ish hum
    x = x / (np.sqrt(np.mean(x ** 2)) + 1e-9) * 32768 * 10 ** (db / 20)
    return (x + rng.standard_normal(n) * 32768 * 10 ** ((db - 6) / 20)).astype(np.float32)


def mix(x, snr=15):
    x = x.astype(np.float32)
    s = np.sqrt(np.mean(x ** 2)) + 1e-9
    n = room(len(x)); n *= s / (np.sqrt(np.mean(n ** 2)) + 1e-9) / 10 ** (snr / 20)
    return np.clip(x + n, -32768, 32767).astype(np.int16)


def emb(x):
    return AF._get_embeddings(np.asarray(x, np.int16)).astype(np.float32)


def stream_hits(m, frames, thr, patience=2, cooldown=25):
    m.reset(); hits, run, last = 0, 0, -999
    for i, e in enumerate(fs._unit(frames)):
        s = m.feed(e)
        run = run + 1 if s >= thr else 0
        if run >= patience and i - last >= cooldown:
            hits += 1; last = i; run = 0
    return hits


def main(d, bank_out=None):
    phrases = sorted(p for p in os.listdir(d) if not p.startswith("_"))
    speech = sorted(glob.glob(f"{d}/_speech/*.wav"))
    n_sent = len(speech) // 12  # 12 voices in order
    spk_of = lambda i: ["3", "17", "40", "88", "120", "205", "333", "410", "512", "640", "777", "860"][i // n_sent]
    bank_clips = [rd(p) for i, p in enumerate(speech) if spk_of(i) in BANK_SPK]
    bank = [emb(fs.pad_clip(c, room(16000).astype(np.int16))) for c in bank_clips]
    if bank_out:
        np.savez_compressed(bank_out, rows=np.concatenate(bank).astype(np.float16), lens=np.array([len(b) for b in bank], np.int32))
        print(f"bank: {len(bank)} sentences, {sum(map(len, bank))} frames -> {bank_out}")
    # false-accept stream: held-out voices, everyday speech + near misses + every wake phrase from voices
    # other than the enroller (added per word below), 0.5-2 s gaps of room noise
    fa_parts = []
    for i, p in enumerate(speech):
        if spk_of(i) not in BANK_SPK:
            fa_parts += [mix(rd(p), 20), room(int(rng.uniform(0.5, 2) * 16000)).astype(np.int16)]
    fa_audio = np.concatenate(fa_parts)
    fa_emb = emb(fa_audio)
    fa_h = len(fa_audio) / 16000 / 3600
    print(f"false-accept stream: {len(fa_audio) / 16000 / 60:.1f} min of everyday speech + near misses")
    rows, cpu = [], []
    for ph in phrases:
        for spk in ENROLLERS:
            reps = [rd(f"{d}/{ph}/{spk}_{n:02d}.wav") for n in range(30)]
            noise = room(3 * 16000).astype(np.int16)
            enr = [trim_clip(mix(r, 25)) for r in reps[:10]]
            enr = [c for c in enr if c is not None]
            frames = [emb(fs.pad_clip(c, noise)) for c in enr]
            lens = [len(c) / 16000 for c in enr]
            negs = [emb(noise)] + bank
            for oph in phrases:  # other enrolled words by the same household voice
                if oph != ph:
                    negs += [emb(fs.pad_clip(trim_clip(rd(f"{d}/{oph}/{spk}_{n:02d}.wav")), noise)) for n in range(3)]
            cal = fs.calibrate(frames, lens, negs)
            m = fs.Matcher(cal["templates"]); thr = cal.get("threshold", 1.0)
            same = [fs.peak(m, emb(fs.pad_clip(mix(r, 15), noise))) >= thr for r in reps[10:]]
            other = [fs.peak(m, emb(fs.pad_clip(mix(rd(f"{d}/{ph}/{o}_00.wav"), 15), noise))) >= thr
                     for o in ["3", "17", "40", "88", "120", "205", "512", "777"]]
            # the other phrases, by every voice, appended to the FA stream for this word
            near = np.concatenate([np.concatenate([mix(rd(f"{d}/{oph}/{o}_{n:02d}.wav"), 20), room(16000).astype(np.int16)])
                                   for oph in phrases if oph != ph for o in ["512", "777"] for n in range(5)])
            ne = emb(near)
            t = time.process_time()
            fa = stream_hits(m, fa_emb, thr) + stream_hits(m, ne, thr)
            cpu.append((time.process_time() - t) / (len(fa_emb) + len(ne)) * 1000)
            hours = fa_h + len(near) / 16000 / 3600
            rows.append((ph, spk, cal["ok"], cal["heard"], thr, len(cal["templates"]), np.mean(same), np.mean(other), fa, hours))
            print(f"{ph:10s} voice {spk:>3s}: gate {'pass' if cal['ok'] else 'FAIL'} ({cal['heard']}/10) thr {thr:.3f} "
                  f"neg_max {cal['neg_max']:.3f} | same-voice TA {np.mean(same) * 100:5.1f}%  other-voice {np.mean(other) * 100:5.1f}% "
                  f"| FA {fa} in {hours * 60:.1f} min", flush=True)
    ok = [r for r in rows if r[2]]
    print(f"\n{len(ok)}/{len(rows)} enrollments passed the gate")
    if ok:
        fa_tot, h_tot = sum(r[8] for r in ok), sum(r[9] for r in ok)
        print(f"passed words: same-voice true accept {np.mean([r[6] for r in ok]) * 100:.1f}%, "
              f"other-voice accept {np.mean([r[7] for r in ok]) * 100:.1f}%, "
              f"false accepts {fa_tot} in {h_tot:.2f} h = {fa_tot / h_tot:.1f}/h")
    print(f"matcher CPU: {np.mean(cpu):.3f} ms per 80 ms frame per word on this machine")


if __name__ == "__main__":
    a = sys.argv[1:]
    bank = a[a.index("--bank") + 1] if "--bank" in a else None
    main(a[0], bank)
