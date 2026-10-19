#!/usr/bin/env python3
"""enroll_merge.py <synthetic run dir> <job dir> <feature dir>: feature dir = symlinks to the synthetic run's files,
except positive_features_train.npy = synthetic + real positives (concatenated, float16 like the originals)."""
import os, sys, numpy as np
src, job, dst = sys.argv[1:4]
os.makedirs(dst, exist_ok=True)
for f in ("positive_test", "negative_test", "negative_features_train.npy", "positive_features_test.npy", "negative_features_test.npy"):
    p = os.path.join(dst, f)
    if os.path.lexists(p):
        os.remove(p)
    os.symlink(os.path.abspath(os.path.join(src, f)), p)
syn = np.load(os.path.join(src, "positive_features_train.npy"), mmap_mode="r")
real = np.load(os.path.join(job, "real_features_train.npy"), mmap_mode="r").astype(syn.dtype)
out = np.lib.format.open_memmap(os.path.join(dst, "positive_features_train.npy"), mode="w+", dtype=syn.dtype,
                                shape=(syn.shape[0] + real.shape[0],) + syn.shape[1:])
out[:syn.shape[0]] = syn; out[syn.shape[0]:] = real; out.flush()
print(f"positives: {syn.shape[0]} synthetic + {real.shape[0]} real")
