#!/usr/bin/env python3
"""0.13.34: every local module the installed admin page imports is installed (bq: 0.13.33 crash loop).

0.13.33 moved the render code from admin/gs_routine.py into admin/gs_render.py, and gs_routine.py imports it.
tools/install-files.sh never copied gs_render.py, so grok-station-admin failed at `import gs_routine` on every
start after the update. The older check (tests/test_install.py) only read grok_admin.py's own top-level imports.

These tests read every install line in tools/install-files.sh, follow every import (nested ones too) from
admin/grok_admin.py through each local module it reaches, and require each one installed where the admin page
looks for it. Then they copy the payload into a temporary /opt/grok-station the way the installer does and import
grok_admin from there with python3 -I, so a missing file fails here, not on the station.
gs-update.sh is the bot-side copy of the same graph: every local module its embedded gs_routine.py reaches must be
written next to it, byte-identical to the repo file.
"""
import ast
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "tools", "install-files.sh")
UPDATE = os.path.join(ROOT, "scripts", "gs-update.sh")
OPT = "/opt/grok-station"
# Where the installed admin page finds modules: its own folder (sys.path[0] and HERE), ../relay, and the parent
# (dimming.py). Repo folders searched for the matching source, in the same order.
ADMIN_PATH = (OPT + "/admin", OPT + "/relay", OPT)
REPO_DIRS = ("admin", "relay", "app", ".", "wake")


def installed_files():
    """{installed path: (repo path, mode, owner)} for every install line in tools/install-files.sh."""
    out = {}
    with open(INSTALL, encoding="utf-8") as fh:
        text = fh.read().replace("\\\n", " ")
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for seg in line.split("&&"):
            try:
                toks = shlex.split(seg, comments=True)
            except ValueError:
                continue
            if not toks or toks[0] != "install" or "-d" in toks:
                continue
            mode, owner, args, i = None, None, [], 1
            while i < len(toks):
                t = toks[i]
                if t in ("-o", "-g"):
                    owner = (owner or "") + t + toks[i + 1]
                    i += 2
                    continue
                if t.startswith("-m"):
                    mode = t[2:] or toks[i + 1]
                    i += 1 if t[2:] else 2
                    continue
                args.append(t)
                i += 1
            dest, srcs = args[-1], args[:-1]
            if not dest.startswith(OPT + "/"):
                continue
            for s in srcs:
                if not s.startswith("$S/") or "*" in s:
                    continue
                rel = s[3:]
                if dest.endswith("/"):
                    target = dest + os.path.basename(rel)
                else:
                    target = dest[:-4] if dest.endswith(".new") else dest
                out[target] = (rel, mode, owner)
    return out


def imports_of(path):
    """Every top-level module name imported anywhere in the file (absolute imports, nested ones included)."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def repo_module(name, first):
    """Repo source of a local module, searched from the importing file's folder, else None."""
    for d in (first,) + REPO_DIRS:
        p = os.path.normpath(os.path.join(ROOT, d, name + ".py"))
        if os.path.isfile(p):
            return os.path.relpath(p, ROOT)
    return None


def admin_graph():
    """{module: repo path} for grok_admin.py and every local module it reaches."""
    seen, todo = {"grok_admin": "admin/grok_admin.py"}, ["admin/grok_admin.py"]
    while todo:
        rel = todo.pop()
        for name in sorted(imports_of(os.path.join(ROOT, rel))):
            src = repo_module(name, os.path.dirname(rel))
            if src and name not in seen:
                seen[name] = src
                todo.append(src)
    return seen


def heredocs(script):
    """{file name: body} for every `cat > "$T/<name>" <<'TAG'` block in a shell script."""
    with open(script, encoding="utf-8") as fh:
        text = fh.read()
    out = {}
    for line in text.splitlines():
        if line.startswith('cat > "$T/') and "<<'" in line:
            name = line.split('"$T/', 1)[1].split('"', 1)[0]
            tag = line.split("<<'", 1)[1].split("'", 1)[0]
            out[name] = text.split(line + "\n", 1)[1].split("\n" + tag + "\n", 1)[0] + "\n"
    return out


class AdminInstall(unittest.TestCase):
    def test_graph_reaches_gs_render(self):
        graph = admin_graph()
        for m in ("gs_routine", "gs_render", "owner_check", "safe_io", "bot_content", "gs_integrations", "dimming"):
            self.assertIn(m, graph)

    def test_every_admin_import_is_installed(self):
        files = installed_files()
        for name, src in sorted(admin_graph().items()):
            hits = [d + "/" + name + ".py" for d in ADMIN_PATH if d + "/" + name + ".py" in files]
            self.assertTrue(hits, "%s (%s) is imported by the admin page but tools/install-files.sh never installs it"
                            % (name, src))
            self.assertEqual(files[hits[0]][0], src, name)

    def test_gs_render_installed_like_gs_routine(self):
        files = installed_files()
        self.assertIn(OPT + "/admin/gs_render.py", files)
        self.assertEqual(files[OPT + "/admin/gs_render.py"][1:], files[OPT + "/admin/gs_routine.py"][1:])
        self.assertEqual(files[OPT + "/admin/gs_render.py"][1], "644")

    def test_installed_layout_imports(self):
        """Stage the installed tree and import grok_admin from it, isolated from the repo."""
        files = installed_files()
        with tempfile.TemporaryDirectory() as d:
            opt = os.path.join(d, "opt")
            for target, (rel, mode, _owner) in files.items():
                if not target.endswith(".py") and not target.endswith(".txt"):
                    continue
                dst = os.path.join(opt, os.path.relpath(target, OPT))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(os.path.join(ROOT, rel), dst)
                os.chmod(dst, int(mode or "755", 8))
            state, run = os.path.join(d, "state"), os.path.join(d, "run")
            os.makedirs(state)
            os.makedirs(run)
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ADMIN_STATE_DIR": state, "ADMIN_RUN_DIR": run,
                   "ADMIN_VERSION_FILE": os.path.join(d, "VERSION"), "ADMIN_HELPER": "/bin/false",
                   "WAKE_CONFIG": os.path.join(d, "wakewords.json"), "ADMIN_PERSONAS": os.path.join(d, "p.json")}
            admin = os.path.join(opt, "admin")
            code = ("import sys; sys.path.insert(0, %r); sys.dont_write_bytecode = True\n"
                    "import grok_admin, gs_routine, gs_render, owner_check, safe_io\n"
                    "bad = [m.__name__ for m in (grok_admin, gs_routine, gs_render, owner_check, safe_io)\n"
                    "       if not m.__file__.startswith(%r)]\n"
                    "print('outside', bad) if bad else print('ok')\n" % (admin, opt + os.sep))
            r = subprocess.run([sys.executable, "-I", "-c", code], cwd=d, env=env, capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            self.assertEqual(r.stdout.strip().splitlines()[-1], "ok", r.stdout)
            self.assertFalse(stat.S_IMODE(os.stat(os.path.join(admin, "gs_render.py")).st_mode) & 0o022)


class BotUpdate(unittest.TestCase):
    """scripts/gs-update.sh runs its embedded gs_routine.py from $T; every local module it reaches is written there."""

    def test_embedded_graph_is_complete_and_byte_identical(self):
        docs = heredocs(UPDATE)
        self.assertIn("gs_routine.py", docs)
        todo, seen = ["admin/gs_routine.py"], set()
        while todo:
            rel = todo.pop()
            name = os.path.basename(rel)
            if name in seen:
                continue
            seen.add(name)
            self.assertIn(name, docs, "gs-update.sh does not write %s next to gs_routine.py" % name)
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                self.assertEqual(docs[name], fh.read(), "%s in gs-update.sh differs from %s" % (name, rel))
            for m in sorted(imports_of(os.path.join(ROOT, rel))):
                src = repo_module(m, os.path.dirname(rel))
                if src:
                    todo.append(src)
        self.assertTrue({"gs_routine.py", "gs_render.py", "owner_check.py"} <= seen)


if __name__ == "__main__":
    unittest.main()
