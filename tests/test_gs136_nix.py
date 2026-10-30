#!/usr/bin/env python3
"""Nix review of 0.13.6: M1 (no symlink-following `install -d -o grok`), L3 (no root `install -o grok` into
$O/wake), L4 (safe-mkdirs trusts only a root-owned, not group/other-writable base).

The tests run unprivileged: the real shell steps are extracted from install.sh and tools/install-files.sh with
the owner set to the current user and the trusted base owner (root) swapped for the current user through a shim
that imports tools/safe-mkdirs.py. The root path itself is exercised with sudo in the evidence runs.
"""
import getpass
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "safe-mkdirs.py")
ME = getpass.getuser()


def _load():
    spec = importlib.util.spec_from_file_location("safe_mkdirs", TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SHIM = r'''
import importlib.util, os, runpy, sys
tool = %r
spec = importlib.util.spec_from_file_location("safe_mkdirs", tool)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
if hasattr(m, "main"):
    m.ROOT_UID = os.getuid()  # tests run unprivileged: trust a base owned by this user instead of root
    sys.exit(m.main(sys.argv[1:]))
sys.argv = [tool] + sys.argv[1:]
runpy.run_path(tool, run_name="__main__")
''' % TOOL


def _src(name):
    return open(os.path.join(ROOT, name)).read()


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        os.chmod(self.t, 0o755)
        self.shim = os.path.join(self.t, "shim.py")
        open(self.shim, "w").write(SHIM)
        self.victim = os.path.join(self.t, "victim")
        os.mkdir(self.victim)
        os.chmod(self.victim, 0o700)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def _untouched(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.victim).st_mode), 0o700)
        self.assertEqual(os.listdir(self.victim), [])

    def _sh(self, script, **env):
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, cwd=ROOT,
                              env=dict(os.environ, **env))

    def _fix(self, text):
        """Run as this user: drop root-only owner flags, point the tool at the shim, run runuser as a no-op."""
        text = text.replace("-o grok -g grok", "").replace("-o root -g grok", "")
        text = text.replace("python3 -I - --put grok ", "python3 -I - --put %s " % ME)
        text = re.sub(r"\bpython3 -I - grok\b", "python3 -I - %s" % ME, text)
        text = re.sub(r"\bpython3 -I - (--\S+ \S+ )grok\b", r"python3 -I - \g<1>%s" % ME, text)
        text = text.replace('< "$S/tools/safe-mkdirs.py"', '< "%s"' % self.shim)
        text = text.replace("< tools/safe-mkdirs.py", '< "%s"' % self.shim)
        text = text.replace("runuser -u grok -- ", "")
        return text


class M1(Base):
    def test_no_install_d_with_grok_owner_left(self):
        for f in ("install.sh", "tools/install-files.sh"):
            for line in _src(f).split("\n"):
                if not line.lstrip().startswith("#"):
                    self.assertNotRegex(line, r"install -d[^\n]*-o grok[\s:]", (f, line))

    def _install_files_wake_block(self, S, O):
        src = _src("tools/install-files.sh")
        start = src.index("if [ -d /opt/grok-station/wake ]")
        end = src.index('install -m755 "$S/admin/grok_admin.py"')
        block = self._fix(src[start:end]).replace("/opt/grok-station", O)
        return 'set -eu\nS="%s"\n%s' % (S, block)

    def _repo_copy(self):
        S = os.path.join(self.t, "repo")
        os.makedirs(os.path.join(S, "wake", "models"))
        os.makedirs(os.path.join(S, "wake", "lines"))
        os.makedirs(os.path.join(S, "tools"))
        shutil.copy(TOOL, os.path.join(S, "tools", "safe-mkdirs.py"))
        for n in ("grok_wake.py", "gain.py", "fewshot.py", "wait_tone.py"):
            open(os.path.join(S, "wake", n), "w").write("# %s\n" % n)
        open(os.path.join(S, "wake", "models", "fewshot_negatives.npz"), "wb").write(b"npz")
        open(os.path.join(S, "wake", "lines", "a.wav"), "wb").write(b"wav")
        return S

    def test_install_files_plain_update_works(self):
        S, O = self._repo_copy(), os.path.join(self.t, "opt")
        os.makedirs(os.path.join(O, "wake"))
        r = self._sh(self._install_files_wake_block(S, O))
        self.assertEqual(r.returncode, 0, r.stderr)
        for rel in ("wake/grok_wake.py", "wake/wait_tone.py", "wake/models/fewshot_negatives.npz", "wake/lines/a.wav"):
            p = os.path.join(O, rel)
            self.assertTrue(os.path.isfile(p) and not os.path.islink(p), rel)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o644, rel)
        self.assertTrue(os.path.isdir(os.path.join(O, "wake", "models", "enrolled")))

    def test_install_files_symlinked_models_is_refused(self):
        S, O = self._repo_copy(), os.path.join(self.t, "opt")
        os.makedirs(os.path.join(O, "wake"))
        os.symlink(self.victim, os.path.join(O, "wake", "models"))
        r = self._sh(self._install_files_wake_block(S, O))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("symlink", r.stdout + r.stderr)
        self._untouched()

    def test_install_files_symlinked_lines_is_refused(self):
        S, O = self._repo_copy(), os.path.join(self.t, "opt")
        os.makedirs(os.path.join(O, "wake"))
        os.symlink(self.victim, os.path.join(O, "wake", "lines"))
        r = self._sh(self._install_files_wake_block(S, O))
        self.assertNotEqual(r.returncode, 0)
        self._untouched()

    def _enroll_line(self, V):
        line = [l for l in _src("install.sh").split("\n") if "enroll/inbox" in l and not l.lstrip().startswith("#")]
        self.assertTrue(line)
        body = "\n".join(line)
        # a multi-line if/else around the call is kept whole
        src = _src("install.sh")
        i = src.index(line[0])
        j = src.rfind("\n", 0, i)
        if line[0].lstrip().startswith("if "):
            body = src[i:src.index("\n  fi\n", i) + 5]
        else:
            # include continuation lines
            k = i
            while src[k:src.index("\n", k)].rstrip().endswith("\\"):
                k = src.index("\n", k) + 1
            body = src[i:src.index("\n", k)]
        return 'set -eu\nDRY=0\nV="%s"\nrun() { "$@"; }\nf() {\n%s\n}\nf\n' % (V, self._fix(body))

    def test_enroll_plain_is_0750(self):
        V = os.path.join(self.t, "var")
        os.mkdir(V)
        r = self._sh(self._enroll_line(V))
        self.assertEqual(r.returncode, 0, r.stderr)
        for rel in ("enroll", "enroll/inbox"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(V, rel)).st_mode), 0o750, rel)

    def test_symlinked_inbox_is_refused(self):
        V = os.path.join(self.t, "var")
        os.makedirs(os.path.join(V, "enroll"))
        os.symlink(self.victim, os.path.join(V, "enroll", "inbox"))
        r = self._sh(self._enroll_line(V))
        self.assertNotEqual(r.returncode, 0)
        self._untouched()


class L3(Base):
    def test_no_root_install_owner_grok_into_wake(self):
        for f in ("install.sh", "tools/install-files.sh"):
            for line in _src(f).split("\n"):
                if not line.lstrip().startswith("#") and re.search(r"\binstall\b[^\n]*-o grok[\s:]", line):
                    self.assertNotRegex(line, r"(\$O|/opt/grok-station)/wake", (f, line))

    def test_install_sh_wake_files_with_symlinked_wake(self):
        """st_wake's file copies, with $O/wake swapped for a symlink after the dirs were checked."""
        src = _src("install.sh")
        body = src[src.index("st_wake() {\n"):]
        body = body[:body.index("\n}\n")]
        lines = [l for l in body.split("\n") if ("$O/wake" in l or '"$O" wake' in l) and ("wake/grok_wake.py" in l or "hey_grok.onnx" in l
                                                                    or "fewshot_negatives.npz" in l)]
        self.assertEqual(len(lines), 3, lines)
        O = os.path.join(self.t, "opt")
        os.mkdir(O)
        os.symlink(self.victim, os.path.join(O, "wake"))
        S = os.path.join(self.t, "repo")
        shutil.copytree(os.path.join(ROOT, "wake"), os.path.join(S, "wake"),
                        ignore=shutil.ignore_patterns("venv", "__pycache__"))
        os.mkdir(os.path.join(S, "tools"))
        shutil.copy(TOOL, os.path.join(S, "tools", "safe-mkdirs.py"))
        script = 'set -eu\nDRY=0\nO="%s"\nrun() { "$@"; }\n%s\n' % (O, self._fix("\n".join(lines)))
        r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, cwd=S)
        self.assertNotEqual(r.returncode, 0)
        self._untouched()


    def test_put_replaces_a_planted_file_symlink_not_its_target(self):
        m = _load()
        base = os.path.join(self.t, "opt")
        os.makedirs(os.path.join(base, "wake"))
        target = os.path.join(self.victim, "secret")
        open(target, "w").write("keep")
        os.symlink(target, os.path.join(base, "wake", "grok_wake.py"))
        src = os.path.join(self.t, "grok_wake.py")
        open(src, "w").write("new")
        kw = {"root_uid": os.getuid()} if "root_uid" in m.put.__code__.co_varnames else {}
        m.put(base, "wake", [src], os.getuid(), os.getgid(), **kw)
        p = os.path.join(base, "wake", "grok_wake.py")
        self.assertFalse(os.path.islink(p))
        self.assertEqual(open(p).read(), "new")
        self.assertEqual(open(target).read(), "keep")
        self.assertEqual(sorted(os.listdir(os.path.join(base, "wake"))), ["grok_wake.py"])


class L4(Base):
    def test_base_not_owned_by_root_is_refused(self):
        m = _load()
        base = os.path.join(self.t, "base")
        os.mkdir(base, 0o755)
        self.assertTrue(hasattr(m, "ROOT_UID"))
        if os.getuid() == 0:
            os.chown(base, 65534, 65534)
            uid = 0
        else:
            uid = os.getuid() + 1  # trust someone else: this user's base must be refused
        with self.assertRaises(m.Refused) as c:
            m.mkdirs(base, ["x"], os.getuid(), os.getgid(), root_uid=uid)
        self.assertIn("not owned by root", str(c.exception))
        self.assertFalse(os.path.exists(os.path.join(base, "x")))

    def test_group_or_other_writable_base_is_refused(self):
        m = _load()
        for mode in (0o775, 0o757):
            base = os.path.join(self.t, "base%o" % mode)
            os.mkdir(base)
            os.chmod(base, mode)
            with self.assertRaises(m.Refused):
                m.mkdirs(base, ["x"], os.getuid(), os.getgid(), root_uid=os.getuid())
            self.assertFalse(os.path.exists(os.path.join(base, "x")))

    def test_good_base_with_mode(self):
        m = _load()
        base = os.path.join(self.t, "ok")
        os.mkdir(base, 0o755)
        m.mkdirs(base, ["a/b"], os.getuid(), os.getgid(), mode=0o750, root_uid=os.getuid())
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(base, "a", "b")).st_mode), 0o750)

    @unittest.skipUnless(os.getuid() == 0, "needs root: the real ROOT_UID path is run with sudo in /tmp/gs136-l4-*")
    def test_cli_as_root_refuses_user_owned_base(self):
        base = os.path.join(self.t, "nb")
        os.mkdir(base, 0o755)
        os.chown(base, 65534, 65534)
        r = subprocess.run([sys.executable, "-I", "-", "nobody", base, "x"], stdin=open(TOOL), capture_output=True,
                           text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not owned by root", r.stderr)


if __name__ == "__main__":
    unittest.main()
