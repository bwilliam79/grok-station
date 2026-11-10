#!/usr/bin/env python3
"""0.13.37 regression: a re-pin logs the fingerprint and warns when it differs from the paired one.

At 0.13.36 PUT /admin/bots/<slug>/key wrote "pair -" to the audit log and answered {"ok": true}: nothing said which
key was pinned or that it replaced a different one, and the page pinned a different key without asking. Now the
relay logs `repin new=<fp> old=<fp|-> first|same|changed via=configure|setup`, returns the fingerprints, and the
page asks before it replaces a key with a different fingerprint (Configure and the Accept screen both say so).
Keys are made up. Only public verify-key fingerprints are logged.
"""
import base64, os, sys, unittest

from nacl.signing import SigningKey

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_avatar  # noqa: E402  (temp state dirs and relay import)
import gs1336_repaint_chrome as H  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def b64(sk):
    return base64.b64encode(bytes(sk.verify_key)).decode()


class Relay(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = test_avatar.Avatar.asyncTearDown
    admin = test_avatar.Avatar.admin

    async def asyncSetUp(self):
        await test_avatar.Avatar.asyncSetUp(self)
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        await self.client.close()
        app = web.Application()
        app.add_routes([web.put("/admin/bots/{slug}/key", self.relay.admin_key_put)])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addCleanup(setattr, self.relay, "_known", self.relay._known)
        self.relay._known = lambda slug: slug in ("jules", "otto")

    def audit_lines(self):
        path = self.relay.audit_path()
        if not os.path.exists(path):
            return []
        return [l for l in open(path).read().splitlines() if " repin " in l]

    async def put(self, slug, pub):
        resp = await self.client.put("/admin/bots/%s/key" % slug, headers=self.admin("application/json"),
                                     json={"actor": "admin", "public_key": pub,
                                           "expect_old_fp": self.relay.pinned_fingerprint(slug).replace(" ", "") or "-"})
        self.assertEqual(resp.status, 200, await resp.text())
        return await resp.json()

    async def test_same_and_changed_keys_are_logged_with_fingerprints(self):
        br = self.botreply
        old_pub = b64(self.signer)  # pinned for jules by the harness
        same = await self.put("jules", old_pub)
        self.assertEqual(same["changed"], False)
        self.assertEqual(same["fingerprint"], br.key_fingerprint(old_pub))
        self.assertEqual(same["previous_fingerprint"], br.key_fingerprint(old_pub))
        new_pub = b64(SigningKey.generate())
        changed = await self.put("jules", new_pub)
        self.assertEqual(changed["changed"], True)
        self.assertEqual(changed["previous_fingerprint"], br.key_fingerprint(old_pub))
        self.assertEqual(changed["fingerprint"], br.key_fingerprint(new_pub))
        lines = self.audit_lines()
        self.assertEqual(len(lines), 2, lines)
        f_old = br.key_fingerprint(old_pub).replace(" ", "")
        f_new = br.key_fingerprint(new_pub).replace(" ", "")
        self.assertTrue(lines[0].endswith("admin jules repin new=%s old=%s same via=configure" % (f_old, f_old)), lines[0])
        self.assertTrue(lines[1].endswith("admin jules repin new=%s old=%s changed via=configure" % (f_new, f_old)), lines[1])
        joined = "\n".join(open(self.relay.audit_path()).read().splitlines())
        self.assertNotIn(old_pub, joined)
        self.assertNotIn(new_pub, joined)

    def test_first_pin_says_first(self):
        r = self.relay
        r.audit_repin("admin", "otto", "", "0123 4567 89ab cdef", "setup")
        self.assertTrue(self.audit_lines()[-1].endswith("admin otto repin new=0123456789abcdef old=- first via=setup"))


REVIEW = """
const fpOf = k => keyFingerprint(k);
bots[0].paired = true; bots[0].public_key = %r;
el._setup = {owner: '', rows: {otto: {status: 'sent', relay: 'review', relay_fp: %r, paired_computer: 'den-mac', name: 'Otto', sent_at: 1}}};
paintBots(el, bots);
const w = L().querySelector('.repinwarn');
res.warn = w ? w.textContent : null;
"""


@unittest.skipUnless(H.CHROME, "needs Chrome")
class Page(unittest.TestCase):
    def test_accept_screen_warns_when_the_key_differs(self):
        import hashlib
        old = b64(SigningKey.generate())
        new_fp = hashlib.sha256(bytes(SigningKey.generate().verify_key)).hexdigest()[:16]
        r = H.chrome(REVIEW % (old, new_fp))
        self.assertIsNotNone(r["warn"])
        self.assertIn("different from the one paired now", r["warn"])

    def test_no_warning_for_the_same_key(self):
        import hashlib
        sk = SigningKey.generate()
        same_fp = hashlib.sha256(bytes(sk.verify_key)).hexdigest()[:16]
        r = H.chrome(REVIEW % (b64(sk), same_fp))
        self.assertIsNone(r["warn"])


class Source(unittest.TestCase):
    def test_configure_asks_before_replacing_a_different_key(self):
        js = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        start = js.index("function wirePair(")
        body = js[start:js.index("\nfunction ", start + 10)]
        self.assertIn("oldFp !== newFp", body)
        self.assertIn('action: "Replace key"', body)
        self.assertIn("r.data.changed", body)


if __name__ == "__main__":
    unittest.main()
