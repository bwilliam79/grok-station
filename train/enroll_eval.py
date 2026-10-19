#!/usr/bin/env python3
"""enroll_eval.py <phrase> <model.onnx> <report.json> <job dir> <publish dir> <persona>
Recall on held-out REAL clips (clean + background-mixed at 15/10/5 dB) for each threshold/patience, false wakes per
hour from the evaluate2 report (10.7 h validation set). Gate: among configs with FA/h <= 1.0, take the best real
recall; publish (model + json with sha256) only if it is >= 0.80. Writes <job dir>/result.json either way."""
import glob, hashlib, json, os, random, shutil, sys, time, numpy as np, scipy.io.wavfile as wf
from openwakeword.model import Model
m, onnx_path, report, job, pub, persona = sys.argv[1:7]
TH = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99, 0.995]; PAT = [1, 2, 3]
fa = json.load(open(report))["fa_per_hour"][m]
syn = json.load(open(report))["recall"][m]
held = [l.strip() for l in open(os.path.join(job, "heldout.txt")) if l.strip()]
bg = sorted(glob.glob("data/background/*.wav")); random.seed(7)
def load(p): return wf.read(p)[1].astype(np.float32)
def mix(a, snr):
    L = len(a) + 32000; x = np.zeros(L, np.float32); x[16000:16000 + len(a)] = a
    if snr is None: return x.astype(np.int16)
    b = load(random.choice(bg))
    if len(b) < L: b = np.tile(b, L // max(len(b), 1) + 1)
    s = random.randrange(len(b) - L + 1); b = b[s:s + L]
    pa, pb = np.mean(a ** 2) + 1e-9, np.mean(b ** 2) + 1e-9
    return np.clip(x + b * np.sqrt(pa / (pb * 10 ** (snr / 10))), -32768, 32767).astype(np.int16)
oww = Model(wakeword_models=[onnx_path], inference_framework="onnx"); key = next(iter(oww.models))
def runmin(x, p): return x if p == 1 else np.lib.stride_tricks.sliding_window_view(x, p).min(1)
peaks = []
for c in held:
    for snr in (None, 15, 10, 5):
        oww.reset(); x = mix(load(c), snr)
        s = np.array([oww.predict(x[i:i + 1280])[key] for i in range(0, len(x) - 1280, 1280)])
        peaks.append([float(runmin(s, p).max()) for p in PAT])
peaks = np.array(peaks)
rec = {f"{t}|{p}": round(float((peaks[:, j] >= t).mean()), 3) for t in TH for j, p in enumerate(PAT)}
cands = [k for k in rec if fa[k] <= 1.0]
best = max(cands, key=lambda k: (rec[k], -fa[k], -int(k.split("|")[1]))) if cands else None
res = {"name": m, "persona": persona, "trials": len(peaks), "heldout_clips": len(held), "real_recall": rec,
       "chosen": best, "passed": bool(best and rec[best] >= 0.8)}
if best:
    t, p = best.split("|")
    res.update(threshold=float(t), patience=int(p), recall_real=rec[best], fa_h=fa[best], recall_synthetic=syn[best]["all"])
json.dump(res, open(os.path.join(job, "result.json"), "w"), indent=1)
print(json.dumps({k: v for k, v in res.items() if k != "real_recall"}))
if res["passed"]:
    os.makedirs(pub, exist_ok=True)
    shutil.copy(onnx_path, os.path.join(pub, f".{m}.onnx.tmp")); os.replace(os.path.join(pub, f".{m}.onnx.tmp"), os.path.join(pub, f"{m}.onnx"))
    meta = {k: res[k] for k in ("name", "persona", "threshold", "patience", "recall_real", "fa_h", "recall_synthetic")}
    meta.update(sha256=hashlib.sha256(open(os.path.join(pub, f"{m}.onnx"), "rb").read()).hexdigest(), trained=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    json.dump(meta, open(os.path.join(pub, f".{m}.json.tmp"), "w"), indent=1); os.replace(os.path.join(pub, f".{m}.json.tmp"), os.path.join(pub, f"{m}.json"))
    print(f"PUBLISHED {m}")
sys.exit(0 if res["passed"] else 3)
