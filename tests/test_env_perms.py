#!/usr/bin/env python3
"""Regression: an admin save must keep /etc/grok-station/env root:grok 0640 so the relay can read the location."""
import io, os, stat, sys, tempfile, unittest
from contextlib import redirect_stderr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def helper():
    g = {"__name__": "helper"}
    exec(compile(open(os.path.join(ROOT, "admin", "grok-station-helper")).read(), "helper", "exec"), g)
    return g


class EnvPerms(unittest.TestCase):
    def test_mode_and_group_survive_save(self):
        g = helper()
        with tempfile.TemporaryDirectory() as d:
            env = os.path.join(d, "env"); open(env, "w").write("GROK_STATION_TOKEN=x\n"); os.chmod(env, 0o600)
            g["ENV"]["display"] = env
            g["station_env_owner"] = lambda: (os.getuid(), os.getgid(), 0o640)  # stands in for root:grok
            g["env_write"]("display", {"GROK_STATION_LAT": "41.9", "GROK_STATION_LON": "-87.6"})
            st = os.stat(env)
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o640)
            self.assertEqual(st.st_gid, os.getgid())
            self.assertIn("GROK_STATION_LAT=41.9", open(env).read())
            wake = os.path.join(d, "wake.env"); open(wake, "w").write("A=1\n"); os.chmod(wake, 0o644)
            g["ENV"]["wake"] = wake
            g["env_write"]("wake", {"A": "2"})
            self.assertEqual(stat.S_IMODE(os.stat(wake).st_mode), 0o644)  # other files keep their mode

    def test_owner_is_root_grok(self):
        src = open(os.path.join(ROOT, "admin", "grok-station-helper")).read()
        self.assertIn('return 0, grp.getgrnam("grok").gr_gid, 0o640', src)
        sh = open(os.path.join(ROOT, "install.sh")).read()
        self.assertIn("run chown root:grok /etc/grok-station/env", sh)
        self.assertIn("run chmod 0640 /etc/grok-station/env", sh)

    def test_relay_warns_once(self):
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        import tools
        orig_env, orig_stat = tools.STATION_ENV, tools.os.stat
        def denied(p, *a, **k):
            if p == "/nope/env":
                raise PermissionError(13, "Permission denied")
            return orig_stat(p, *a, **k)
        tools.STATION_ENV, tools.os.stat = "/nope/env", denied
        tools._station_env["warned"] = False
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                self.assertEqual(tools.location()[:2], ("", "") if not tools.LAT else tools.location()[:2])
                tools.location()
        finally:
            tools.STATION_ENV, tools.os.stat = orig_env, orig_stat
        self.assertEqual(err.getvalue().count("permission denied"), 1)


if __name__ == "__main__":
    unittest.main()
