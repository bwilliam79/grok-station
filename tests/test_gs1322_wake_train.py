"""0.13.22 wake training: score the current set, clear one bot, and keep error text inside its row.

The success percentage is recordings since the last clear, then the most recent 20.
False rejects and false accepts are counts on that same set. Clear training deletes
one bot's sessions and stats. Another bot's sessions stay.
"""
import json, os, shutil, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin"))
sys.path.insert(0, os.path.join(ROOT, "wake"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_admin  # noqa: E402  (sets the admin test env before grok_admin is first imported)
ga = test_admin.ga
import fewshot as fs
import grok_wake as gw

JS = os.path.join(ROOT, "admin", "web", "app.js")
CSS = os.path.join(ROOT, "admin", "web", "admin.css")


def _fn(src, name):
    i = src.index("function " + name + "(")
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]
    raise AssertionError("unclosed " + name)


def _session(root, phrase, ses, n=5):
    d = os.path.join(root, phrase, ses)
    os.makedirs(d, exist_ok=True)
    mark = int(ses.replace("-", "")) % 251 + 1
    for i in range(n):
        with open(os.path.join(d, "%02d.wav" % i), "wb") as fh:
            fh.write(bytes([(mark + i) & 0xFF, mark & 0xFF]) * 40)
    with open(os.path.join(d, "session.json"), "w") as f:
        json.dump({"phrase": phrase, "count": n}, f)
    return d


class ScoreWindow(unittest.TestCase):
    def test_history_before_clear_and_past_the_cap_is_not_scored(self):
        old = [("20260101-000000", i) for i in range(100)]
        recent = [("20261101-%06d" % i, i) for i in range(25)]
        kept = fs.current_training_set(old + recent, "20260101-000000", fs.SCORE_WINDOW)
        self.assertEqual(len(kept), 20)
        self.assertTrue(all(item[0].startswith("20261101-") for item in kept))
        self.assertEqual([item[1] for item in kept], list(range(5, 25)))
        # No clear yet: the cap still drops the oldest of a long run.
        capped = fs.current_training_set(old + recent, "", 20)
        self.assertEqual(len(capped), 20)
        self.assertNotIn("20260101-000000", [item[0] for item in capped])

    def test_percentage_is_the_window_not_the_old_failures(self):
        history = [False] * 100
        window = [True, True, True, False]
        counts = fs.training_counts(window, [False, True, False])
        self.assertEqual(counts["count"], 4)
        self.assertEqual(counts["success_pct"], 75)
        self.assertEqual(counts["false_rejects"], 1)
        self.assertEqual(counts["false_accepts"], 1)
        self.assertEqual(counts["false_reject_rate"], 0.25)
        self.assertNotEqual(counts["success_pct"], int(round(100 * (sum(window)) / (len(history) + len(window)))))
        self.assertIn("75% of this set. False rejects 1. False accepts 1.", fs.score_tail(counts))

    def test_listener_scores_only_the_current_paths(self):
        tmp = tempfile.mkdtemp(prefix="gs-score-")
        old = gw.ENROLL_DIR
        gw.ENROLL_DIR = tmp
        try:
            for n, ses in enumerate(("20261001-000000", "20261002-000000", "20261003-000000",
                                     "20261004-000000", "20261005-000000")):
                _session(tmp, "hey_alpha", ses, n=5)
            paths, _noise = gw.score_sources("hey_alpha")
            self.assertEqual(len(paths), 20)
            self.assertFalse(any("20261001-000000" in p for p in paths))
            self.assertTrue(any("20261005-000000" in p for p in paths))
            with open(os.path.join(tmp, "hey_alpha", "cleared.json"), "w") as f:
                json.dump({"session": "20261003-000000"}, f)
            paths, _noise = gw.score_sources("hey_alpha")
            self.assertEqual(len(paths), 10)
            self.assertTrue(all("20261004-000000" in p or "20261005-000000" in p for p in paths))
        finally:
            gw.ENROLL_DIR = old
            shutil.rmtree(tmp, ignore_errors=True)


class ClearTraining(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.orig = ga.wake
        ga.wake = lambda m, p, body=None, timeout=3: (self.sent.append((m, p, body)), (503, {"error": "down"}))[1]
        self.inbox = tempfile.mkdtemp(prefix="gs-clear-")
        self._inbox = ga.ENROLL_INBOX
        ga.ENROLL_INBOX = self.inbox
        self.enrolled = ga.ENROLLED
        os.makedirs(os.path.dirname(self.enrolled), exist_ok=True)
        self.c = test_admin.signed_in()

    def tearDown(self):
        ga.wake = self.orig
        ga.ENROLL_INBOX = self._inbox
        shutil.rmtree(self.inbox, ignore_errors=True)

    def _word(self, name, persona, metrics):
        ov = ga.read_json(os.path.join(os.environ["ADMIN_STATE_DIR"], "wake-overrides.json"), {})
        ov[name] = {"persona": persona, "enabled": True}
        ga.write_json(os.path.join(os.environ["ADMIN_STATE_DIR"], "wake-overrides.json"), ov, 0o640)
        raw = ga.read_json(self.enrolled, {"words": []})
        raw["words"] = [w for w in raw.get("words", []) if w.get("name") != name]
        raw["words"].append({"name": name, "persona": persona, "kind": "fewshot", "metrics": metrics})
        ga.write_json(self.enrolled, raw, 0o644)

    def test_clear_deletes_one_bots_recordings_and_stats(self):
        self._word("hey_alpha", "alpha", {"heard": 3, "count": 40, "success_pct": 8, "false_rejects": 37, "false_accepts": 2})
        self._word("hey_beta", "beta", {"heard": 5, "count": 5, "success_pct": 100, "false_rejects": 0, "false_accepts": 0})
        alpha = _session(self.inbox, "hey_alpha", "20261001-120000")
        beta = _session(self.inbox, "hey_beta", "20261001-120000")
        outside = tempfile.mkdtemp(prefix="gs-keep-")
        open(os.path.join(outside, "keep.wav"), "wb").write(b"keep")
        os.symlink(outside, os.path.join(self.inbox, "hey_alpha", "20261002-120000"))
        try:
            code, body = self.c.req("POST", "/api/bots/alpha/training/clear", {})
            self.assertEqual(code, 200, body)
            self.assertIn("hey_alpha", body["phrases"])
            self.assertNotIn("hey_beta", body["phrases"])
            self.assertFalse(os.path.isdir(alpha))
            self.assertTrue(os.path.isdir(beta))
            self.assertTrue(os.path.isfile(os.path.join(outside, "keep.wav")))
            self.assertFalse(os.path.islink(os.path.join(self.inbox, "hey_alpha", "20261002-120000")))
            marker = json.load(open(os.path.join(self.inbox, "hey_alpha", "cleared.json")))
            self.assertRegex(marker["session"], r"\d{8}-\d{6}")
            words = {w["name"]: w for w in ga.read_json(self.enrolled, {}).get("words", [])}
            self.assertNotIn("metrics", words["hey_alpha"])
            self.assertEqual(words["hey_beta"]["metrics"]["success_pct"], 100)
            self.assertIn(("POST", "/enroll/clear", {"name": "hey_alpha"}), self.sent)
            self.assertFalse(any(call[2].get("name") == "hey_beta" for call in self.sent))
            self.assertTrue(os.path.isfile(os.path.join(beta, "00.wav")))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_clear_while_recording_deletes_nothing(self):
        self._word("hey_alpha", "alpha", {"success_pct": 10, "false_rejects": 1, "false_accepts": 0})
        d = _session(self.inbox, "hey_alpha", "20261001-120000")
        ga.write_enroll_status({"hey_alpha": {"state": "recording", "message": "Recording.", "persona": "alpha"}})
        code, body = self.c.req("POST", "/api/bots/alpha/training/clear", {})
        self.assertEqual(code, 409, body)
        self.assertTrue(os.path.isfile(os.path.join(d, "00.wav")))
        self.assertEqual(self.sent, [])


class ScoreLineCopy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = open(JS, encoding="utf-8").read()
        cls.fn = _fn(cls.js, "trainScoreLine")

    def _line(self, src):
        script = self.fn + "\nprocess.stdout.write(trainScoreLine(%s));" % json.dumps(src)
        out = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
        return out.stdout

    def test_zero_tries(self):
        self.assertEqual(self._line({"count": 0, "success_pct": 0, "false_rejects": 0, "false_accepts": 0}), "No tries yet")
        self.assertEqual(self._line({}), "No tries yet")

    def test_singular_and_plural(self):
        one = {"count": 1, "success_pct": 100, "false_rejects": 0, "false_accepts": 1}
        self.assertEqual(self._line(one), "Last 1 try: 100% recognized · 0 missed · 1 false wake")
        two = {"count": 2, "success_pct": 50, "false_rejects": 1, "false_accepts": 2}
        self.assertEqual(self._line(two), "Last 2 tries: 50% recognized · 1 missed · 2 false wakes")
        many = {"count": 20, "success_pct": 80, "false_rejects": 4, "false_accepts": 0}
        self.assertEqual(self._line(many), "Last 20 tries: 80% recognized · 4 missed · 0 false wakes")


class ErrorTextRow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = open(JS, encoding="utf-8").read()
        cls.css = open(CSS, encoding="utf-8").read()

    def test_error_clamps_to_three_lines_until_show_details(self):
        fn = _fn(self.js, "trainErrorHTML")
        self.assertIn('class="error-text"', fn)
        self.assertIn('class="error-text-body"', fn)
        self.assertIn('data-expanded="false"', fn)
        self.assertIn(">Show details<", fn)
        bind = _fn(self.js, "bindTrainError")
        self.assertIn('dataset.expanded', bind)
        self.assertIn('"Show details"', bind)
        self.assertIn('"Hide details"', bind)
        setter = _fn(self.js, "openSetWake")
        self.assertIn("bindTrainError(box)", setter)
        body = self.css.split(".wwmsg .error-text-body{", 1)[1].split("}", 1)[0].replace(" ", "")
        self.assertIn("-webkit-line-clamp:3", body)
        self.assertIn("line-clamp:3", body)
        self.assertIn("overflow:hidden", body)
        self.assertNotIn("nowrap", body)
        opened = self.css.split('.wwmsg .error-text[data-expanded="true"] .error-text-body{', 1)[1].split("}", 1)[0]
        self.assertIn("-webkit-line-clamp:unset", opened.replace(" ", ""))
        flat = self.css.replace(" ", "").replace("\n", "")
        self.assertIn(".wacts.btn.sm{min-height:44px;height:44px;width:auto;flex:none}", flat)
        wake = _fn(self.js, "openBot")
        self.assertIn('id="wrem" type="button">Remove phrase</button>${clearAct}', wake)
        self.assertIn('class="btn sm q" id="wclear"', wake)
        self.assertNotIn('<div class="field"><button class="btn sm" id="wclear"', wake)


if __name__ == "__main__":
    unittest.main()
