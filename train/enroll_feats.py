#!/usr/bin/env python3
"""enroll_feats.py <session_dir> <job_dir> [rounds]: split a session's real clips 80/20 (seeded), write
<job_dir>/heldout.txt and <job_dir>/real_features_train.npy (train clips x rounds, augmented with the same RIRs and
background audio as the synthetic set, through openWakeWord's own feature pipeline)."""
import glob, os, random, sys, numpy as np
from openwakeword.data import augment_clips
from openwakeword.utils import compute_features_from_generator

ses, job = sys.argv[1], sys.argv[2]
rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 200
clips = sorted(glob.glob(os.path.join(ses, "[0-9][0-9].wav")))
assert len(clips) >= 10, f"need 10+ clips, have {len(clips)}"
rng = random.Random(len(clips))
held = sorted(rng.sample(clips, max(3, len(clips) // 5)))
train = [c for c in clips if c not in held]
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
