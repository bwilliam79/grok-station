"""0.13.30: Rescan bots. The Bots page runs the same list ask as the voice import, and returns counts only.

Fails on 0.13.29: there is no relay or admin rescan route, and the menu has no Rescan bots item.
"""
import asyncio
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from nacl.signing import SigningKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import handoff  # noqa: E402

_loader = importlib.machinery.SourceFileLoader(
    "grok_station_reply_rescan", os.path.join(ROOT, "tests", "fixtures", "grok-station-reply"))
_spec = importlib.util.spec_from_loader("grok_station_reply_rescan", _loader)
helper = importlib.util.module_from_spec(_spec)
_loader.exec_module(helper)

JS = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "admin", "web", "admin.css"), encoding="utf-8").read()
RELAY_SRC = open(os.path.join(ROOT, "relay", "relay.py"), encoding="utf-8").read()
ADMIN_SRC = open(os.path.join(ROOT, "admin", "grok_admin.py"), encoding="utf-8").read()


def _b64(raw):
    import base64
    return base64.b64encode(raw).decode()


def _fn(src, name):
    mark = "\nfunction " + name + "("
    start = src.find(mark)
    if start < 0:
        mark = "\nasync function " + name + "("
        start = src.find(mark)
    if start < 0:
        raise AssertionError("missing " + name)
    nxt = src.find("\nfunction ", start + 1)
    nxt2 = src.find("\nasync function ", start + 1)
    ends = [i for i in (nxt, nxt2) if i > start]
    return src[start:min(ends) if ends else None]


class RelayRescan(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import relay
        self.relay = relay
        r = relay
        self.tmp = tempfile.mkdtemp()
        self.snap = {
            "WEBHOOKS_FILE": r.WEBHOOKS_FILE, "MINTED_FILE": r.MINTED_FILE, "PENDING_DIR": r.PENDING_DIR,
            "SECRETS_DIR": r.SECRETS_DIR, "REPLIES_DIR": r.REPLIES_DIR, "PERSONAS_FILE": r.PERSONAS_FILE,
            "REPLY_WAIT": r.REPLY_WAIT, "ADMIN_TOKEN": r.ADMIN_TOKEN, "HANDOFF": r.HANDOFF, "_limits": r._limits,
            "log": r.log, "voices": set(r.VOICES),
            "personas": {k: (dict(v) if isinstance(v, dict) else v) for k, v in r._personas.items()},
            "hooks": {"mtime": r._hooks.get("mtime"), "map": dict(r._hooks.get("map") or {})},
            "seen": set(r._reply_seen), "busy": getattr(r, "_rescan_busy", False),
        }
        r.SECRETS_DIR = os.path.join(self.tmp, "secrets")
        os.makedirs(r.SECRETS_DIR, exist_ok=True)
        r._reply_seen.clear()
        r._reply_rate = None
        r._rtt = None
        r._rtt_dir = None
        r.WEBHOOKS_FILE = os.path.join(self.tmp, "webhooks.json")
        r.MINTED_FILE = os.path.join(self.tmp, "minted", "voice-tokens.json")
        r.PENDING_DIR = os.path.join(self.tmp, "pending")
        r.REPLIES_DIR = os.path.join(self.tmp, "replies")
        r.PERSONAS_FILE = os.path.join(self.tmp, "personas.json")
        r.ADMIN_TOKEN = b"admin-secret"
        r.HANDOFF = True
        r.REPLY_WAIT = 2
        r.VOICES.clear()
        r.VOICES.update({"ara", "rex"})
        r._personas.update(mtime=None, map={}, voices=None, hosts=[], names=[])
        r._hooks.update(mtime=None, map={})
        r._limits = handoff.Limits(os.path.join(self.tmp, "limits.json"), r.today)
        r._rescan_busy = False
        self.lines = []
        r.log = lambda *a: self.lines.append(" ".join(map(str, a)))
        with open(r.PERSONAS_FILE, "w") as f:
            json.dump({"personas": [
                {"slug": "sol", "name": "Sol", "voice": "ara", "prompt": "Helper.", "aliases": [], "facts": []},
            ]}, f)
        self.signer = SigningKey.generate()
        self.token = "hook-" + ("z" * 8)
        app = web.Application()
        app.add_routes(r.ADMIN_ROUTES)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        if getattr(self, "client", None) is not None:
            await self.client.close()
        r, s = self.relay, self.snap
        r.WEBHOOKS_FILE, r.MINTED_FILE = s["WEBHOOKS_FILE"], s["MINTED_FILE"]
        r.SECRETS_DIR = s["SECRETS_DIR"]
        r.PENDING_DIR, r.REPLIES_DIR = s["PENDING_DIR"], s["REPLIES_DIR"]
        r.PERSONAS_FILE, r.REPLY_WAIT = s["PERSONAS_FILE"], s["REPLY_WAIT"]
        r.ADMIN_TOKEN, r.HANDOFF, r._limits, r.log = s["ADMIN_TOKEN"], s["HANDOFF"], s["_limits"], s["log"]
        r.VOICES.clear()
        r.VOICES.update(s["voices"])
        r._personas.clear()
        r._personas.update(s["personas"])
        r._hooks.clear()
        r._hooks.update(s["hooks"])
        r._reply_seen.clear()
        r._reply_seen.update(s["seen"])
        r._rescan_busy = s["busy"]

    def headers(self):
        return {"Authorization": "Bearer admin-secret"}

    async def _ready(self, url):
        r, c = self.relay, self.client
        pub = _b64(bytes(self.signer.verify_key))
        res = await c.put("/admin/bots/sol/key", headers=self.headers(),
                          json={"public_key": pub, "actor": "admin", "expect_old_fp": "-"})
        self.assertEqual(res.status, 200, await res.text())
        self.assertTrue(r.write_webhooks({"sol": {"url": url, "token": self.token, "saved_at": 10}}))
        saved = await c.put("/admin/bots/list-source", headers=self.headers(),
                            json={"list_source": "sol", "actor": "admin"})
        self.assertEqual(saved.status, 200, await saved.text())
        listed = await (await c.get("/admin/bots", headers=self.headers())).json()
        return listed["station_pub"]

    def _open_age(self):
        """Drop the 60 second gap without dropping an open ask."""
        path = self.relay._limits.path
        doc = json.load(open(path))
        doc["list_ask"]["last"] = time.time() - 120
        doc["list_ask"]["out"] = ""
        json.dump(doc, open(path, "w"))

    async def _post(self):
        res = await self.client.post("/admin/bots/rescan", headers=self.headers(), json={"actor": "admin"})
        body = await res.json()
        return res.status, body

    async def test_adds_unknown_names_only_and_counts(self):
        c, r = self.client, self.relay
        seen = {"n": 0}

        async def hook(req):
            body = await req.json()
            seen["n"] += 1
            seen["q"] = body.get("question")
            seen["kind"] = body.get("kind")
            seed = _b64(bytes(self.signer))
            names = ["Kai", "Sol", "Mira"] if seen["n"] == 1 else ["Kai", "Sol", "Mira"]
            outer = helper.build_list_outer(body["persona"], body["request_id"], names, seed, station_pub)
            async with c.post("/voice/reply", data=outer, headers={"Content-Type": "application/json"}) as resp:
                seen["status"] = resp.status
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/hook", hook)
        srv = TestServer(app)
        await srv.start_server()
        try:
            station_pub = await self._ready(str(srv.make_url("/hook")))
            status, body = await self._post()
            self.assertEqual(status, 200, body)
            self.assertEqual(set(body), {"ok", "added", "known"})
            self.assertEqual(body, {"ok": True, "added": 2, "known": 1})
            self.assertEqual(seen["q"], "list bots")
            self.assertEqual(seen["kind"], "bot_list")
            self.assertEqual(seen["status"], 200)
            view = await (await c.get("/admin/bots/imported", headers=self.headers())).json()
            rows = {row["name"]: row["state"] for row in view["imported"]["rows"]}
            self.assertEqual(rows["Kai"], "new")
            self.assertEqual(rows["Mira"], "new")
            self.assertEqual(rows.get("Sol"), "configured")
            blob = "\n".join(self.lines)
            for secret in ("Kai", "Mira", self.token, str(srv.make_url("/hook"))):
                self.assertNotIn(secret, blob)
                self.assertNotIn(secret, json.dumps(body))
            self._open_age()
            status, again = await self._post()
            self.assertEqual(status, 200, again)
            self.assertEqual(again, {"ok": True, "added": 0, "known": 3})
            self.assertNotIn("Kai", json.dumps(again))
        finally:
            await srv.close()

    async def test_each_error_and_busy_and_rate_limit(self):
        c, r = self.relay, self.relay
        status, body = await self._post()
        self.assertEqual(body.get("reason"), "no-source")
        self.assertNotIn("name", body)
        self.assertEqual(set(body), {"error", "reason"})

        started, release = asyncio.Event(), asyncio.Event()

        async def slow(req):
            await req.json()
            started.set()
            await release.wait()
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/hook", slow)
        srv = TestServer(app)
        await srv.start_server()
        try:
            await self._ready(str(srv.make_url("/hook")))
            real_route = handoff.route
            calls = {"n": 0}

            def once(*a, **k):
                calls["n"] += 1
                if calls["n"] >= 2:
                    return None
                return real_route(*a, **k)

            handoff.route = once
            try:
                r.REPLY_WAIT = 0.2
                status, body = await self._post()
            finally:
                handoff.route = real_route
            self.assertEqual(body.get("reason"), "no-webhook")
            self.assertEqual(set(body), {"error", "reason"})

            self._open_age()
            r.REPLY_WAIT = 2
            first = asyncio.create_task(self._post())
            await asyncio.wait_for(started.wait(), 2)
            status, busy = await self._post()
            self.assertEqual(busy.get("reason"), "busy")
            self.assertEqual(set(busy), {"error", "reason"})
            release.set()
            r.REPLY_WAIT = 0.2
            status, timed = await first
            self.assertEqual(timed.get("reason"), "timeout")

            status, limited = await self._post()
            self.assertEqual(limited.get("reason"), "rate-limited")

            self._open_age()
            dead = "http://127.0.0.1:9/hook"
            self.assertTrue(r.write_webhooks({"sol": {"url": dead, "token": self.token, "saved_at": 10}}))
            r._hooks.update(mtime=None, map={})
            status, down = await self._post()
            self.assertEqual(down.get("reason"), "unavailable")
            self.assertNotIn(dead, json.dumps(down))
            self.assertNotIn(self.token, json.dumps(down))
        finally:
            release.set()
            await srv.close()

    async def test_voice_import_path_unchanged(self):
        i = RELAY_SRC.index("if import_team_request(heard):")
        window = RELAY_SRC[i:i + 1400]
        self.assertIn("await ask_bot_list(http)", window)
        self.assertIn("voice_list_line", window)
        self.assertNotIn("rescan", window)
        self.assertTrue(self.relay.import_team_request("Hey Grok, update my team list."))
        self.assertTrue(self.relay.import_team_request("Hey Grok, import the rest of my team."))
        line = self.relay.voice_list_line({}, "Sol", {"n": 2, "new": 1, "missing": 0, "dropped": 0}, "ok")
        self.assertIn("1 new", line)
        self.assertNotIn("Kai", line)

    async def test_open_list_is_busy(self):
        r = self.relay
        rid = "vs_" + "c" * 22
        os.makedirs(r.PENDING_DIR, exist_ok=True)
        handoff.write_pending(r.PENDING_DIR, rid, "sol", "n" * 16, int(time.time() * 1000) + 60000,
                              question="list bots", reply_by="2099-01-01T00:00:00Z", kind="bot_list")
        self.assertIsNone(r._limits.take_list(rid, now=time.time() - 120))
        status, body = await self._post()
        self.assertEqual(body.get("reason"), "busy")


class AdminRescan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import test_admin
        cls.ta = test_admin
        cls.ga = test_admin.ga

    def setUp(self):
        self.c = self.ta.signed_in()
        self._rc = self.ga.relay_call

    def tearDown(self):
        self.ga.relay_call = self._rc

    def test_auth_csrf_and_lan(self):
        self.assertEqual(self.ta.C().req("POST", "/api/bots/rescan", {})[0], 401)
        self.assertEqual(self.c.req("POST", "/api/bots/rescan", {}, csrf=False)[0], 403)
        h = self.ga.Handler.__new__(self.ga.Handler)
        h.client_address = ("203.0.113.5", 9)
        h.headers = {"X-GS-Admin": "1", "Content-Type": "application/json"}
        h.command, h.path = "POST", "/api/bots/rescan"
        h.sent = []
        h.send = lambda code, obj=None, **k: h.sent.append((code, obj))
        old = self.ga.home_subnets
        self.ga.home_subnets = lambda *a, **k: [__import__("ipaddress").ip_network("127.0.0.0/8")]
        try:
            h.route("POST")
        finally:
            self.ga.home_subnets = old
        self.assertEqual(h.sent[0][0], 403)
        self.assertEqual(h.sent[0][1].get("error"), "LAN only")

    def test_no_password_and_no_body_echo(self):
        seen = {}

        def fake(method, path, payload=None, timeout=10, **k):
            seen["timeout"] = timeout
            seen["payload"] = payload
            return 200, {"ok": True, "added": 1, "known": 2, "name": "Piper",
                         "url": "https://hooks.example/hook"}

        self.ga.relay_call = fake
        st, j = self.c.req("POST", "/api/bots/rescan", {"name": "Piper", "url": "https://hooks.example/hook"})
        self.assertEqual(st, 200, j)
        self.assertEqual(j, {"ok": True, "added": 1, "known": 2})
        self.assertNotIn("password", seen["payload"])
        self.assertEqual(seen["payload"], {"actor": "admin"})
        self.assertGreaterEqual(seen["timeout"], 190)
        self.assertNotIn("Piper", json.dumps(j))
        self.assertNotIn("hooks.example", json.dumps(j))

        def bad(method, path, payload=None, timeout=10, **k):
            return 504, {"error": "Piper did not answer", "reason": "timeout", "url": "https://hooks.example/x"}

        self.ga.relay_call = bad
        st, j = self.c.req("POST", "/api/bots/rescan", {})
        self.assertEqual(st, 504)
        self.assertEqual(j, {"error": "Could not rescan bots.", "reason": "timeout"})

        def weird(method, path, payload=None, timeout=10, **k):
            return 500, {"error": "boom", "reason": "weird"}

        self.ga.relay_call = weird
        st, j = self.c.req("POST", "/api/bots/rescan", {})
        self.assertEqual(j.get("reason"), "unavailable")
        self.assertNotIn("boom", json.dumps(j))

    def test_socket_wait_clears_the_60s_idle_cap(self):
        calls = []

        class Sock:
            def gettimeout(self):
                return 60

            def settimeout(self, value):
                calls.append(value)

        h = self.ga.Handler.__new__(self.ga.Handler)
        h.connection = Sock()
        h.sent = []
        h.send = lambda code, obj=None, **k: h.sent.append((code, obj))
        self.ga.relay_call = lambda *a, **k: (200, {"ok": True, "added": 0, "known": 0})
        try:
            h.bots_rescan()
        finally:
            self.ga.relay_call = self._rc
        self.assertGreater(calls[0], 60)
        self.assertEqual(calls[-1], 60)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class RescanUi(unittest.TestCase):
    def test_menu_order_and_copy(self):
        html = JS[JS.index("function botsHeaderShell("):JS.index("function botsCalloutHTML(")]
        self.assertLess(html.index("Refresh avatars"), html.index("Rescan bots"))
        self.assertLess(html.index("Rescan bots"), html.index('class="sep"'))
        self.assertLess(html.index('class="sep"'), html.index("Revoke all bots"))
        self.assertIn("Set up your main bot first.", html)
        self.assertIn('id="rescanhint"', html)
        copy = _fn(JS, "rescanCopy")
        self.assertIn("Asking ", copy)
        self.assertIn("for its team list.", copy)
        self.assertIn("Still waiting for ", copy)
        self.assertIn("This can take up to 3 minutes.", copy)
        self.assertIn("as Not set up. Press Set up on each.", copy)
        self.assertIn("No new bots. Your team list is up to date.", copy)
        self.assertIn("did not answer in 3 minutes.", copy)
        self.assertIn("Could not rescan bots. Try again.", copy)
        for name in ("rescanShow", "rescanStart", "rescanSyncItem", "rescanGlyph", "rescanMenuGlyph", "rescanDismiss"):
            body = _fn(JS, name)
            for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
                self.assertNotIn(bad, body, name)
        start = _fn(JS, "rescanStart")
        self.assertIn('"/api/bots/rescan"', start)
        self.assertIn("30000", start)
        self.assertIn("scrollIntoView", start)
        self.assertNotIn(".focus(", start)
        self.assertIn('aria-busy', _fn(JS, "rescanMarkBusy"))
        self.assertIn("aria-describedby", _fn(JS, "rescanSyncItem"))
        self.assertIn("aria-disabled", _fn(JS, "rescanSyncItem"))
        self.assertIn(".botscan{", CSS)
        self.assertIn("margin:0 0 12px", CSS)
        self.assertIn("#botmore[aria-busy=true]::after", CSS)
        self.assertIn("width:6px", CSS[CSS.index("#botmore[aria-busy=true]"):CSS.index("#botmore[aria-busy=true]") + 400])
        self.assertIn("opacity:.55", CSS[CSS.index("#rescan.is-off"):CSS.index("#rescan.is-off") + 80])
        show = _fn(JS, "rescanShow")
        self.assertIn('setAttribute("role", "status")', show)
        self.assertIn('setAttribute("aria-live", "polite")', show)
        self.assertIn("textContent", _fn(JS, "rescanShow"))
        # scrollIntoView on the first new row does not move focus.
        self.assertIn("scrollIntoView", start)
        self.assertNotIn("focus(", start)

    def test_status_row_sits_above_the_card(self):
        show = _fn(JS, "rescanShow")
        self.assertIn('closest(".botsheet")', show)
        self.assertIn("insertBefore(row, card)", show)
        self.assertNotIn("insertBefore(row, list)", show)

    def test_status_row_has_no_fill(self):
        i = CSS.index(".botscan{")
        rule = CSS[i:CSS.index("}", i)]
        self.assertNotIn("background", rule)

    def test_status_row_padding_and_dismiss_hit_area(self):
        i = CSS.index(".botscan{")
        rule = CSS[i:CSS.index("}", i)]
        self.assertIn("padding:8px 0", rule)
        self.assertIn("align-items:center", rule)
        j = CSS.index(".botscan .botscanx{")
        desk = CSS[j:CSS.index("}", j)]
        self.assertIn("min-height:24px", desk)
        k = CSS.index(".botscan .botscanx{", j + 1)
        phone = CSS[k:CSS.index("}", k)]
        self.assertIn("min-height:44px", phone)
        self.assertIn("align-items:center", phone)
        self.assertIn("flex-basis:100%", phone)

    def test_copy_and_disabled_state_under_node(self):
        names = ("rescanMain", "rescanCopy")
        src = "\n".join(_fn(JS, n) for n in names)
        prog = src + """
const out = {
  off: rescanMain([{slug:"sol", name:"Sol", main:false, paired:true, webhook:{configured:true}}]),
  unpaired: rescanMain([{slug:"sol", name:"Sol", main:true, paired:false, webhook:{configured:true}}]),
  on: rescanMain([{slug:"sol", name:"Sol", main:true, paired:true, webhook:{configured:true}}]),
  run: rescanCopy("Sol", "run", 0),
  wait: rescanCopy("Sol", "wait", 0),
  one: rescanCopy("Sol", "added", 1),
  two: rescanCopy("Sol", "added", 2),
  none: rescanCopy("Sol", "none", 0),
  late: rescanCopy("Sol", "timeout", 0),
  fail: rescanCopy("Sol", "fail", 0)
};
process.stdout.write(JSON.stringify(out));
"""
        got = json.loads(subprocess.run(["node", "-e", prog], capture_output=True, text=True, check=True).stdout)
        self.assertIsNone(got["off"])
        self.assertIsNone(got["unpaired"])
        self.assertEqual(got["on"]["name"], "Sol")
        self.assertEqual(got["run"], "Asking Sol for its team list.")
        self.assertEqual(got["wait"], "Still waiting for Sol. This can take up to 3 minutes.")
        self.assertEqual(got["one"], "Added 1 bot as Not set up. Press Set up on each.")
        self.assertEqual(got["two"], "Added 2 bots as Not set up. Press Set up on each.")
        self.assertEqual(got["none"], "No new bots. Your team list is up to date.")
        self.assertIn("Sol did not answer in 3 minutes.", got["late"])
        self.assertEqual(got["fail"], "Could not rescan bots. Try again.")


if __name__ == "__main__":
    unittest.main()
