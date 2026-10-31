"""NIX-1313-07: replace_file fsyncs the directory. NIX-1313-08: a failed write is reported, never swallowed
(wake-word delete on the admin page and in the wake listener, plus the sibling admin and display writes)."""
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "relay"))
sys.path.insert(0, os.path.join(ROOT, "wake"))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, HERE)
import safe_io  # noqa: E402

NOT_ROOT = unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory modes")


def denied(*a, **k):
    raise PermissionError(13, "Permission denied")


class DirFsync(unittest.TestCase):
    def test_replace_file_fsyncs_the_directory(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        kinds = []
        real = os.fsync

        def spy(fd):
            kinds.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            return real(fd)
        with mock.patch.object(safe_io.os, "fsync", spy):
            safe_io.replace_file(os.path.join(d, "x.json"), "{}", "t")
        self.assertEqual(kinds, ["file", "dir"])  # the temp's data, then the rename

    def test_failed_dir_fsync_is_logged_not_raised(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        logs = []
        old = safe_io._log[0]
        safe_io.set_log(logs.append)
        real = os.fsync

        def fail_dir(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(5, "EIO")
            return real(fd)
        try:
            with mock.patch.object(safe_io.os, "fsync", fail_dir):
                safe_io.replace_file(os.path.join(d, "x.json"), "{}", "t")
        finally:
            safe_io.set_log(old)
        self.assertEqual(json.load(open(os.path.join(d, "x.json"))), {})
        self.assertTrue(any("dir fsync failed" in l for l in logs), logs)


class AdminWrites(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_admin
        cls.ta = test_admin
        cls.ga = test_admin.ga

    def out(self, fn, *a):
        buf = io.StringIO()
        with redirect_stdout(buf):
            r = fn(*a)
        return r, buf.getvalue()

    @NOT_ROOT
    def test_delete_reports_eacces_on_the_enrolled_list(self):
        ga = self.ga
        c = self.ta.signed_in()
        edir = os.path.dirname(ga.ENROLLED)
        os.makedirs(edir, exist_ok=True)
        ga.write_json(ga.ENROLLED, {"words": [{"name": "hey_dene", "persona": "dene", "kind": "fewshot"}]}, 0o640)
        os.chmod(edir, 0o555)  # like /opt/grok-station/wake/models/enrolled (grok 0755) as seen by grok-admin
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                st, body = c.req("DELETE", "/api/wakewords/hey_dene", {})
        finally:
            os.chmod(edir, 0o755)
            ga.write_json(ga.ENROLLED, {"words": []}, 0o640)
        self.assertNotEqual(st, 200, body)
        self.assertIn("error", body)
        self.assertIn("wakeword delete failed", buf.getvalue())

    def test_sessions_save_failure_logged(self):
        with mock.patch.object(self.ga, "write_json", denied):
            _, out = self.out(self.ga.AUTH._save_sessions)
        self.assertIn("admin sessions save failed PermissionError", out)

    def test_stale_guest_rewrite_failure_logged(self):
        p = self.ga.path("t1.json")
        self.ga.write_json(p, {"guest": True}, 0o640)
        with mock.patch.object(self.ga, "write_json", denied):
            _, out = self.out(self.ga.drop_stale_guest)
        self.assertIn("t1 guest clear failed PermissionError", out)

    def test_admin_audit_append_failure_logged(self):
        with mock.patch("builtins.open", denied):
            _, out = self.out(self.ga.append_audit, "audit persona=- action=x last4=- time=1")
        self.assertIn("admin audit write failed PermissionError", out)

    def test_homebridge_clear_failure_logged_missing_quiet(self):
        import gs_integrations as gsi
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "homebridge.json")
        with mock.patch.dict(os.environ, {"HOMEBRIDGE_BLOCK": p}):
            _, out = self.out(gsi.hb_clear_block)
            self.assertEqual(out, "")
            open(p, "w").close()
            with mock.patch.object(gsi.os, "unlink", denied):
                _, out = self.out(gsi.hb_clear_block)
        self.assertIn("homebridge block clear failed PermissionError", out)


class WakeForget(unittest.TestCase):
    @NOT_ROOT
    def test_forget_reports_eacces_on_the_enrolled_list(self):
        import grok_wake as gw
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: (os.chmod(os.path.join(d, "enr"), 0o755), shutil.rmtree(d, True)))
        inbox, enr = os.path.join(d, "in"), os.path.join(d, "enr")
        os.makedirs(inbox)
        os.makedirs(enr)
        json.dump({"words": [{"name": "hey_dene"}]}, open(os.path.join(enr, "wakewords.enrolled.json"), "w"))
        os.chmod(enr, 0o555)
        old = gw.ENROLL_DIR, gw.ENROLLED_DIR
        gw.ENROLL_DIR, gw.ENROLLED_DIR = inbox, enr
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                ok, err = gw.forget_word("hey_dene")
        finally:
            gw.ENROLL_DIR, gw.ENROLLED_DIR = old
        self.assertEqual((ok, err), (False, "write failed"))
        self.assertIn("wakeword forget failed", buf.getvalue())
        self.assertIn("write refused PermissionError", buf.getvalue())


class DisplayWrites(unittest.TestCase):
    def test_weather_cache_failure_logged(self):
        import test_cards  # noqa: F401
        import grok_station as gs
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("builtins.open", denied):
            gs.save_weather_cache({"t": 1}, 1.0, "/nonexistent-dir/weather.json")
        self.assertIn("weather cache save failed", buf.getvalue())

    def test_dimming_stamp_failure_logged(self):
        import dimming
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(dimming, "write_backlight", lambda *a: True), \
                mock.patch.object(dimming, "set_overlay", lambda *a: None), mock.patch("builtins.open", denied):
            dimming.apply_brightness(40, "/", stamp=os.path.join(tempfile.mkdtemp(), "dim"))
        self.assertIn("dim stamp write failed PermissionError", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
