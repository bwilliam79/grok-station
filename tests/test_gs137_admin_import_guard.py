"""0.13.7 (I1): no test file imports grok_admin before the admin test environment is set.

grok_admin reads ADMIN_STATE_DIR and friends once, at import. A test file that imported it first fixed those paths
at /var/lib/grok-station for every later file in the same run, so test_finish_update's Station tests wrote there
(06546c4: test_avatar_limits; then test_gs137_pins_commit). A static scan: in each test file, the first import of
grok_admin (top level or inside a function) must come after either an ADMIN_STATE_DIR assignment or an import of a
test module that itself sets the environment (test_admin, or one that passes this rule and sets it).

Only test_admin builds the sandbox. A file with its own ADMIN_STATE_DIR block does not count: whichever file ran
first fixed grok_admin's paths, helper and key file for every later file (a reversed-order run failed 155 tests
that way), so every file shares test_admin's sandbox.
"""
import ast
import functools
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _tree(name):
    with open(os.path.join(HERE, name + ".py"), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _events(name):
    """(line, col, kind, module) in source order: kind is 'admin' (grok_admin import), 'env' (ADMIN_STATE_DIR set)
    or 'test' (import of another test module)."""
    out = []
    for node in ast.walk(_tree(name)):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            mods = [node.module]
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value == "grok_admin":
            mods = ["grok_admin"]  # importlib.import_module("grok_admin") / __import__
        for m in mods:
            if m.split(".")[0] == "grok_admin":
                out.append((node.lineno, node.col_offset, "admin", m))
            elif m.startswith("test_") and os.path.exists(os.path.join(HERE, m + ".py")):
                out.append((node.lineno, node.col_offset, "test", m))
        if isinstance(node, ast.Constant) and node.value == "ADMIN_STATE_DIR":
            out.append((node.lineno, node.col_offset, "env", ""))
        if isinstance(node, ast.keyword) and node.arg == "ADMIN_STATE_DIR":
            out.append((node.lineno, node.col_offset, "env", ""))
    return sorted(out)


@functools.lru_cache(maxsize=None)
def sets_env(name, seen=()):
    """True when importing this test module leaves test_admin's sandbox set (it is test_admin, or imports one that is)."""
    if name == "test_admin":
        return True
    for _, _, kind, mod in _events(name):
        if kind == "test" and mod not in seen and mod != name and sets_env(mod, seen + (name,)):
            return True
    return False


def problems(name):
    if name == "test_admin":
        return []
    ready = False
    out = []
    for line, _, kind, mod in _events(name):
        if kind == "test" and mod != name and sets_env(mod, (name,)):
            ready = True
        elif kind == "admin" and not ready:
            out.append("%s.py:%d imports grok_admin before the admin test env is set" % (name, line))
            break
    for line, _, kind, _ in _events(name):
        if kind == "env" and _sets_state(name, line):
            out.append("%s.py:%d sets its own ADMIN_STATE_DIR; import test_admin instead" % (name, line))
            break
    return out


def _sets_state(name, line):
    """The ADMIN_STATE_DIR mention on this line assigns it (environ[...] =, update(...=), setdefault), not a read."""
    with open(os.path.join(HERE, name + ".py"), encoding="utf-8") as fh:
        text = fh.read().split("\n")[line - 1]
    return bool(re.search(r'ADMIN_STATE_DIR"\]\s*=[^=]|ADMIN_STATE_DIR\s*=|setdefault\(\s*"ADMIN_STATE_DIR"|\(\(\s*"ADMIN_STATE_DIR"', text))


class AdminImportGuard(unittest.TestCase):
    def test_every_test_file_sets_the_env_first(self):
        bad = []
        for fn in sorted(os.listdir(HERE)):
            if fn.endswith(".py") and fn.startswith("test_"):
                bad += problems(fn[:-3])
        self.assertEqual(bad, [])

    def test_the_rule_catches_the_two_known_leaks(self):
        import tempfile
        cases = {
            "test_zz_leak_a": 'import os, sys\nsys.path.insert(0, "admin")\nimport grok_admin as ga\n',  # 06546c4 shape
            "test_zz_leak_b": 'import grok_admin as ga  # noqa: E402\nimport test_admin\n',  # pins_commit shape
            "test_zz_ok_a": 'import test_admin\nga = test_admin.ga\n',
            "test_zz_ok_b": 'def t():\n    import test_admin\n    import grok_admin\n',
            # own sandbox (test_version shape before I1): fixed grok_admin's paths for later files
            "test_zz_leak_c": 'import os\nif "ADMIN_STATE_DIR" not in os.environ:\n    os.environ["ADMIN_STATE_DIR"] = "/tmp/x"\nimport grok_admin as ga\n',
        }
        global HERE
        old = HERE
        with tempfile.TemporaryDirectory() as d:
            for k, v in cases.items():
                open(os.path.join(d, k + ".py"), "w").write(v)
            open(os.path.join(d, "test_admin.py"), "w").write('import os\nos.environ["ADMIN_STATE_DIR"] = "x"\nimport grok_admin as ga\n')
            HERE = d
            sets_env.cache_clear()
            try:
                self.assertEqual(len(problems("test_zz_leak_a")), 1)
                self.assertEqual(len(problems("test_zz_leak_b")), 1)
                self.assertEqual(problems("test_zz_ok_a"), [])
                self.assertEqual(problems("test_zz_ok_b"), [])
                self.assertEqual(len(problems("test_zz_leak_c")), 2)
            finally:
                HERE = old
                sets_env.cache_clear()


if __name__ == "__main__":
    unittest.main()
