"""0.12.9: a bot added through the real Add bot API calls is not flagged 'Update available' (fresh or re-added).

Regression: 0.12.8 recorded the pairing release (note_setup) but finish_rows only read it when the relay had no
signed update status for the slug. The relay kept updates.json, helpers.json and refusals after a delete, so a bot
re-added with the same id inherited the old bot's status and showed 'Update available' straight away. 0.12.8's
FreshBotNotBehind tests built the records by hand and never deleted and re-added a bot through the API.

This drives the station admin API (the same calls the Add bot dialog makes) against the real relay admin routes
served on a loopback port. Nothing leaves the box. Names are made up.
"""
import asyncio, base64, json, os, sys, tempfile, threading, time, unittest, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_admin as ta  # noqa: E402  (sets the admin env, then imports grok_admin)

ga = ta.ga
URL, HOOK = "https://api2.cursor.sh/v1/hooks/abc123", "sk-test-0123456789-WXYZ"
PUB = base64.b64encode(bytes(range(1, 33))).decode()


class RelayOnLoopback:
    def __init__(self):
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        os.environ.setdefault("RELAY_TOKEN", "test-token"); os.environ.setdefault("XAI_API_KEY", "test-key")
        import relay, handoff
        from aiohttp import web
        self.r = r = relay
        self.tmp = tempfile.mkdtemp()
        names = ("WEBHOOKS_FILE", "MINTED_FILE", "PENDING_DIR", "SECRETS_DIR", "REPLIES_DIR", "PERSONAS_FILE",
                 "ADMIN_TOKEN", "UPDATES_FILE", "HELPER_FILE", "_limits", "log")
        self.snap = {n: getattr(r, n) for n in names}
        bots = os.path.join(self.tmp, "bots"); os.makedirs(bots)
        r.WEBHOOKS_FILE = os.path.join(bots, "webhooks.json")
        r.MINTED_FILE = os.path.join(self.tmp, "minted", "voice-tokens.json")
        r.PENDING_DIR = os.path.join(self.tmp, "pending"); r.REPLIES_DIR = os.path.join(self.tmp, "replies")
        r.SECRETS_DIR = os.path.join(self.tmp, "secrets"); os.makedirs(r.SECRETS_DIR)
        r.PERSONAS_FILE = os.path.join(self.tmp, "personas.json")
        json.dump({"personas": []}, open(r.PERSONAS_FILE, "w"))
        r.UPDATES_FILE = os.path.join(bots, "updates.json"); r.HELPER_FILE = os.path.join(bots, "helpers.json")
        r.ADMIN_TOKEN = b"admin-secret"
        r._limits = handoff.Limits(os.path.join(self.tmp, "limits.json"), r.today)
        r.log = lambda *a: None
        r._personas.update(mtime=None, map={}, voices=None, hosts=[]); r._hooks.update(mtime=None, map={})
        r.VOICES.update({"ara"})
        r._update_bad.clear()
        app = web.Application(); app.add_routes(r.ADMIN_ROUTES)
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.runner = web.AppRunner(app)
            self.loop.run_until_complete(self.runner.setup())
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            self.loop.run_until_complete(site.start())
            self.port = site._server.sockets[0].getsockname()[1]
            ready.set()
            self.loop.run_forever()
        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)

    def call(self, method, path, payload=None, timeout=10, raw=None, content_type=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method,
                                     headers={"Authorization": "Bearer admin-secret", "X-Client-Cert-Fp": "c" * 64,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def close(self):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        for n, v in self.snap.items():
            setattr(self.r, n, v)


class AddBotEndToEnd(unittest.TestCase):
    def setUp(self):
        self.relay = RelayOnLoopback()
        self.addCleanup(self.relay.close)
        old = ga.relay_call
        ga.relay_call = self.relay.call
        self.addCleanup(setattr, ga, "relay_call", old)
        oldv, ga.VERSION_FILE = ga.VERSION_FILE, os.path.join(ROOT, "VERSION")
        self.addCleanup(setattr, ga, "VERSION_FILE", oldv)
        self.c = ta.signed_in()

    def add_bot(self, slug, host):
        """POST /api/bots, PUT key, PUT webhook: the Add bot dialog's calls, in its order."""
        st, j = self.c.req("POST", "/api/bots", {"name": "Alpha", "slug": slug, "key_mode": "home", "voice": "ara",
                                                  "reply_host": host, "password": ta.GOOD})
        self.assertEqual(st, 201, j)
        st, j = self.c.req("PUT", "/api/bots/%s/key" % slug, {"public_key": PUB, "password": ta.GOOD})
        self.assertEqual(st, 200, j)
        st, j = self.c.req("PUT", "/api/bots/%s/webhook" % slug, {"url": URL, "key": HOOK, "password": ta.GOOD})
        self.assertEqual(st, 200, j)

    def row(self, slug):
        st, v = self.c.req("GET", "/api/finish-update")
        self.assertEqual(st, 200, v)
        rows = [r for g in v["computers"] for r in g["bots"] if r["slug"] == slug]
        self.assertEqual(len(rows), 1, v)
        return rows[0], v

    def test_fresh_add_is_not_behind(self):
        self.add_bot("alpha", "alpha.example.lan")
        r, v = self.row("alpha")
        self.assertEqual(r["state"], "done", r)
        self.assertEqual(v["behind"], 0)

    def test_readd_after_delete_is_not_behind(self):
        self.add_bot("alpha", "alpha.local")
        # The earlier bot answered an update on an older release: a signed status the relay recorded.
        rec = {"alpha": {"release": "0.12.0", "instr": "0" * 16, "helper": "0.11.7", "computer": "-",
                         "at": int(time.time()) - 3600}}
        json.dump(rec, open(self.relay.r.UPDATES_FILE, "w"))
        json.dump({"alpha": "0.11.7"}, open(self.relay.r.HELPER_FILE, "w"))
        self.relay.r._update_bad["alpha"] = int(time.time()) - 3600
        st, j = self.c.req("DELETE", "/api/bots/alpha", {"password": ta.GOOD})
        self.assertEqual(st, 200, j)
        self.add_bot("alpha", "alpha.local")
        r, v = self.row("alpha")
        self.assertEqual(r["state"], "done", r)
        self.assertEqual(v["behind"], 0)
        st, j = self.c.req("GET", "/api/bots")
        b = next(x for x in j["bots"] if x["slug"] == "alpha")
        self.assertIsNone(b["update"])
        self.assertIsNone(b["update_refused_at"])
        self.assertEqual(b["reply_host"], "alpha.local")  # stored exactly as reported

    def test_rekey_without_delete_drops_the_old_status(self):
        self.add_bot("alpha", "alpha.example.lan")
        json.dump({"alpha": {"release": "0.12.0", "instr": "0" * 16, "helper": "-", "computer": "-",
                             "at": int(time.time()) - 3600}}, open(self.relay.r.UPDATES_FILE, "w"))
        st, j = self.c.req("PUT", "/api/bots/alpha/key", {"public_key": PUB, "password": ta.GOOD})
        self.assertEqual(st, 200, j)
        r, _ = self.row("alpha")
        self.assertEqual(r["state"], "done", r)


if __name__ == "__main__":
    unittest.main()
