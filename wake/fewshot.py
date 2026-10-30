"""Few-shot wake words: match the user's own recordings on the Pi, with no training step.

openWakeWord already turns every 80 ms of audio into a 96-number speech embedding (melspectrogram.onnx +
embedding_model.onnx). Hey Grok's classifier reads that stream; so does this. At enrollment each recording
becomes a template: the embedding frames that cover the spoken word. At runtime a streaming subsequence
DTW (cosine cost, steps that advance the template by 0, 1 or 2 rows per frame) scores the newest frame
against every template at once. The word's score is the mean of its best two template scores, 0..1.

Pure numpy. The embedder is passed in, so tests can use a fake one.
"""
import numpy as np

FRAME_S = 0.08         # one embedding per 80 ms
WINDOW_S = 0.76        # each embedding looks at the last 760 ms of audio
LEAD_S = 1.0           # quiet audio fed before a clip so the first frames have context, as in the live stream
MAX_ROWS = 18          # template length cap (1.44 s of frames)
MIN_ROWS = 4
MAX_TEMPLATES = 20     # CPU cap: at most 20 recordings per word are kept (the most typical ones)
STAY_PENALTY = 0.08    # holding on one template row costs a little more, so noise can't idle along a template
TOPK = 2
MARGIN = 0.03          # the weakest recording kept must clear the loudest negative by this much
# bq-0019 (Nix GO with conditions, Oct 30 2026): two recorded words may sound alike if the station still tells
# them apart. SEP_MARGIN is a floor (C5): never below MARGIN; a change goes back to Nix with measured margins.
SEP_MARGIN = max(0.05, MARGIN)
MIN_PAIR_TEMPLATES = 3  # C1: a word with fewer saved templates cannot be checked, so the pair fails
PATIENCE = 2            # the listener's FEWSHOT_PATIENCE: consecutive frames at or over the threshold
VERSION = 1


def _unit(a):
    a = np.asarray(a, np.float32)
    return a / (np.linalg.norm(a, axis=-1, keepdims=True) + 1e-6)


def clip_frames(emb, clip_len_s, lead_s=LEAD_S):
    """Template rows: frames whose 760 ms window ends inside the clip, after the first 0.3 s of it."""
    n = len(emb)
    ends = np.arange(n) * FRAME_S + WINDOW_S
    keep = np.where((ends >= lead_s + 0.3) & (ends <= lead_s + clip_len_s + 0.05))[0]
    rows = emb[keep] if len(keep) else emb[-MIN_ROWS:]
    if len(rows) > MAX_ROWS:  # keep the middle of a long clip
        a = (len(rows) - MAX_ROWS) // 2
        rows = rows[a:a + MAX_ROWS]
    return np.asarray(rows, np.float32)


def pad_clip(pcm, noise=None, lead_s=LEAD_S, tail_s=0.4):
    """Clip with quiet lead/tail. noise (int16) fills the pad when given, so pads sound like the room."""
    pcm = np.asarray(pcm, np.int16)
    nl, nt = int(lead_s * 16000), int(tail_s * 16000)
    if noise is not None and len(noise) >= 1600:
        reps = int(np.ceil((nl + nt) / len(noise))) + 1
        bed = np.tile(np.asarray(noise, np.int16), reps)
        return np.concatenate([bed[:nl], pcm, bed[nl:nl + nt]])
    rng = np.random.default_rng(len(pcm))
    q = lambda n: (rng.standard_normal(n) * 30).astype(np.int16)
    return np.concatenate([q(nl), pcm, q(nt)])


class Matcher:
    """Streaming template match for one word. feed(unit embedding) -> score 0..1."""

    def __init__(self, templates):
        ts = [np.asarray(t, np.float32) for t in templates if len(t) >= 2]
        if not ts:
            raise ValueError("no templates")
        self.R = _unit(np.concatenate(ts))               # all rows stacked: one matmul per frame
        lens = [len(t) for t in ts]
        starts = np.cumsum([0] + lens[:-1])
        self.ends = starts + np.array(lens) - 1
        idx = np.arange(len(self.R))
        tid = np.repeat(np.arange(len(ts)), lens)
        self.first = np.isin(idx, starts)
        self.ok1 = ~self.first                            # row i-1 is the same template
        self.ok2 = self.ok1 & np.concatenate([[False], self.ok1[:-1]]) & (tid == np.roll(tid, 2))
        self.n = len(ts)
        self.reset()

    def reset(self):
        self.D = np.full(len(self.R), np.inf, np.float32)
        self.L = np.ones(len(self.R), np.float32)

    def feed(self, e):
        c = 1.0 - self.R @ e                              # cosine distance of every row to this frame
        D, L = self.D, self.L
        cand = np.stack([D + STAY_PENALTY * L,            # stay on row i (cost grows with path)
                         np.where(self.ok1, np.roll(D, 1), np.inf),
                         np.where(self.ok2, np.roll(D, 2), np.inf)])
        lens = np.stack([L, np.roll(L, 1), np.roll(L, 2)])
        avg = cand / lens
        k = np.argmin(avg, 0)
        ar = np.arange(len(D))
        best, bl = cand[k, ar], lens[k, ar]
        newD = np.where(self.first, c, best + c)
        newL = np.where(self.first, 1.0, bl + 1.0)
        bad = ~np.isfinite(newD)
        newD[bad], newL[bad] = np.inf, 1.0
        self.D, self.L = newD.astype(np.float32), newL.astype(np.float32)
        s = 1.0 - self.D[self.ends] / self.L[self.ends]
        s = np.where(np.isfinite(s), s, 0.0)
        top = np.sort(s)[-min(TOPK, len(s)):]
        return float(max(0.0, top.mean()))


def reps_needed(n):
    """Leave-one-out recordings that must be heard. 80% of whatever was kept: 4 of 5, 8 of 10."""
    return int(np.ceil(0.8 * n))


def peak(matcher, frames):
    matcher.reset()
    p = 0.0
    for e in _unit(frames):
        p = max(p, matcher.feed(e))
    matcher.reset()
    return p


def race(a, thr_a, b, thr_b, frames, patience=PATIENCE):
    """Replay the listener's Detector on one stream with two words: "a", "b" or None, plus each peak.

    Same rule as grok_wake.Detector.feed: a word hits after `patience` frames in a row at or over its
    threshold; on the first frame with a hit, the higher score wins. A NaN score raises ValueError (C1)."""
    a.reset(); b.reset()
    na = nb = 0
    pa = pb = 0.0
    winner = None
    for e in _unit(frames):
        sa, sb = a.feed(e), b.feed(e)
        if not (np.isfinite(sa) and np.isfinite(sb)):
            raise ValueError("score")
        pa, pb = max(pa, sa), max(pb, sb)
        na = na + 1 if sa >= thr_a else 0
        nb = nb + 1 if sb >= thr_b else 0
        if winner is None:
            hits = [(s, k) for s, k, n in ((sa, "a", na), (sb, "b", nb)) if n >= patience]
            if hits:
                winner = max(hits)[1]
    a.reset(); b.reset()
    return winner, pa, pb


def pair_check(tmpl, word_frames, thr, other_templates, other_thr):
    """bq-0019: can the listener tell this word from another recorded word? Returns a dict of numbers only.

    ok needs all of: no recording of this word goes to the other word; no saved template of the other word goes
    to this word; and on at least 80% of this word's recordings, this word's peak beats the other's by SEP_MARGIN.
    Both sides are leave-one-out: a recording is never scored against its own template (C6c). Fewer than
    MIN_PAIR_TEMPLATES on either side, a bad threshold, or a NaN fails closed (C1)."""
    out = {"ok": False, "misroutes": 0, "min_margin": None, "pass_rate": 0.0}
    try:
        ot = [np.asarray(t, np.float32) for t in (other_templates or [])]
        if len(tmpl) < MIN_PAIR_TEMPLATES or len(ot) < MIN_PAIR_TEMPLATES or len(word_frames) != len(tmpl):
            return out
        thr, other_thr = float(thr), float(other_thr)
        if not (np.isfinite(thr) and np.isfinite(other_thr)):
            return out
        full_other, margins, mis = Matcher(ot), [], 0
        for j in range(len(tmpl)):
            mine = Matcher(tmpl[:j] + tmpl[j + 1:])
            win, pa, pb = race(mine, thr, full_other, other_thr, word_frames[j])
            mis += win == "b"
            margins.append(pa - pb)
        full_mine = Matcher(tmpl)
        for k in range(len(ot)):
            theirs = Matcher(ot[:k] + ot[k + 1:])
            win, _, _ = race(full_mine, thr, theirs, other_thr, ot[k])
            mis += win == "a"
        if not all(np.isfinite(m) for m in margins):
            return out
        rate = sum(m >= SEP_MARGIN for m in margins) / len(margins)
        out.update(misroutes=int(mis), min_margin=round(float(min(margins)), 3), pass_rate=round(float(rate), 3))
        out["ok"] = mis == 0 and sum(m >= SEP_MARGIN for m in margins) >= reps_needed(len(margins))
    except (ValueError, TypeError, FloatingPointError):
        out["ok"] = False
    return out


def calibrate(word_frames, word_lens, negatives, others=()):
    """Pick templates and a threshold from one word's recordings.

    word_frames: per recording, the padded clip's full embedding stream. word_lens: clip seconds.
    negatives: embedding streams that must not trigger (room noise, generic speech, other words).
    others: (name, Matcher) for the other enrolled words. The Matcher carries .threshold and .templates (its saved
    templates). Every pair is checked both ways with pair_check (bq-0019); a Matcher without them fails closed.
    Returns a dict: ok, threshold, templates, pos (leave-one-out peaks), neg_max, heard, message.
    """
    tmpl = [clip_frames(f, s) for f, s in zip(word_frames, word_lens)]
    n = len(tmpl)
    if n < 3:
        return {"ok": False, "message": "Not good enough yet. Too few recordings were kept.", "pos": [], "neg_max": 0.0}
    pos = []
    for j in range(n):  # leave one out: can the other recordings hear this one?
        pos.append(peak(Matcher(tmpl[:j] + tmpl[j + 1:]), word_frames[j]))
    order = np.argsort(pos)[::-1]
    keep = sorted(order[:MAX_TEMPLATES].tolist())
    m = Matcher([tmpl[i] for i in keep])
    neg = [peak(m, f) for f in negatives]
    neg_max = max(neg, default=0.0)
    pos_s = sorted(pos, reverse=True)
    need = reps_needed(n)
    floor = pos_s[need - 1]                          # the weakest of the recordings we must hear
    thr = float(np.clip(max(neg_max + MARGIN, (floor + neg_max) / 2), 0.5, 0.97))
    collide, pairs = [], {}
    for name, om in others:  # every other recorded word, both ways (C2); Hey Grok is checked by the listener (C3)
        pc = pair_check(tmpl, word_frames, thr, getattr(om, "templates", None), getattr(om, "threshold", None))
        pairs[name] = pc
        if not pc["ok"]:
            collide.append(name)
    heard = int(sum(p >= thr for p in pos))
    ok = heard >= need and floor >= neg_max + MARGIN and not collide
    if collide:
        msg = f"Not good enough yet. It sounds too much like {', '.join(collide)}. Try a different phrase."
    elif ok:
        msg = f"Ready. It heard {heard} of {n} recordings."
    else:
        msg = (f"Not good enough yet. It heard {heard} of {n} recordings. "
               "Train more to add 5 more recordings, somewhere quiet.")
    return {"ok": bool(ok), "threshold": round(thr, 3), "templates": [tmpl[i] for i in keep], "pos": [round(p, 3) for p in pos],
            "neg_max": round(neg_max, 3), "heard": heard, "count": n, "collide": collide, "pairs": pairs, "message": msg}


def save(path, templates, meta):
    flat = np.concatenate(templates).astype(np.float16)
    np.savez(path, rows=flat, lens=np.array([len(t) for t in templates], np.int32), version=np.int32(VERSION))


def load(path):
    with np.load(path) as z:
        rows, lens = z["rows"].astype(np.float32), z["lens"]
    out, a = [], 0
    for n in lens:
        out.append(rows[a:a + int(n)]); a += int(n)
    return out
