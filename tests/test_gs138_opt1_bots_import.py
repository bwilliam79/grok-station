#!/usr/bin/env python3
"""OPT1: test_bots_api must import relay/handoff from this file's location.

The relay directory is a sys.path entry, not a package. BotsApi.asyncSetUp used to
`import handoff` / `import relay` and succeed only when cwd or an earlier test had
already put relay/ on sys.path. A clean process with a foreign cwd must still set up.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BotsApiImportPath(unittest.TestCase):
    def test_setup_imports_relay_from_a_foreign_cwd(self):
        foreign = tempfile.mkdtemp(prefix="gs138-opt1-")
        script = textwrap.dedent(
            """\
            import importlib.util, inspect, os, sys
            root = os.environ["GS_ROOT"]
            os.chdir(os.environ["GS_FOREIGN"])
            relay_dir = os.path.realpath(os.path.join(root, "relay"))
            sys.path = [p for p in sys.path if not p or os.path.realpath(p) != relay_dir]
            path = os.path.join(root, "tests", "test_bots_api.py")
            spec = importlib.util.spec_from_file_location("test_bots_api_opt1", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            src = inspect.getsource(mod.BotsApi.asyncSetUp)
            helper_at = src.find("_relay_on_path()")
            import_at = src.find("import handoff")
            if helper_at < 0 or import_at < 0 or helper_at > import_at:
                raise SystemExit("asyncSetUp does not put relay/ on sys.path before importing handoff")
            for name in ("handoff", "relay", "botreply", "home", "voices", "late_answer", "t1"):
                sys.modules.pop(name, None)
            sys.path = [p for p in sys.path if not p or os.path.realpath(p) != relay_dir]
            mod._relay_on_path()
            import handoff
            import relay
            got = os.path.realpath(os.path.dirname(relay.__file__))
            if got != relay_dir:
                raise SystemExit("relay loaded from %s" % got)
            if os.path.realpath(os.path.dirname(handoff.__file__)) != relay_dir:
                raise SystemExit("handoff loaded from %s" % handoff.__file__)
            print("OK")
            """
        )
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["GS_ROOT"] = ROOT
        env["GS_FOREIGN"] = foreign
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=foreign,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + "\n" + proc.stderr)
        self.assertIn("OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
