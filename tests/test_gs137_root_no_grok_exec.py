#!/usr/bin/env python3
"""Nix M1 (0.13.7): no root install step runs a program from a grok-owned path.

grok owns /opt/grok-station/wake (and portraits), so a venv there could be swapped for anything and then run by
root. The wake venv lives in root-owned /opt/grok-station/wake-venv; the old one is removed as grok, never run.
"""
import os
import re
import shlex
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GROK_TREES = ("/opt/grok-station/wake/", "/opt/grok-station/portraits/", "$O/wake/", "$O/portraits/",
              "/var/lib/grok-station/enroll/", "$V/enroll/")
ROOT_SCRIPTS = ("install.sh", "tools/install-files.sh", "tools/grok-station-update", "tools/grok-station",
                "bootstrap.sh", "tools/build-image/firstboot.sh")


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _first_word(cmd):
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words = words[1:]
    if words and words[0] == "env":
        words = [w for w in words[1:] if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
    return words[0] if words else ""


class Static(unittest.TestCase):
    def test_no_venv_or_program_under_a_grok_tree_in_root_scripts(self):
        bad = []
        for rel in ROOT_SCRIPTS:
            if not os.path.exists(os.path.join(ROOT, rel)):
                continue
            for n, line in enumerate(_read(rel).splitlines(), 1):
                code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
                if not code.strip() or "runuser -u grok --" in code:
                    continue
                # [ -e PATH ] / [ -L PATH ] only stat the path; anything else naming the old venv is refused.
                if re.search(r"(\$O|/opt/grok-station)/wake/venv\b", re.sub(r"\[ [^\]]*\]", "", code)):
                    bad.append("%s:%d venv in grok tree: %s" % (rel, n, line.strip()))
                for tree in GROK_TREES:
                    # a grok-tree path in command position: line start, after run/chk/sh_ ", or after ; && || | $(
                    if re.search(r'(^|[;&|(]|\brun |\bchk |\bsh_ ")\s*"?' + re.escape(tree), code.strip()):
                        bad.append("%s:%d runs %s: %s" % (rel, n, tree, line.strip()))
        self.assertEqual(bad, [])

    def test_wake_unit_and_docs_use_the_root_owned_venv(self):
        unit = _read("systemd/grok-station-wake.service")
        self.assertIn("ExecStart=/opt/grok-station/wake-venv/bin/python /opt/grok-station/wake/grok_wake.py", unit)
        self.assertIn("User=grok", unit)
        self.assertNotIn("wake/venv", _read("docs/enroll.md"))
        src = _read("install.sh")
        self.assertIn("WV=$O/wake-venv", src)
        self.assertIn('_root_tree_ok "$WV"', src)
        self.assertIn('runuser -u grok -- rm -rf --one-file-system -- "$O/wake/venv"', src)


class DryRun(unittest.TestCase):
    def test_no_root_command_starts_under_a_grok_tree(self):
        r = subprocess.run(["sh", os.path.join(ROOT, "install.sh"), "--dry-run", "--verbose"], capture_output=True,
                           text=True, timeout=60, env=dict(os.environ, NO_COLOR="1"), cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        cmds = [l[2:] for l in r.stdout.splitlines() if l.startswith("+ ")]
        self.assertTrue(any("/opt/grok-station/wake-venv/bin/pip install" in c for c in cmds))
        bad = []
        for c in cmds:
            if c.startswith("runuser -u grok -- "):
                continue
            inner = c
            m = re.match(r"^sh -c '(.*)'$", c)
            if m:
                inner = m.group(1)
            for part in re.split(r"\|\||&&|;|\|", inner):
                part = part.strip().lstrip("{( ").strip()
                part = re.sub(r"^\[[^\]]*\]\s*", "", part)
                w = _first_word(part)
                if any(w.startswith(t) for t in GROK_TREES if t.startswith("/")):
                    bad.append(c)
            if "/opt/grok-station/wake/venv" in c:
                bad.append(c)
        self.assertEqual(bad, [])


class StopWordCleanup(unittest.TestCase):
    """_vosk_remove runs for real against a fake $O: it must not execute the old venv's pip as root."""

    def test_old_venv_programs_are_never_run(self):
        src = _read("install.sh")
        body = src[src.index("_vosk_remove() {"):]
        body = body[:body.index("\n}\n") + 3]
        with tempfile.TemporaryDirectory() as t:
            o = os.path.join(t, "opt")
            vb = os.path.join(o, "wake", "venv", "bin")
            os.makedirs(vb)
            marker = os.path.join(t, "ran-as-root")
            for prog in ("pip", "python"):
                p = os.path.join(vb, prog)
                open(p, "w").write("#!/bin/sh\necho \"$0 $*\" >> '%s'\n" % marker)
                os.chmod(p, 0o755)
            stub = os.path.join(t, "bin")
            os.makedirs(stub)
            open(os.path.join(stub, "runuser"), "w").write('#!/bin/sh\nshift 3\nexec "$@"\n')
            os.chmod(os.path.join(stub, "runuser"), 0o755)
            script = 'DRY=0\nO="%s"\n%s\n_vosk_remove\n' % (o, body)
            r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30,
                               env=dict(os.environ, PATH=stub + ":" + os.environ["PATH"]))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(marker), open(marker).read() if os.path.exists(marker) else "")

    def test_old_venv_removal_does_not_follow_a_symlink(self):
        src = _read("install.sh")
        if "_old_wake_venv_remove() {" not in src:
            self.fail("no _old_wake_venv_remove")
        body = src[src.index("_old_wake_venv_remove() {"):]
        body = body[:body.index("\n}\n") + 3]
        with tempfile.TemporaryDirectory() as t:
            o = os.path.join(t, "opt")
            os.makedirs(os.path.join(o, "wake"))
            victim = os.path.join(t, "victim")
            os.makedirs(victim)
            open(os.path.join(victim, "keep"), "w").write("x")
            os.symlink(victim, os.path.join(o, "wake", "venv"))
            stub = os.path.join(t, "bin")
            os.makedirs(stub)
            log = os.path.join(t, "runuser.log")
            open(os.path.join(stub, "runuser"), "w").write('#!/bin/sh\necho "$*" >> "%s"\nshift 3\nexec "$@"\n' % log)
            os.chmod(os.path.join(stub, "runuser"), 0o755)
            r = subprocess.run(["sh", "-c", 'DRY=0\nO="%s"\n%s\n_old_wake_venv_remove\n' % (o, body)],
                               capture_output=True, text=True, timeout=30,
                               env=dict(os.environ, PATH=stub + ":" + os.environ["PATH"]))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(os.path.join(victim, "keep")))
            self.assertFalse(os.path.lexists(os.path.join(o, "wake", "venv")))
            self.assertIn("-u grok -- rm -rf --one-file-system --", open(log).read())


class RootTreeCheck(unittest.TestCase):
    def _fn(self):
        src = _read("install.sh")
        body = src[src.index("_root_tree_ok() {"):]
        return body[:body.index("\nPY\n}\n") + 6]

    def test_refuses_a_non_root_parent(self):
        with tempfile.TemporaryDirectory() as t:  # owned by the test user, not root
            r = subprocess.run(["sh", "-c", '%s\n_root_tree_ok "%s/wake-venv"' % (self._fn(), t)],
                               capture_output=True, text=True, timeout=30)
            if os.getuid() == 0:
                self.skipTest("running as root")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("not root-owned", r.stderr + r.stdout)

    def test_accepts_root_tree(self):
        r = subprocess.run(["sh", "-c", '%s\n_root_tree_ok /usr/lib' % self._fn()], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


GROK_CODE_TREES = re.compile(r"/opt/grok-station/(?:wake|portraits)(?:/|\b)")
ROOT_PATHS = ("admin/grok-station-helper", "scripts/gs-update.sh", "admin/grok_admin.py")


class RootHelperImports(unittest.TestCase):
    """0.13.7 (Nix M1b): the sudo helper, gs-update.sh and the admin page never import or run code under $O/wake
    or $O/portraits (grok owns both). The helper loads gain.py from root-owned /opt/grok-station/gain.py."""

    def test_no_code_path_under_a_grok_tree(self):
        code = re.compile(r"\.(?:py|sh|so|pyc)\b|exec_module|spec_from_file_location|import_module|runpy|\bexec\b")
        for rel in ROOT_PATHS:
            for n, line in enumerate(_read(rel).splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if GROK_CODE_TREES.search(line) and code.search(line):
                    self.fail("%s:%d runs or imports under a grok tree: %s" % (rel, n, line.strip()))

    def _helper(self):
        import importlib.machinery
        import importlib.util
        path = os.path.join(ROOT, "admin", "grok-station-helper")
        loader = importlib.machinery.SourceFileLoader("gs_helper_m1b", path)
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("gs_helper_m1b", loader))
        loader.exec_module(mod)
        return mod

    def test_helper_loads_only_root_owned_copies(self):
        h = self._helper()
        seen = []
        real = h.os.path.isfile
        h.os.path.isfile = lambda p: seen.append(p) or False
        self.addCleanup(setattr, h.os.path, "isfile", real)
        h.__dict__.pop("__file__", None)  # as installed: no repo checkout beside it
        for fn in (h.station_gain, h.station_sounds):
            fn.__dict__.pop("_mod", None)
            self.assertIsNone(fn())
        self.assertIn("/opt/grok-station/gain.py", seen)
        self.assertEqual([p for p in seen if GROK_CODE_TREES.search(p)], [])

    def test_update_installs_the_root_copy(self):
        text = _read("tools/install-files.sh")
        self.assertRegex(text, r'install -o root -g root -m644 "\$S/wake/gain\.py" /opt/grok-station/gain\.py\.new && '
                               r'mv /opt/grok-station/gain\.py\.new /opt/grok-station/gain\.py')
        i = text.index("/opt/grok-station/gain.py.new")
        self.assertIn("st.st_uid != 0 or st.st_mode & 0o022", text[:i])  # parents checked root-owned first
        self.assertIn("install-files.sh", _read("install.sh"))  # install and Update both run install-files.sh


if __name__ == "__main__":
    unittest.main()
