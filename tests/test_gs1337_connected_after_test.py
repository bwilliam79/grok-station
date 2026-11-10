#!/usr/bin/env python3
"""0.13.37 regression: a Set up row shows Connected only after its Test passes.

At 0.13.36 saveSetupWebhook started the Test and called /api/bots/setup-connected right away, so the row said
Connected while the Test was still waiting, and stayed Connected when it then failed. Now:
  * the admin route needs the test_id, and the relay's record for that slug must say "ok" (409 otherwise);
  * the page waits for the Test (up to TEST_LIMIT_S), shows a Waiting state, and only then posts setup-connected;
  * the paste-values path waits the same limit, not a private 30 s clock.
"""
import json, os, re, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_admin as ta  # noqa: E402  (sets the admin env, then imports grok_admin)
import gs1336_repaint_chrome as H  # noqa: E402

ga = ta.ga
TID = "t_abcdefgh12345678"


class _Fake:
    def __init__(self):
        self.sent = []

    def send(self, code, body):
        self.sent.append((code, body))

    def bots_unconfigured(self):
        self.sent.append((503, {"error": "unconfigured"}))

    def _setup_ctx(self):
        return {"known": {"otto"}, "configured": {"otto"}, "names": {"otto": "Otto"}}


class Route(unittest.TestCase):
    def setUp(self):
        ta.reset_state()
        self.saved = ga.relay_call
        self.asked = []

    def tearDown(self):
        ga.relay_call = self.saved

    def call(self, body, state):
        def fake(method, path, body=None, **kw):
            self.asked.append((method, path))
            return 200, {"state": state}
        ga.relay_call = fake
        h = _Fake()
        ga.Handler.bots_setup_connected(h, body)
        return h.sent[-1]

    def test_no_test_id_is_refused(self):
        code, body = self.call({"slug": "otto"}, "ok")
        self.assertEqual(code, 409)
        self.assertEqual(body["error"], ga.SETUP_NOT_TESTED)
        self.assertEqual(self.asked, [])
        self.assertNotEqual(ga.setup_load().get("rows", {}).get("otto", {}).get("status"), "connected")

    def test_a_waiting_or_failed_test_is_refused(self):
        for state in ("waiting", "failed", "cancelled"):
            code, body = self.call({"slug": "otto", "test_id": TID}, state)
            self.assertEqual(code, 409, state)
        self.assertEqual(self.asked[-1], ("GET", "/admin/bots/otto/test/" + TID))
        self.assertNotEqual(ga.setup_load().get("rows", {}).get("otto", {}).get("status"), "connected")

    def test_a_passed_test_connects(self):
        code, body = self.call({"slug": "otto", "test_id": TID}, "ok")
        self.assertEqual(code, 200, body)
        self.assertEqual(ga.setup_load()["rows"]["otto"]["status"], "connected")

    def test_a_bad_test_id_never_reaches_the_relay(self):
        code, _ = self.call({"slug": "otto", "test_id": "../../x"}, "ok")
        self.assertEqual(code, 409)
        self.assertEqual(self.asked, [])


SAVE = """
hook('url').value = 'https://api2.cursor.sh/hooks/abc123'; hook('key').value = 'pasted-webhook-value';
window.testState = 'waiting';
window.answer = (url, opt) => {
  if (url.indexOf('/webhook') >= 0) return [200, {}];
  if (/\\/test\\/t_/.test(url)) return [200, {state: window.testState, error: window.testState === 'failed' ? 'No answer' : null, limit_s: 180}];
  if (url.indexOf('/test') >= 0) return [202, {test_id: 't_abcdefgh12345678'}];
  if (url.indexOf('setup-connected') >= 0) return [200, {setup: {owner: '', rows: {otto: {status: 'connected'}}}}];
  if (url.indexOf('/api/bots') >= 0 && (opt.method || 'GET') === 'GET') return [200, {bots: bots, setup: el._setup}];
  return [200, {}];
};
L().querySelector('[data-act="savehook"]').click();
await tick(2500);
res.connectedEarly = window.calls.filter(c => c.url.indexOf('setup-connected') >= 0).length;
res.waitingText = L().textContent.indexOf('Testing') >= 0 && L().textContent.indexOf('waiting for Otto') >= 0;
res.waitingPanel = !!L().querySelector('.setpanel[data-slug="otto"] .twait');
res.limitShown = L().textContent.indexOf('up to 3:00') >= 0;
res.saidConnected = /Connected(?! only)/.test(L().textContent);
window.testState = %r;
await tick(2500);
const cc = window.calls.filter(c => c.url.indexOf('setup-connected') >= 0);
res.connectedAfter = cc.length;
res.connectedBody = cc.length ? JSON.parse(cc[0].body) : null;
"""


@unittest.skipUnless(H.CHROME, "needs Chrome")
class Page(unittest.TestCase):
    def test_waiting_then_connected_only_after_ok(self):
        r = H.chrome(SAVE % "ok", budget=12000)
        self.assertEqual(r["connectedEarly"], 0)
        self.assertTrue(r["waitingText"])
        self.assertTrue(r["waitingPanel"])
        self.assertTrue(r["limitShown"])
        self.assertFalse(r["saidConnected"])
        self.assertEqual(r["connectedAfter"], 1)
        self.assertEqual(r["connectedBody"], {"slug": "otto", "test_id": "t_abcdefgh12345678"})

    def test_a_failed_test_never_connects(self):
        r = H.chrome(SAVE % "failed", budget=12000)
        self.assertEqual((r["connectedEarly"], r["connectedAfter"]), (0, 0))


class Source(unittest.TestCase):
    def test_paste_path_waits_the_shared_limit(self):
        js = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        self.assertNotIn("Date.now() - t0 < 30000", js)
        self.assertEqual(len(re.findall(r'"/api/bots/setup-connected", \{slug: (?:row\.)?slug, test_id: tid\}', js)), 2)


if __name__ == "__main__":
    unittest.main()
