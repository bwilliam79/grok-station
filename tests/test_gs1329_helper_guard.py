"""0.13.29: helper files cannot change without a new HELPER_VERSION, and the update reinstalls it.

0.13.27 changed scripts/gs-seal-reply but left HELPER_VERSION at 0.13.17, so gs-update.sh never
offered the new helper (it reinstalls only when the manifest's helper line is newer than the
installed one). tools/helper-releases.txt records one line per helper release: the version and
one sha256 over the files install-home.sh fetches. tools/check-version.py fails when those files
change and the last line still names the same version, when HELPER_VERSION has no line, or when
the versions do not go up.
"""
import hashlib
import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))


def _load(name, rel):
    loader = importlib.machinery.SourceFileLoader(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


cv = _load("gs1329_check_version", "tools/check-version.py")


def _tree_reader(overrides=None):
    overrides = overrides or {}

    def read(path):
        if path in overrides:
            return overrides[path]
        with open(os.path.join(ROOT, path), "rb") as fh:
            return fh.read()
    return read


def _helper_version():
    text = open(os.path.join(ROOT, "scripts", "gs_seal_common.py"), encoding="utf-8").read()
    return re.search(r'^HELPER_VERSION = "([0-9.]+)"$', text, re.M).group(1)


class RecordedHelper(unittest.TestCase):
    def test_helper_files_are_the_ones_the_installer_fetches(self):
        files = cv.helper_files(_tree_reader()("scripts/install-home.sh").decode())
        self.assertEqual(files, ["scripts/gs-seal-setup", "scripts/gs-seal-reply",
                                 "scripts/gs_seal_common.py", "scripts/vendor-lock.txt",
                                 "admin/gs_render.py", "relay/owner_check.py", "app/cards.py"])

    def test_the_tree_matches_its_record(self):
        self.assertIsNone(cv.helper_problem(_tree_reader()))
        last = open(os.path.join(ROOT, "tools", "helper-releases.txt")).read().split("\n")
        last = [ln for ln in last if ln and not ln.startswith("#")][-1].split()
        self.assertEqual(last[0], _helper_version())

    def test_helper_version_is_0_13_30_everywhere(self):
        self.assertEqual(_helper_version(), "0.13.45")  # 0.13.45: offer yes ignores punctuation; cards.py ships with the helper
        with open(os.path.join(ROOT, "admin", "grok_admin.py"), encoding="utf-8") as fh:
            admin = fh.read()
        self.assertRegex(admin, r'(?m)^HELPER_RELEASE = "0\.13\.45"  #')

    def test_a_changed_helper_file_without_a_bump_fails(self):
        for path in ("scripts/gs-seal-reply", "scripts/gs_seal_common.py", "scripts/gs-seal-setup",
                     "scripts/vendor-lock.txt", "admin/gs_render.py", "app/cards.py"):
            data = _tree_reader()(path) + b"\n# changed\n"
            msg = cv.helper_problem(_tree_reader({path: data}))
            self.assertIsNotNone(msg, path)
            self.assertIn("without a HELPER_VERSION bump", msg)

    def test_a_later_release_header_does_not_bump_the_helper(self):
        # A 0.13.38 header lives in the templates, which are not helper files.
        base = _tree_reader()

        def read(path):
            data = base(path)
            if path in ("admin/gs_routine.py", "admin/routine-home.txt") and data:
                return data.replace(b"Grok Station instructions, release ", b"Grok Station instructions, release 9.9.9 ", 1)
            return data

        self.assertIsNone(cv.helper_problem(read))

    def test_the_installed_renderer_is_the_file_in_the_digest(self):
        text = open(os.path.join(ROOT, "scripts", "install-home.sh"), encoding="utf-8").read()
        commit = re.search(r"(?m)^COMMIT=([0-9a-f]{40})$", text).group(1)
        want = re.search(r"(?m)^HASH_GS_RENDER=([0-9a-f]{64})$", text).group(1)
        with open(os.path.join(ROOT, "admin", "gs_render.py"), "rb") as fh:
            head = fh.read()
        self.assertEqual(hashlib.sha256(head).hexdigest(), want)
        blob = subprocess.run(["git", "-C", ROOT, "show", "%s:admin/gs_render.py" % commit], capture_output=True)
        if blob.returncode != 0:
            self.skipTest("installer commit does not contain gs_render.py yet")
        self.assertEqual(blob.stdout, head)
        self.assertIsNone(cv.helper_problem(_tree_reader()))

    def test_a_bump_without_a_record_line_fails(self):
        common = _tree_reader()("scripts/gs_seal_common.py").replace(
            b'HELPER_VERSION = "%s"' % _helper_version().encode(), b'HELPER_VERSION = "9.9.9"')
        msg = cv.helper_problem(_tree_reader({"scripts/gs_seal_common.py": common}))
        self.assertIn("9.9.9", msg)
        self.assertIn("tools/helper-releases.txt", msg)

    def test_rerecording_the_same_version_fails(self):
        rec = _tree_reader()("tools/helper-releases.txt")
        again = rec + ("%s %s\n" % (_helper_version(), "0" * 64)).encode()
        self.assertIn("goes up", cv.helper_problem(_tree_reader({"tools/helper-releases.txt": again})))

    def test_missing_record_passes_for_older_trees(self):
        def read(path):
            if path == "tools/helper-releases.txt":
                return None
            return _tree_reader()(path)
        self.assertIsNone(cv.helper_problem(read))


SNIPPET = re.compile(r'if py - "\$want_helper" "\$\{cur:-0\.0\.0\}" <<\'PY\'\n(.*?)\nPY\n', re.S)


def _old_script(commit):
    r = subprocess.run(["git", "-C", ROOT, "show", "%s:scripts/gs-update.sh" % commit], capture_output=True)
    return r.stdout if r.returncode == 0 else None


class UpdaterReinstallsHelper(unittest.TestCase):
    """gs-update.sh reinstalls the shared helper when the manifest's helper is newer than the installed one."""

    def _decides(self, script_text, want, have):
        body = SNIPPET.search(script_text).group(1)
        return subprocess.run([sys.executable, "-I", "-", want, have], input=body.encode()).returncode == 0

    def test_version_compare_in_current_and_older_updaters(self):
        new = _helper_version()
        scripts = {"current": open(os.path.join(ROOT, "scripts", "gs-update.sh")).read()}
        for rel, commit in (("0.13.24", "489bf8e8c0dadeeb9185e58854fa2024922518a6"),
                            ("0.13.27", "ab4a549a390f463d79ddc866b69bdc11c207543a"),
                            ("0.13.28", "55d41dce601ba9281de836720e7ef8ca118a5292")):
            raw = _old_script(commit)
            if raw is not None:
                scripts[rel] = raw.decode()
        for name, text in scripts.items():
            self.assertTrue(self._decides(text, new, "0.13.17"), name)   # what every 0.13.17 to 0.13.28 home has
            self.assertFalse(self._decides(text, new, new), name)        # another bot already updated it
            self.assertFalse(self._decides(text, "0.13.17", "0.13.17"), name)  # the 0.13.28 manifest: never

    def test_home_update_runs_the_installer_from_0_13_17_to_0_13_29(self):
        import test_finish_update as tfu
        new = _helper_version()
        installer = ('#!/bin/sh\nprintf \'%%s\\n\' "$*" > "$HOME/install-args"\n'
                     'sed -i \'s/^HELPER_VERSION = .*/HELPER_VERSION = "%s"/\' '
                     '"$HOME/.local/share/grok-station/bin/gs_seal_common.py"\nprintf \'%%s\\n\' %s\n') % (new, new)

        class Fake(tfu.Fake):
            helper = new

            def manifest(self, **kw):
                return super().manifest(**kw).replace(b"helper 9.9.9", b"helper " + self.helper.encode())

        for installed, runs in (("0.13.17", True), (new, False)):
            f = Fake("home")
            f.files["scripts/install-home.sh"] = installer.encode()
            open(os.path.join(f.state, "bin/gs_seal_common.py"), "w").write('HELPER_VERSION = "%s"\n' % installed)
            f.publish()
            r = f.run()
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(" helper=%s " % new, tfu.status(r.stdout))
            self.assertEqual(os.path.exists(os.path.join(f.home, "install-args")), runs, installed)


if __name__ == "__main__":
    unittest.main()
