"""0.12.16 security review, Info items.

1. /api/portrait decides caching from os.fstat of the handle it read, not a later stat (an upload in between
   must not be cached under the new version).
2. The cacheable header is `private, max-age=86400`, not immutable.
3. The automatic avatar ask sees a persona-folder picture in any format, and its once-only rule survives a restart.
"""
import asyncio, os, stat, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "relay"))


class PortraitCacheRace(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, HERE)
        import test_admin
        self.ga = ga = test_admin.ga
        base = os.path.dirname(ga.PERSONAS)
        os.makedirs(base, exist_ok=True)
        self.f = f = os.path.join(base, "zed.png")
        with open(f, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        self.addCleanup(lambda: os.path.exists(f) and os.unlink(f))
        self.c = test_admin.signed_in()
        self.v_old = ga.portrait_version("zed")

    def get(self, q):
        st, body = self.c.req("GET", "/api/portrait/zed" + q, raw=True)
        return st, self.c.last.getheader("Cache-Control"), body

    def test_upload_between_read_and_stat_is_not_cached_as_new(self):
        """The old bytes are read, then a new picture lands before the version is worked out. Old bytes must not
        be cacheable under the new picture's ?v=; under their own version they may be."""
        ga, f = self.ga, self.f
        real_open = open
        new_ns, new_body = 1_900_000_000_123_456_789, b"\x89PNG\r\n\x1a\n" + b"1" * 99
        new_v = "%x-%x" % (new_ns, len(new_body))

        def racing_open(path, *a, **kw):
            fh = real_open(path, *a, **kw)
            if a[:1] == ("rb",) and os.path.realpath(str(path)) == os.path.realpath(f):
                tmp = f + ".up"
                with real_open(tmp, "wb") as w:
                    w.write(new_body)  # the upload lands while we hold the old file's handle
                os.utime(tmp, ns=(new_ns, new_ns))
                os.replace(tmp, f)
            return fh

        def reset():
            with real_open(f, "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
            return ga.portrait_version("zed")

        ga.open = racing_open
        self.addCleanup(lambda: delattr(ga, "open"))
        st, cc, body = self.get("?v=" + new_v)
        self.assertEqual((st, len(body)), (200, 72))  # the old bytes went out
        self.assertEqual(ga.portrait_version("zed"), new_v)  # and the new picture is now on disk
        self.assertEqual(cc, "no-store", "old bytes cached under the new picture's version")
        old_v = reset()
        st, cc, body = self.get("?v=" + old_v)
        self.assertEqual((st, len(body)), (200, 72))
        self.assertNotEqual(cc, "no-store", "the version of the bytes actually sent should be cacheable")

    def test_cache_header_is_a_day_not_immutable(self):
        st, cc, _b = self.get("?v=" + self.v_old)
        self.assertEqual((st, cc), (200, "private, max-age=86400"))
        self.assertNotIn("immutable", self.ga.PORTRAIT_CACHE)


class AvatarAutoAsk(unittest.TestCase):
    def setUp(self):
        import relay
        self.r = r = relay
        self.tmp = tempfile.mkdtemp()
        saved = {k: getattr(r, k) for k in ("PORTRAIT_WRITE", "PORTRAIT_DIRS", "PERSONAS_FILE")
                 if hasattr(r, k)}
        if hasattr(r, "AVATAR_AUTO_FILE"):
            saved["AVATAR_AUTO_FILE"] = r.AVATAR_AUTO_FILE
        self.addCleanup(lambda: [setattr(r, k, v) for k, v in saved.items()])
        r.PORTRAIT_WRITE = os.path.join(self.tmp, "portraits")
        r.PORTRAIT_DIRS = [os.path.join(self.tmp, "ro")]
        r.PERSONAS_FILE = os.path.join(self.tmp, "personas", "personas.json")
        r.AVATAR_AUTO_FILE = os.path.join(self.tmp, "avatar-auto.json")
        for d in (r.PORTRAIT_WRITE, r.PORTRAIT_DIRS[0], os.path.dirname(r.PERSONAS_FILE)):
            os.makedirs(d)
        r._avatar_auto.clear()
        self.addCleanup(r._avatar_auto.clear)

    def test_persona_folder_picture_in_any_format_means_no_ask(self):
        r = self.r
        self.assertTrue(r.avatar_auto_due("jules", time.monotonic()))
        for d in (os.path.dirname(r.PERSONAS_FILE), r.PORTRAIT_DIRS[0]):
            for ext in ("png", "jpg", "jpeg", "webp"):
                f = os.path.join(d, "jules." + ext)
                open(f, "wb").write(b"x")
                self.assertFalse(r.avatar_auto_due("jules", time.monotonic()), f)
                os.unlink(f)

    def test_once_only_survives_a_restart(self):
        r = self.r
        self.assertTrue(r.avatar_auto_due("jules", time.monotonic()))
        if hasattr(r, "avatar_auto_mark"):
            r.avatar_auto_mark("jules")
        else:
            r._avatar_auto["jules"] = time.monotonic()
        r._avatar_auto.clear()  # what a restart does to the in-memory record
        self.assertFalse(r.avatar_auto_due("jules", time.monotonic() + 7200), "asked again after a restart")
        mode = stat.S_IMODE(os.stat(r.AVATAR_AUTO_FILE).st_mode)
        self.assertEqual(mode & 0o007, 0, oct(mode))  # no world access
        r.forget_bot_status("jules")  # a new pairing resets it
        self.assertTrue(r.avatar_auto_due("jules", time.monotonic()))
