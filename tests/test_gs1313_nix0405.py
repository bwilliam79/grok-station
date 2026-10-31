"""NIX-1313-04: safe_io writes refuse a foreign owner, a hard link and a failed fchmod; tmp-then-rename writers use a
random O_EXCL temp. NIX-1313-05: wake reads from folders another service writes go through safe_io (FIFO, symlink).
The alarm guard raises a non-OSError so no handler can swallow a hang."""
import json
import os
import shutil
import signal
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "relay"))
sys.path.insert(0, os.path.join(ROOT, "wake"))
sys.path.insert(0, HERE)
import safe_io  # noqa: E402
import handoff  # noqa: E402
import tools  # noqa: E402
import home  # noqa: E402


class Hang(Exception):
    pass


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.outside = os.path.join(self.d, "outside")
        with open(self.outside, "w") as f:
            f.write("KEEP")

        def fire(*a):
            raise Hang("blocked on a FIFO")
        self._old = signal.signal(signal.SIGALRM, fire)
        signal.alarm(3)
        self.logs = []
        self._log = safe_io._log[0]
        safe_io.set_log(self.logs.append)

    def tearDown(self):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self._old)
        safe_io.set_log(self._log)
        shutil.rmtree(self.d, ignore_errors=True)

    def p(self, name):
        return os.path.join(self.d, name)

    def planted_0666(self, name):
        p = self.p(name)
        with open(p, "w") as f:
            f.write("PLANTED")
        os.chmod(p, 0o666)
        return p

    def hardlink(self, name):
        p = self.p(name)
        os.link(self.outside, p)
        return p

    def foreign(self):
        """A planted file owned by another uid: as non-root, emulate by changing who this process is to safe_io."""
        return mock.patch.object(safe_io.os, "geteuid", lambda: os.getuid() + 1)

    def logged(self, word):
        self.assertTrue(any(word in l for l in self.logs), self.logs)


class Nix04Writes(Base):
    def test_write_refuses_planted_0666_foreign_owner(self):
        p = self.planted_0666("w.json")
        with self.foreign(), self.assertRaises(safe_io.Refused):
            safe_io.write_file(p, "NEW", "t")
        self.assertEqual(open(p).read(), "PLANTED")
        self.logged("write refused foreign-owner")

    def test_append_refuses_planted_0666_foreign_owner(self):
        p = self.planted_0666("a.log")
        with self.foreign(), self.assertRaises(safe_io.Refused):
            safe_io.open_append(p, "t")
        self.assertEqual(open(p).read(), "PLANTED")
        self.logged("write refused foreign-owner")

    def test_write_refuses_hardlink(self):
        with self.assertRaises(safe_io.Refused):
            safe_io.write_file(self.hardlink("w.json"), "NEW", "t")
        self.assertEqual(open(self.outside).read(), "KEEP")
        self.logged("write refused hardlink")

    def test_append_refuses_hardlink(self):
        with self.assertRaises(safe_io.Refused):
            safe_io.open_append(self.hardlink("a.log"), "t")
        self.assertEqual(open(self.outside).read(), "KEEP")
        self.logged("write refused hardlink")

    def test_failed_fchmod_refuses(self):
        def deny(*a):
            raise PermissionError(1, "denied")
        p = self.p("m.json")
        with mock.patch.object(safe_io.os, "fchmod", deny), self.assertRaises(safe_io.Refused):
            safe_io.write_file(p, "x", "t", mode=0o640, excl=True)
        self.assertFalse(os.path.exists(p))  # created by this call, so removed
        self.logged("write refused fchmod")

    def test_allowed_owner_passes(self):
        p = self.planted_0666("ok.log")
        with self.foreign():
            fd, created = safe_io.open_append(p, "t", owners=(os.getuid(),))
        os.close(fd)
        self.assertFalse(created)

    # (3) fixed-name tmp writers: a hard-linked or planted <path>.tmp is never written through
    def test_handoff_limits_save_ignores_hardlinked_tmp(self):
        path = self.p("handoff.json")
        self.hardlink("handoff.json.tmp")
        handoff.Limits(path, lambda: "2026-10-31")._save({"day": "2026-10-31"})
        self.assertEqual(open(self.outside).read(), "KEEP")
        self.assertEqual(json.load(open(path)), {"day": "2026-10-31"})

    def test_pending_write_ignores_hardlinked_tmp(self):
        rid = "vs_" + "a" * 22
        self.hardlink("." + rid + ".tmp")
        handoff.write_pending(self.d, rid, "jules", "n", 0, "q")
        self.assertEqual(open(self.outside).read(), "KEEP")
        self.assertTrue(os.path.isfile(self.p(rid + ".json")))

    def test_timer_save_ignores_hardlinked_tmp(self):
        path = self.p("timers.json")
        with open(path, "w") as f:
            f.write("{}")  # present, so an empty save still rewrites it
        self.hardlink("timers.json.tmp")
        self.assertTrue(tools.save_timer_state(path, {}, 1.0, wall_now=1.0))
        self.assertEqual(open(self.outside).read(), "KEEP")
        self.assertEqual(json.load(open(path))["timers"], {})

    def test_homebridge_block_ignores_hardlinked_tmp(self):
        path = self.p("homebridge.json")
        self.hardlink("homebridge.json.tmp")
        with mock.patch.dict(os.environ, {"HOMEBRIDGE_BLOCK": path}):
            home._hb_write_block("admin")
        self.assertEqual(open(self.outside).read(), "KEEP")
        self.assertEqual(json.load(open(path)), {"blocked": "admin"})


class Nix05Wake(Base):
    @classmethod
    def setUpClass(cls):
        import grok_wake
        cls.g = grok_wake

    def fifo(self, name):
        p = self.p(name)
        os.mkfifo(p, 0o660)
        return p

    def link(self, name, text='{"guest": true, "mode": "setup", "button": false}'):
        with open(self.outside, "w") as f:
            f.write(text)
        p = self.p(name)
        os.symlink(self.outside, p)
        return p

    def _both(self, name, attr, call, fallback):
        for p in (self.fifo(name), self.link(name + "2")):
            with mock.patch.object(self.g, attr, p):
                self.assertEqual(call(), fallback)
        self.logged("read refused")

    def test_t1_admin_dir(self):
        self._both("t1.json", "T1_FILE", lambda: json.loads(__import__("urllib.parse").parse.unquote(self.g.t1_query())).get("guest"), False)

    def test_setup_run_dir(self):
        self._both("setup.json", "SETUP_FILE", self.g.setup_mode, None)

    def test_overrides_admin_dir(self):
        base = dict(self.g.enroll_phrases())
        self._both("ov.json", "OVERRIDES", self.g.enroll_phrases, base)

    def test_mic_reset_admin_dir(self):
        base = self.g.mic_reset_flags(self.p("missing.json"))
        for p in (self.fifo("mr.json"), self.link("mr2.json")):
            self.assertEqual(self.g.mic_reset_flags(p), base)
        self.logged("read refused")

    def test_late_job_and_pcm(self):
        rid = "vs_" + "b" * 22
        for make in (self.fifo, self.link):
            for f in os.listdir(self.d):
                if f != "outside":
                    os.unlink(self.p(f))
            make(rid + ".json")
            with mock.patch.object(self.g, "LATE_DIR", self.d):
                self.assertIsNone(self.g.load_late_job(rid))
        self.logged("read refused")

    def test_dropped_wakewords(self):
        drop = os.path.join(self.d, "drop")
        os.mkdir(drop)
        os.mkfifo(os.path.join(drop, "hey_a.json"))
        with open(os.path.join(drop, "hey_b.json"), "w") as f:
            json.dump({"name": "hey_b", "sha256": "0" * 64}, f)
        os.symlink(self.outside, os.path.join(drop, "hey_b.onnx"))
        self.assertEqual(self.g.load_dropped_wakewords(drop), [])
        self.logged("read refused")


class AdminSiblings(Base):
    """Same class on the admin side: files the relay or wake writes, read by the admin page."""

    @classmethod
    def setUpClass(cls):
        import test_admin
        cls.ga = test_admin.ga
        import gs_integrations
        cls.gsi = gs_integrations

    def _pair(self, name):
        f = self.p(name)
        os.mkfifo(f)
        with open(self.outside, "w") as fh:
            fh.write('{"blocked": "admin", "words": [1]}')
        l = self.p(name + "2")
        os.symlink(self.outside, l)
        return f, l

    def test_homebridge_block_read(self):
        for p in self._pair("homebridge.json"):
            with mock.patch.dict(os.environ, {"HOMEBRIDGE_BLOCK": p}):
                self.assertIsNone(self.gsi.hb_block_kind())
        self.logged("read refused")

    def test_read_json_enrolled(self):
        for p in self._pair("wakewords.enrolled.json"):
            self.assertEqual(self.ga.read_json(p, {"d": 1}), {"d": 1})
        self.logged("read refused")


class DisplaySiblings(Base):
    """The display (grok) reads two files the admin page (grok-admin) writes."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(ROOT, "app"))
        import test_cards  # noqa: F401  (display test env: no screen)
        import grok_station
        cls.gs = grok_station

    def _pair(self, name):
        f = self.p(name)
        os.mkfifo(f)
        with open(self.outside, "w") as fh:
            fh.write('{"mode": "setup", "code": "ABCDEF", "latest": "99.0.0"}')
        l = self.p(name + "2")
        os.symlink(self.outside, l)
        return f, l

    def test_update_check(self):
        v = self.p("VERSION")
        with open(v, "w") as fh:
            fh.write("0.0.1\n")
        for p in self._pair("update-check.json"):
            self.assertFalse(self.gs.read_update_available(p, v))
        self.logged("read refused")

    def test_setup_file(self):
        for p in self._pair("setup.json"):
            with mock.patch.object(self.gs, "SETUP_FILE", p):
                self.assertIsNone(self.gs.read_setup())
        self.logged("read refused")


if __name__ == "__main__":
    unittest.main()
