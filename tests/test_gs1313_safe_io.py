"""NIX-1313-03 round 2: every read and write in a group-writable station folder goes through relay/safe_io.py.

Each helper refuses a FIFO (without blocking) and a symlink; one representative caller per folder keeps its
missing-file fallback when refused. The alarm guard raises a non-OSError so no handler can swallow a hang."""
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_cards as tc  # noqa: E402,F401  (same env as the other relay tests)
import relay  # noqa: E402
import handoff  # noqa: E402


class Hang(Exception):
    pass


class Guarded(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.outside = os.path.join(self.d, "outside")
        with open(self.outside, "w") as f:
            f.write('{"secret": true}')

        def fire(*a):
            raise Hang("blocked on a FIFO")
        self._old = signal.signal(signal.SIGALRM, fire)
        signal.alarm(3)
        self.logs = []
        try:
            import safe_io
            self._saved_log = safe_io._log[0]
            safe_io.set_log(self.logs.append)
        except ImportError:
            self._saved_log = None
        self._relay_log = relay.log
        relay.log = lambda *a: self.logs.append(" ".join(map(str, a)))

    def tearDown(self):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self._old)
        relay.log = self._relay_log
        if self._saved_log is not None:
            import safe_io
            safe_io.set_log(self._saved_log)
        shutil.rmtree(self.d, ignore_errors=True)

    def fifo(self, name):
        p = os.path.join(self.d, name)
        os.mkfifo(p, 0o660)
        return p

    def link(self, name):
        p = os.path.join(self.d, name)
        os.symlink(self.outside, p)
        return p

    def refused(self, verb="read"):
        self.assertTrue(any(f"{verb} refused" in l for l in self.logs), self.logs)


class Helpers(Guarded):
    def _reads(self):
        import safe_io
        return [lambda p: safe_io.read_bytes(p, 1024, "t"), lambda p: safe_io.read_text(p, 1024, "t"),
                lambda p: safe_io.read_json(p, 1024, "t")]

    def test_readers_refuse_fifo(self):
        import safe_io
        for fn in self._reads():
            p = self.fifo("f%d" % len(self.logs))
            with self.assertRaises(safe_io.Refused):
                fn(p)
        self.assertIsNone(safe_io.read_json_or_none(self.fifo("g"), 1024, "t"))
        self.assertTrue(all("read refused not-regular" in l for l in self.logs), self.logs)

    def test_readers_refuse_symlink(self):
        import safe_io
        for i, fn in enumerate(self._reads()):
            with self.assertRaises(safe_io.Refused):
                fn(self.link("l%d" % i))
        self.assertIsNone(safe_io.read_json_or_none(self.link("m"), 1024, "t"))
        self.refused()

    def test_readers_cap_and_missing(self):
        import safe_io
        big = os.path.join(self.d, "big")
        with open(big, "wb") as f:
            f.write(b"x" * 2048)
        with self.assertRaises(safe_io.Refused):
            safe_io.read_bytes(big, 1024, "t")
        with self.assertRaises(FileNotFoundError):
            safe_io.read_bytes(os.path.join(self.d, "nope"), 1024, "t")
        self.assertEqual(safe_io.read_bytes(big, 4096, "t"), b"x" * 2048)

    def test_writers_refuse_fifo(self):
        import safe_io
        with self.assertRaises(safe_io.Refused):
            safe_io.open_write(self.fifo("w1"), "t")
        with self.assertRaises(safe_io.Refused):
            safe_io.open_append(self.fifo("w2"), "t")
        with self.assertRaises(safe_io.Refused):
            safe_io.write_file(self.fifo("w3"), b"x", "t")
        self.refused("write")

    def test_writers_refuse_symlink(self):
        import safe_io
        for name, fn in (("s1", lambda p: safe_io.open_write(p, "t")),
                         ("s2", lambda p: safe_io.open_append(p, "t")),
                         ("s3", lambda p: safe_io.write_file(p, b"x", "t"))):
            with self.assertRaises(safe_io.Refused):
                fn(self.link(name))
        self.assertEqual(open(self.outside).read(), '{"secret": true}')
        self.refused("write")

    def test_write_file_mode_and_append(self):
        import safe_io
        p = os.path.join(self.d, "ok")
        safe_io.write_file(p, "hi", "t", mode=0o640)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o640)
        fd, created = safe_io.open_append(p, "t")
        os.write(fd, b"!")
        os.close(fd)
        self.assertFalse(created)
        self.assertEqual(open(p).read(), "hi!")


class Callers(Guarded):
    """One representative caller per group-writable folder: refused like missing, no hang, logged."""

    def test_state_handoff_limits(self):
        for p in (self.fifo("handoff.json"), self.link("handoff2.json")):
            lim = handoff.Limits(p, lambda: "2026-10-31")
            self.assertEqual(lim._load()["bots"], {})
        self.refused()

    def test_bots_webhooks_read(self):
        for p in (self.fifo("webhooks.json"), self.link("webhooks2.json")):
            self.assertIsNone(relay.read_json_file(p))
        self.refused()

    def test_voice_reply(self):
        for name in ("rf", "rl"):
            rid = "r" + "0" * 15 + name
            p = os.path.join(self.d, rid + ".json")
            (os.mkfifo(p) if name == "rf" else os.symlink(self.outside, p))
            self.assertEqual(handoff.take_reply(self.d, rid, "jules", "n")[0], None)
        self.refused()

    def test_personas_registry(self):
        saved = relay.PERSONAS_FILE
        try:
            for p in (self.fifo("personas.json"), self.link("personas2.json")):
                relay.PERSONAS_FILE = p
                self.assertEqual(relay.registry_index(), {})
        finally:
            relay.PERSONAS_FILE = saved
        self.refused()

    def test_portraits(self):
        for p in (self.fifo("jules.png"), self.link("kai.png")):
            self.assertIsNone(relay._portrait_bytes(p))
        self.refused()

    def test_audit_append(self):
        for p in (self.fifo("audit.log"), self.link("audit2.log")):
            with self.assertRaises(OSError):
                relay._append_audit(p, "line\n", 0o600)
        self.assertEqual(open(self.outside).read(), '{"secret": true}')
        self.refused("write")


if __name__ == "__main__":
    unittest.main()
