#!/usr/bin/env python3
"""enroll_feats.py <session_dir> <job_dir> [rounds]: split a session's real clips 80/20 (seeded), write
<job_dir>/heldout.txt and <job_dir>/real_features_train.npy (train clips x rounds, augmented with the same RIRs and
background audio as the synthetic set, through openWakeWord's own feature pipeline).
Env TRAIN_N + HELD_FILE: reduced-clip check (first N train clips, evaluated on HELD_FILE's held-out clips)."""
import glob, os, random, sys, numpy as np
from openwakeword.data import augment_clips
from openwakeword.utils import compute_features_from_generator

ses, job = sys.argv[1], sys.argv[2]
rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 200
import json
clips = sorted(glob.glob(os.path.join(ses, "[0-9][0-9].wav")))
try:  # top-up session: also train/evaluate on the earlier session it tops up (same phrase, same speaker)
    prior = json.load(open(os.path.join(ses, "READY"))).get("prior")
except (OSError, ValueError):
    prior = None
if prior:
    clips = sorted(glob.glob(os.path.join(os.path.dirname(os.path.normpath(ses)), prior, "[0-9][0-9].wav"))) + clips
assert len(clips) >= 10 or os.environ.get("TRAIN_N"), f"need 10+ clips, have {len(clips)}"
rng = random.Random(len(clips))
held = sorted(rng.sample(clips, max(3, len(clips) // 5)))
if os.environ.get("HELD_FILE"):  # reduced-clip check: reuse another job's held-out set so results compare directly
    held = [l.strip() for l in open(os.environ["HELD_FILE"]) if l.strip()]
    hb = {os.path.basename(c) for c in held}  # check dirs hold symlinks to the same file names
    train = [c for c in clips if os.path.basename(c) not in hb]
else:
    train = [c for c in clips if c not in held]
if os.environ.get("TRAIN_N"):    # train on only the first N clips (in recording order) outside the held-out set
    train = train[:int(os.environ["TRAIN_N"])]
os.makedirs(job, exist_ok=True)
open(os.path.join(job, "heldout.txt"), "w").write("\n".join(held) + "\n")
open(os.path.join(job, "train.txt"), "w").write("\n".join(train) + "\n")
bg = glob.glob("data/background/*.wav")
rirs = glob.glob("data/rirs/*.wav")
total = 32000
items = train * rounds
gen = augment_clips(items, total_length=total, batch_size=16, background_clip_paths=bg, RIR_paths=rirs)
out = os.path.join(job, "real_features_train.npy")
compute_features_from_generator(gen, n_total=len(items), clip_duration=total, output_file=out, device="cpu",
                                ncpu=int(os.environ.get("OWW_NCPU", 4)))
print(f"real clips: {len(train)} train x {rounds} rounds -> {out}; {len(held)} held out")
