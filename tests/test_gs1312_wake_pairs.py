#!/usr/bin/env python3
"""bq-0019: a recorded wake word close to another one (Hey Jules / Hey Miles) passes when the listener tells them
apart (Nix GO with conditions C1-C6, Oct 30 2026). Fake embeddings only; no audio, no models."""
import json, math, os, stat, sys, tempfile, unittest
import numpy as np
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "wake"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fewshot as fs  # noqa: E402
import test_fewshot as tf  # noqa: E402

g = np.random.default_rng(5)
HEY = g.standard_normal((3, 96)).astype(np.float32)                 # the shared "Hey"
JULES = np.vstack([HEY, g.standard_normal((9, 96))]).astype(np.float32)
MILES = np.vstack([HEY, g.standard_normal((9, 96))]).astype(np.float32)
NEG = g.standard_normal((12, 96)).astype(np.float32)   # an unrelated word: room speech


class Pairs(tf.Fresh):
    def setUp(self):
        super().setUp()
        self._cf = fs.clip_frames
        fs.clip_frames = lambda f, s, lead_s=fs.LEAD_S: f[14:-5]
        self.addCleanup(setattr, fs, "clip_frames", self._cf)

    def _word(self, base, n=10, noise=0.25):
        frames, lens = [], []
        for _ in range(n):
            s, d = tf.say(base, noise)
            frames.append(s); lens.append(d)
        return frames, lens

    def _enrolled(self, base, n=10):
        """An already-built word as the listener loads it: saved templates plus its own threshold."""
        frames, lens = self._word(base, n)
        cal = fs.calibrate(frames, lens, [tf.say(NEG)[0]])
        self.assertTrue(cal["ok"], cal["message"])
        om = fs.Matcher(cal["templates"]); om.threshold = cal["threshold"]; om.templates = cal["templates"]
        return om

    def test_a_near_duplicate_pair_is_rejected(self):  # C6a
        other = self._enrolled(JULES)
        frames, lens = self._word(JULES)
        cal = fs.calibrate(frames, lens, [tf.say(NEG)[0]], others=[("Hey Twin", other)])
        self.assertFalse(cal["ok"])
        self.assertEqual(cal["message"], "Not good enough yet. It sounds too much like Hey Twin. Try a different phrase.")

    def test_a_close_pair_told_apart_passes(self):  # C6b: shares "Hey", the rest differs
        other = self._enrolled(MILES)
        frames, lens = self._word(JULES)
        cal = fs.calibrate(frames, lens, [tf.say(NEG)[0]], others=[("Hey Miles", other)])
        self.assertTrue(cal["ok"], (cal["message"], cal.get("pairs")))
        pc = cal["pairs"]["Hey Miles"]
        self.assertEqual(pc["misroutes"], 0)
        self.assertGreaterEqual(pc["pass_rate"], 0.8)

    def test_leave_one_out_excludes_the_recordings_own_template(self):  # C6c
        frames, lens = self._word(JULES, n=6)
        tmpl = [fs.clip_frames(f, s) for f, s in zip(frames, lens)]
        other = self._enrolled(MILES, n=6)
        built, seen = [], []
        real_init, real_race = fs.Matcher.__init__, fs.race

        def spy_init(m, templates):
            m._ids = {id(t) for t in templates}
            built.append(m)
            real_init(m, templates)

        def spy_race(a, ta, b, tb, stream, **kw):
            seen.append((getattr(a, "_ids", set()), getattr(b, "_ids", set()), id(stream)))
            return real_race(a, ta, b, tb, stream, **kw)

        fs.Matcher.__init__, fs.race = spy_init, spy_race
        try:
            fs.pair_check(tmpl, frames, 0.6, other.templates, other.threshold)
        finally:
            fs.Matcher.__init__, fs.race = real_init, real_race
        mine = [s for s in seen if any(s[2] == id(f) for f in frames)]
        self.assertEqual(len(mine), len(frames))
        for j, (a_ids, _, _) in enumerate(mine):
            self.assertNotIn(id(tmpl[j]), a_ids)
            self.assertEqual(len(a_ids), len(tmpl) - 1)
        theirs = [s for s in seen if s not in mine]
        self.assertEqual(len(theirs), len(other.templates))
        for k, (_, b_ids, stream) in enumerate(theirs):
            self.assertEqual(stream, id(other.templates[k]))
            self.assertNotIn(id(other.templates[k]), b_ids)

    def test_fail_closed_cases(self):  # C6d / C1
        frames, lens = self._word(JULES)
        far = self._enrolled(tf.OTHER)
        cases = {}
        few = fs.Matcher(far.templates[:2]); few.threshold = far.threshold; few.templates = far.templates[:2]
        cases["too few templates"] = few
        none = fs.Matcher(far.templates); none.threshold = far.threshold  # no .templates
        cases["templates missing"] = none
        nan = fs.Matcher(far.templates); nan.threshold = float("nan"); nan.templates = far.templates
        cases["NaN threshold"] = nan
        missing = fs.Matcher(far.templates); missing.templates = far.templates  # no .threshold
        cases["threshold missing"] = missing
        for why, om in cases.items():
            cal = fs.calibrate(frames, lens, [tf.say(NEG)[0]], others=[("Hey Far", om)])
            self.assertFalse(cal["ok"], why)
            self.assertIn("sounds too much like Hey Far", cal["message"], why)
        real_init, real_feed = fs.Matcher.__init__, fs.Matcher.feed
        theirs = {id(t) for t in far.templates}

        def tag(m, templates):
            m._nan = bool(theirs & {id(t) for t in templates})
            real_init(m, templates)
        fs.Matcher.__init__ = tag
        fs.Matcher.feed = lambda m, e: float("nan") if getattr(m, "_nan", False) else real_feed(m, e)
        try:
            cal = fs.calibrate(frames, lens, [tf.say(NEG)[0]], others=[("Hey Far", far)])
        finally:
            fs.Matcher.__init__, fs.Matcher.feed = real_init, real_feed
        self.assertFalse(cal["ok"], "NaN score")
        self.assertIn("sounds too much like Hey Far", cal["message"])
        # Control: the same far word with real scores passes, so the cases above failed for their own reason.
        self.assertTrue(fs.calibrate(frames, lens, [tf.say(NEG)[0]], others=[("Hey Far", far)])["ok"])

    def test_floor_is_never_below_margin(self):  # C5
        self.assertEqual(fs.SEP_MARGIN, 0.05)
        self.assertGreaterEqual(fs.SEP_MARGIN, fs.MARGIN)
        self.assertEqual(fs.PATIENCE, 3 - 1)


class Listener(unittest.TestCase):
    def setUp(self):
        import grok_wake as gw
        self.gw = gw
        self.src = open(os.path.join(ROOT, "wake", "grok_wake.py")).read()

    def test_hey_grok_check_unchanged(self):  # C6e / C3
        body = self.src[self.src.index("def _build_fewshot("):self.src.index("def _note_clip(")]
        self.assertIn("    if cal.get(\"templates\") and BUILTIN != phrase:\n        hg = hey_grok_hears(frames)\n"
                      "        cal[\"hey_grok\"] = round(hg, 3)\n        if hg >= THRESH:\n            cal[\"ok\"] = False\n"
                      "            cal[\"message\"] = \"Not good enough yet. It sounds too much like Hey Grok. Try a different phrase.\"",
                      body)
        self.assertLess(body.index("fs.calibrate("), body.index("hey_grok_hears(frames)"))  # a pair pass can't skip it
        self.assertEqual(self.gw.FEWSHOT_PATIENCE, fs.PATIENCE)

    def test_every_other_recorded_word_is_checked(self):  # C2
        body = self.src[self.src.index("def _build_fewshot("):self.src.index("def _note_clip(")]
        self.assertIn('if isinstance(w, dict) and w.get("kind") == "fewshot" and w.get("name") != phrase:', body)
        self.assertNotIn("negs += t", body)
        self.assertNotIn("nearest", body)

    def test_margin_log_numbers_and_ids_only_capped(self):  # C4
        d = tempfile.mkdtemp()
        p = os.path.join(d, "m.jsonl")
        om = fs.Matcher([tf.WORD]); om.word_id = "hey_miles"
        pairs = {"Hey Miles": {"ok": True, "misroutes": 0, "min_margin": 0.07, "pass_rate": 0.9}}
        for i in range(3000):
            self.gw.log_margins("hey_jules", [("Hey Miles", om)], pairs, path=p, now=1_700_000_000 + i)
        self.assertLessEqual(os.path.getsize(p), self.gw.MARGIN_LOG_MAX)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        rows = [json.loads(l) for l in open(p)]
        self.assertEqual(rows[-1]["t"], 1_700_000_000 + 2999)
        for r in rows:
            self.assertEqual(set(r), {"t", "word", "other", "ok", "misroutes", "min_margin", "pass_rate"})
            self.assertEqual((r["word"], r["other"]), ("hey_jules", "hey_miles"))
        self.assertEqual(os.listdir(d), ["m.jsonl"])
        self.assertNotIn("MARGIN_LOG)", self.src.replace("open(path", ""))  # written only, never read at runtime


class NixFix(unittest.TestCase):  # NIX-1312-01 / NIX-1312-02
    def setUp(self):
        import grok_wake as gw
        self.gw = gw
        self.om = fs.Matcher([tf.WORD]); self.om.word_id = "hey_miles"
        self.pairs = {"Hey Miles": {"ok": True, "misroutes": 0, "min_margin": 0.07, "pass_rate": 0.9}}

    def test_margin_log_tmp_symlink_is_not_followed(self):
        d = tempfile.mkdtemp()
        target = os.path.join(d, "victim.txt")
        with open(target, "w") as f:
            f.write("keep")
        p = os.path.join(d, "m.jsonl")
        os.symlink(target, p + ".tmp")
        self.gw.log_margins("hey_jules", [("Hey Miles", self.om)], self.pairs, path=p, now=1_700_000_000)
        self.assertEqual(open(target).read(), "keep")       # not written through
        self.assertFalse(os.path.islink(p))                  # the log is a real file, not the old link
        if os.path.exists(p):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            self.assertEqual(json.loads(open(p).read().splitlines()[-1])["word"], "hey_jules")

    def test_margin_log_fifo_does_not_block(self):  # NIX-1313-03
        import signal
        d = tempfile.mkdtemp()
        p = os.path.join(d, "m.jsonl")
        os.mkfifo(p, 0o600)

        class Hang(Exception):  # not an OSError, so log_margins cannot swallow it
            pass

        def fire(*a):
            raise Hang("blocked on a FIFO")
        old = signal.signal(signal.SIGALRM, fire)
        signal.alarm(3)
        try:
            self.gw.log_margins("hey_jules", [("Hey Miles", self.om)], self.pairs, path=p, now=1_700_000_000)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        self.assertTrue(stat.S_ISREG(os.lstat(p).st_mode))
        self.assertEqual(json.loads(open(p).read().splitlines()[-1])["word"], "hey_jules")

    def test_oversized_log_is_read_bounded_and_capped(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "m.jsonl")
        with open(p, "wb") as f:
            f.write((b"x" * 99 + b"\n") * 20000)            # 2 MB, about 30 times the cap
        reads = []
        real = os.read

        def spy(fd, n):
            reads.append(n)
            return real(fd, n)
        real_open = __builtins__["open"] if isinstance(__builtins__, dict) else __builtins__.open
        opened = []

        def no_open(path, mode="r", *a, **k):
            if str(path) == p and "r" in mode:
                opened.append(path)
            return real_open(path, mode, *a, **k)
        self.gw.os.read = spy
        self.gw.open = no_open
        try:
            self.gw.log_margins("hey_jules", [("Hey Miles", self.om)], self.pairs, path=p, now=1_700_000_000)
        finally:
            self.gw.os.read = real
            del self.gw.open
        self.assertEqual(opened, [])                         # no unbounded open().read() of the log
        self.assertTrue(reads and max(reads) <= self.gw.MARGIN_LOG_MAX, reads)
        self.assertLessEqual(os.path.getsize(p), self.gw.MARGIN_LOG_MAX)
        self.assertEqual(json.loads(open(p).read().splitlines()[-1])["word"], "hey_jules")

    def test_shared_hey_pair_with_a_good_margin_still_fails_on_misroutes(self):
        """5 of 12 rows shared: margins and pass rate look fine, but the other word fires first on "Hey"."""
        gg = np.random.default_rng(5)
        hey = gg.standard_normal((5, 96)).astype(np.float32)
        jules = np.vstack([hey, gg.standard_normal((7, 96))]).astype(np.float32)
        miles = np.vstack([hey, gg.standard_normal((7, 96))]).astype(np.float32)
        p = Pairs("test_a_close_pair_told_apart_passes")
        p.setUp()
        try:
            other = p._enrolled(miles)
            frames, lens = p._word(jules)
            cal = fs.calibrate(frames, lens, [tf.say(NEG)[0]], others=[("Hey Miles", other)])
        finally:
            p.doCleanups()
        pc = cal["pairs"]["Hey Miles"]
        self.assertGreaterEqual(pc["pass_rate"], 0.8, pc)
        self.assertGreaterEqual(pc["min_margin"], fs.SEP_MARGIN, pc)
        self.assertGreater(pc["misroutes"], 0, pc)
        self.assertFalse(pc["ok"], pc)
        self.assertFalse(cal["ok"])
        self.assertIn("sounds too much like Hey Miles", cal["message"])


if __name__ == "__main__":
    unittest.main()
