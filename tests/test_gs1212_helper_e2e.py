"""0.12.12: 'Helper out of date' never flashes after Add bot. End to end through the real handlers.

The owner saw 'Helper out of date: use Update' for a few seconds right after adding a home bot, three releases in a row.
0.12.11's test only built the bot record by hand. This one drives the real sequence: the admin Add bot API calls,
the automatic post-add Test (a real webhook to a loopback 'bot'), the bot's sealed reply on /voice/reply, its sealed
profile picture on /voice/avatar, and its signed update status on /voice/reply. After every step it reads the
Bots page payload (/api/bots) and renders the row notes with the page's own JS (node), and checks for the warning.

Nothing leaves the box. Names and keys are made up.
"""
import asyncio, base64, importlib.machinery, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, threading
import time, unittest, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import test_admin as ta  # noqa: E402  (sets the admin env, then imports grok_admin)
from nacl.signing import SigningKey  # noqa: E402

ga = ta.ga
_loader = importlib.machinery.SourceFileLoader("gs_reply_1212", os.path.join(ROOT, "tools", "grok-station-reply"))
_spec = importlib.util.spec_from_loader("gs_reply_1212", _loader)
helper = importlib.util.module_from_spec(_spec)
_loader.exec_module(helper)

URL, HOOK = "https://api2.cursor.sh/v1/hooks/abc123", "sk-test-0123456789-WXYZ"
WARN = "Helper out of date"
JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()


def _fn(name):
    i = JS.index("function " + name + "(")
    return JS[i:JS.index("\n}\n", i) + 3]


def render_notes(bot):
    """The Bots row's amber notes, from the page's own helperBehind and finishNotes."""
    if not shutil.which("node"):
        return None
    prog = (_fn("helperBehind") + _fn("finishNotes")
            + "const rowUpdating=()=>false, rowFailLine=()=>'', finishFind=()=>null, FINISH={view:null};"
            + "const ICO_TRI='', esc=s=>String(s);"
            + "process.stdout.write(JSON.stringify(finishNotes(%s)));" % json.dumps(bot))
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (40, 90, 160)).save(buf, "PNG")
    return buf.getvalue()


class Station:
    """The real relay (admin routes, /voice/reply, /voice/avatar) and a loopback bot webhook on one event loop."""

    def __init__(self):
        os.environ.setdefault("RELAY_TOKEN", "test-token"); os.environ.setdefault("XAI_API_KEY", "test-key")
        import relay, handoff
        from aiohttp import web
        self.r = r = relay
        self.tmp = tempfile.mkdtemp()
        names = ("WEBHOOKS_FILE", "MINTED_FILE", "PENDING_DIR", "SECRETS_DIR", "REPLIES_DIR", "PERSONAS_FILE",
                 "ADMIN_TOKEN", "UPDATES_FILE", "HELPER_FILE", "_limits", "log", "PORTRAIT_WRITE", "HANDOFF",
                 "REPLY_WAIT", "_avatar_seen", "_reply_rate")
        self.snap = {n: getattr(r, n) for n in names}
        bots = os.path.join(self.tmp, "bots"); os.makedirs(bots)
        r.WEBHOOKS_FILE = os.path.join(bots, "webhooks.json")
        r.MINTED_FILE = os.path.join(self.tmp, "minted", "voice-tokens.json")
        r.PENDING_DIR = os.path.join(self.tmp, "pending"); r.REPLIES_DIR = os.path.join(self.tmp, "replies")
        r.SECRETS_DIR = os.path.join(self.tmp, "secrets"); os.makedirs(r.SECRETS_DIR)
        r.PORTRAIT_WRITE = os.path.join(self.tmp, "portraits"); os.makedirs(r.PORTRAIT_WRITE)
        r.PERSONAS_FILE = os.path.join(self.tmp, "personas.json")
        json.dump({"personas": []}, open(r.PERSONAS_FILE, "w"))
        r.UPDATES_FILE = os.path.join(bots, "updates.json"); r.HELPER_FILE = os.path.join(bots, "helpers.json")
        r.ADMIN_TOKEN = b"admin-secret"
        r.HANDOFF = True
        r._reply_rate = None  # a fresh reply limiter per test
        # 0.12.15: helper status keeps a little process state (retry clock, degraded view); start each test clean.
        if hasattr(r, "_helper_reset_state"):
            r._helper_reset_state()
        import botreply
        r._avatar_seen = botreply.ShowSeen(os.path.join(self.tmp, "avatar-seen.json"))
        r.REPLY_WAIT = 20
        r._limits = handoff.Limits(os.path.join(self.tmp, "limits.json"), r.today)
        self.lines = []
        r.log = lambda *a: self.lines.append(" ".join(map(str, a)))
        r._personas.update(mtime=None, map={}, voices=None, hosts=[], names=[]); r._hooks.update(mtime=None, map={})
        r.VOICES.update({"ara"})
        r._update_bad.clear(); r._test_last.clear(); r._reply_seen.clear()
        r._helper_asked.clear(); r._update_asked.clear(); r._avatar_hits.clear()
        self.webhooks = []  # bodies the 'bot' received, in order

        async def hook(req):
            self.webhooks.append(await req.json())
            return web.json_response({"ok": True})

        app = web.Application()
        app.add_routes(r.ADMIN_ROUTES)
        app.add_routes([web.post("/voice/avatar", r.voice_avatar), web.post("/hook", hook)])
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

    def _open(self, req, timeout=10):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def call(self, method, path, payload=None, timeout=10, raw=None, content_type=None):
        data = json.dumps(payload).encode() if payload is not None else None
        return self._open(urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method,
            headers={"Authorization": "Bearer admin-secret", "X-Client-Cert-Fp": "c" * 64,
                     "Content-Type": "application/json"}), timeout)

    def post_voice(self, path, body, helper_version=None):
        """What gs-seal-reply sends: the sealed outer JSON, plus X-GS-Helper when the helper has a version."""
        h = {"Content-Type": "application/json"}
        if helper_version:
            h["X-GS-Helper"] = helper_version
        return self._open(urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=body,
                                                 method="POST", headers=h))

    def hook_url(self):
        return "http://127.0.0.1:%d/hook" % self.port

    def close(self):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        for n, v in self.snap.items():
            setattr(self.r, n, v)


class _Flow(unittest.TestCase):
    def setUp(self):
        self.st = Station()
        self.addCleanup(self.st.close)
        old = ga.relay_call
        ga.relay_call = self.st.call
        self.addCleanup(setattr, ga, "relay_call", old)
        oldv, ga.VERSION_FILE = ga.VERSION_FILE, os.path.join(ROOT, "VERSION")
        self.addCleanup(setattr, ga, "VERSION_FILE", oldv)
        self.c = ta.signed_in()
        self.signer = SigningKey.generate()
        self.seed = base64.b64encode(bytes(self.signer)).decode()
        self.pub = base64.b64encode(bytes(self.signer.verify_key)).decode()
        self.seen = []  # (step, bot payload, rendered notes)

    def add_bot(self, slug="ivy"):
        """The Add bot dialog's calls in its order, then the automatic post-add Test (then_avatar)."""
        c = self.c
        st, j = c.req("POST", "/api/bots", {"name": "Ivy", "slug": slug, "key_mode": "home", "voice": "ara",
                                             "reply_host": "box.example.lan", "password": ta.GOOD})
        self.assertEqual(st, 201, j)
        self.check("after create")
        st, j = c.req("PUT", "/api/bots/%s/key" % slug, {"public_key": self.pub, "password": ta.GOOD})
        self.assertEqual(st, 200, j)
        self.check("after pairing")
        st, j = c.req("PUT", "/api/bots/%s/webhook" % slug, {"url": URL, "key": HOOK, "password": ta.GOOD})
        self.assertEqual(st, 200, j)
        # The saved webhook points at the loopback 'bot' (the real URL check only allows the hosted service).
        self.assertTrue(self.st.r.write_webhooks({slug: {"url": self.st.hook_url(), "token": HOOK, "saved_at": 10}}))
        self.check("after webhook")
        st, j = c.req("POST", "/api/bots/%s/test" % slug, {"then_avatar": True})
        self.assertIn(st, (200, 202), j)
        self.tid = j["test_id"]
        self.check("test waiting")

    def wait_hook(self, kind=None, n=None):
        t0 = time.time()
        while time.time() - t0 < 10:
            hits = [w for w in self.st.webhooks if kind is None or w.get("kind") == kind]
            if hits and (n is None or len(self.st.webhooks) >= n):
                return hits[-1]
            time.sleep(0.02)
        self.fail("no webhook of kind %r: %r" % (kind, self.st.webhooks))

    def station_pub(self):
        st, j = self.st.call("GET", "/admin/bots")
        return j["station_pub"]

    def reply(self, slug, rid, text, helper_version=None):
        outer = helper.build_outer(slug, rid, text, self.seed, self.station_pub())
        return self.st.post_voice("/voice/reply", outer, helper_version)

    def ask_update(self, slug="ivy"):
        """The relay call Update makes (finish-update/send forwards exactly this)."""
        st, j = self.st.call("POST", "/admin/bots/%s/update" % slug,
                             {"actor": "admin", "release": "0.12.12", "commit": "a" * 40})
        self.assertIn(st, (200, 202), j)

    def wait_test(self, slug):
        t0 = time.time()
        while time.time() - t0 < 15:
            st, j = self.c.req("GET", "/api/bots/%s/test/%s" % (slug, self.tid))
            if j.get("state") != "waiting":
                return j
            self.check("test still waiting")
            time.sleep(0.05)
        self.fail("test did not finish")

    def bot(self, slug="ivy"):
        st, j = self.c.req("GET", "/api/bots")
        self.assertEqual(st, 200, j)
        return next(b for b in j["bots"] if b["slug"] == slug)

    def check(self, step, slug="ivy"):
        b = self.bot(slug)
        notes = render_notes(b)
        self.seen.append((step, b.get("helper"), notes))
        return b, notes

    def assert_never_warned(self):
        for step, h, notes in self.seen:
            self.assertFalse(h and h.get("out_of_date"), "%s: helper=%r" % (step, h))
            if notes is not None:
                self.assertNotIn(WARN, notes, step)

    def run_flow(self, slug="ivy", reply_version=None):
        self.add_bot(slug)
        ask = self.wait_hook(kind=None)
        self.assertEqual(ask["persona"], slug)
        st, j = self.reply(slug, ask["request_id"], "Hi, I'm here.", reply_version)
        self.assertEqual(st, 200, j)
        self.check("after test reply")
        res = self.wait_test(slug)
        self.assertEqual(res["state"], "ok", res)
        self.check("after test passed")
        self.wait_hook(kind="avatar")
        self.check("avatar asked")
        st, j = self.st.post_voice("/voice/avatar", helper.build_avatar(slug, _png(), self.seed, self.station_pub()),
                                   reply_version)
        self.assertEqual(st, 200, j)
        self.check("after avatar")


class AddBotNeverFlashesHelperWarning(_Flow):
    def test_reply_and_avatar_with_version_then_signed_status(self):
        """0.12.13: every current helper post carries its version (a headerless post is an old helper, see
        test_gs1213); the signed status then confirms."""
        self.run_flow(reply_version=ga.HELPER_RELEASE)
        b, _ = self.check("before status")
        self.assert_never_warned()
        self.ask_update()
        ask = self.wait_hook(kind="update_bot")
        line = "%s instr=%s helper=%s computer=-" % (ask["release"], "0123456789abcdef", ga.HELPER_RELEASE)
        st, j = self.reply("ivy", ask["request_id"], line)
        self.assertEqual(st, 200, j)
        b, notes = self.check("after signed status")
        self.assertEqual(b["helper"]["version"], ga.HELPER_RELEASE)
        self.assertEqual(b["helper"]["state"], "current")
        self.assert_never_warned()

    def test_current_helper_header_flow(self):
        self.run_flow(reply_version=ga.HELPER_RELEASE)
        b, _ = self.check("end")
        self.assert_never_warned()
        self.assertEqual(b["helper"]["state"], "current")

    def test_readd_after_delete_with_old_answer_on_record(self):
        """The reported case: the station still had the earlier bot.s last answer when it was re-added."""
        self.run_flow(reply_version=ga.HELPER_RELEASE)
        st, j = self.c.req("DELETE", "/api/bots/ivy", {"password": ta.GOOD})
        self.assertEqual(st, 200, j)
        self.st.r._test_last.clear()
        self.st.webhooks.clear()
        self.seen.clear()
        self.run_flow(reply_version=ga.HELPER_RELEASE)
        self.assert_never_warned()


class OldHelperStillShows(_Flow):
    def test_signed_status_with_old_helper_warns(self):
        self.run_flow(reply_version=ga.HELPER_RELEASE)
        self.ask_update()
        ask = self.wait_hook(kind="update_bot")
        st, j = self.reply("ivy", ask["request_id"], "%s instr=%s helper=0.11.6 computer=-" % (ask["release"], "0123456789abcdef"))
        self.assertEqual(st, 200, (j, self.st.lines[-5:], ask))
        b, notes = self.check("after old status")
        self.assertIs(b["helper"]["out_of_date"], True, b["helper"])
        self.assertEqual(b["helper"]["state"], "behind")
        if notes is not None:
            self.assertIn(WARN, notes)


if __name__ == "__main__":
    unittest.main()
