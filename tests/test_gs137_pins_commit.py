"""0.13.7: deploy-pins commit is the manifest's update commit, so station and bot render the same instructions.

0.13.4-0.13.6 pinned the manifest commit as `commit`. The station renders a bot's instructions with pins.commit
(grok_admin.render_bot_routine) and the bot with the manifest's update line (gs-update.sh upd_commit), so every
paired bot read "The bot's instructions don't match this release" after Update. Not gated on GS_RELEASE_CHECK:
everything here is local git, no network.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "admin"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_admin  # noqa: E402  (sets the admin test env before grok_admin is first imported)
ga = test_admin.ga
import gs_routine  # noqa: E402

PINS = os.path.join(ROOT, "admin", "web", "deploy-pins.js")
GEN = os.path.join(ROOT, "tools", "gen-deploy-pins.py")


def git_show(ref, path):
    return subprocess.run(["git", "-C", ROOT, "show", "%s:%s" % (ref, path)], check=True, capture_output=True).stdout


def pins():
    text = open(PINS, encoding="utf-8").read()
    return (re.search(r'^  commit: "([0-9a-f]{40})",$', text, re.M).group(1),
            re.search(r'^  update_commit: "([0-9a-f]{40})",$', text, re.M).group(1))


def manifest_update(upd_commit):
    man = git_show(upd_commit, "scripts/update-manifest.txt").decode("ascii")
    m = re.search(r"^update ([0-9a-f]{40}) ([0-9a-f]{64})$", man, re.M)
    return m.group(1), m.group(2)


class PinnedCommitIsManifestUpdate(unittest.TestCase):
    def test_pins_commit_equals_manifest_update_commit(self):
        commit, upd = pins()
        self.assertEqual(manifest_update(upd)[0], commit)

    def test_pinned_gs_update_hash_is_the_manifests(self):
        commit, upd = pins()
        self.assertEqual(ga.deploy_pins()["files"]["scripts/gs-update.sh"], manifest_update(upd)[1])


class StationAndBotRenderTheSameText(unittest.TestCase):
    """The station's expected text (render_bot_routine + deploy_pins) and the bot's (gs-update.sh step F:
    gs_routine render of the manifest commit's template with upd_commit and upd_sha) hash the same."""

    STATION, FP, PUB = "https://station.example:8443", ":".join(["AB"] * 32), "A" * 43 + "="

    def setUp(self):
        old, ga.VERSION_FILE = ga.VERSION_FILE, os.path.join(ROOT, "VERSION")  # the shipped file, not a test stub
        self.addCleanup(setattr, ga, "VERSION_FILE", old)

    def _both(self, mode):
        bot = {"slug": "kai", "key_mode": mode}
        station = ga.render_bot_routine(bot, self.STATION, self.FP, self.PUB, pins=ga.deploy_pins())
        commit, upd = pins()
        upd_commit, upd_sha = manifest_update(upd)
        template = git_show(upd, "admin/routine-%s.txt" % mode).decode("utf-8")
        extra = (self.STATION, self.FP, self.PUB) if mode == "cloud" else ("", "", "")
        botside = gs_routine.render(template, mode, "kai", upd_commit, upd_sha, *extra)
        return gs_routine.instr_hash(station), gs_routine.instr_hash(botside)

    def test_home(self):
        s, b = self._both("home")
        self.assertEqual(s, b)

    def test_cloud(self):
        s, b = self._both("cloud")
        self.assertEqual(s, b)


class GeneratorCannotPinTheWrongCommit(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, GEN] + list(args), capture_output=True, text=True, cwd=ROOT)

    def test_commit_comes_from_the_manifest(self):
        commit, upd = pins()
        r = self._run("--update-commit", upd, "--out", "-")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, open(PINS, encoding="utf-8").read())

    def test_positional_commit_is_an_error(self):
        commit, upd = pins()
        for args in ((upd, "--update-commit", upd), (commit, "--update-commit", upd)):
            r = self._run(*(list(args) + ["--out", "-"]))
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("no commit argument", r.stderr)

    def test_update_commit_required_and_must_hold_a_manifest(self):
        self.assertNotEqual(self._run("--out", "-").returncode, 0)
        r = self._run("--update-commit", "0" * 40, "--out", "-")
        self.assertNotEqual(r.returncode, 0)
        r = self._run("--update-commit", "HEAD", "--out", "-")
        self.assertIn("40 lowercase hex", r.stderr)

    def test_check_deploy_pins_flags_a_mismatch(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("cdp", os.path.join(ROOT, "tools", "check-deploy-pins.py"))
        cdp = importlib.util.module_from_spec(spec); spec.loader.exec_module(cdp)
        self.assertEqual(cdp.manifest_commit_mismatch(), [])
        commit, upd = pins()
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "deploy-pins.js")
            open(bad, "w").write(open(PINS).read().replace('commit: "%s"' % commit, 'commit: "%s"' % upd, 1))
            self.assertEqual(len(cdp.manifest_commit_mismatch(bad)), 1)


if __name__ == "__main__":
    unittest.main()
