"""Homebridge admin API. Real handler, fake config-ui-x on 127.0.0.1.

The helper is stubbed here (grok_admin may already be imported, so ADMIN_HELPER is not
how the stub is installed). The real helper is exercised in test_gs131_homebridge_helper.
"""
import hashlib, http.client, json, os, shutil, stat, sys, tempfile, threading, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "admin"))
if "grok_admin" not in sys.modules:
    _own = tempfile.mkdtemp(prefix="gs131-hb-")
    os.environ.setdefault("ADMIN_STATE_DIR", os.path.join(_own, "state"))
    os.environ.setdefault("ADMIN_RUN_DIR", os.path.join(_own, "run"))
    os.environ.setdefault("ADMIN_PERSONAS", os.path.join(_own, "personas.json"))
    os.environ.setdefault("ADMIN_RELAY_ENV", os.path.join(_own, "missing-admin-relay.env"))
    os.environ.setdefault("ADMIN_WAKE_CTL", "http://127.0.0.1:9")
    os.makedirs(os.environ["ADMIN_STATE_DIR"], exist_ok=True)
    os.makedirs(os.environ["ADMIN_RUN_DIR"], exist_ok=True)

import grok_admin as ga  # noqa: E402
import gs_integrations as gsi  # noqa: E402
from fakes.fake_homebridge import FakeHomebridge  # noqa: E402

GOOD = "kitchen table lamp 42"
SECRET = "hb-secret-pass"
MARKER = "TESTHBTOKEN"
ADMIN_MSG = ("This login is an admin. Grok Station won't store an admin login. "
             "Turn Admin off for this user in Homebridge, or make a new user without Admin, then try again.")
UNCONFIRMED = "Couldn't confirm this login isn't an admin"


class Client:
    def __init__(self, port):
        self.port = port
        self.jar = {}
        self.raws = []

    def req(self, method, path, body=None, csrf=True, ctype="application/json", data=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if self.jar:
            headers["Cookie"] = "; ".join("%s=%s" % kv for kv in self.jar.items())
        payload = data if data is not None else (json.dumps(body if body is not None else {}).encode() if method != "GET" else None)
        if method != "GET":
            headers["Content-Type"] = ctype
            if csrf:
                headers["X-GS-Admin"] = "1"
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        raw = resp.read()
        self.raws.append(raw)
        for key, value in resp.getheaders():
            if key.lower() == "set-cookie":
                name, val = value.split(";", 1)[0].split("=", 1)
                if val:
                    self.jar[name] = val
                else:
                    self.jar.pop(name, None)
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        return resp.status, parsed


def _admin_server():
    httpd = ga.ThreadingHTTPServer(("127.0.0.1", 0), ga.Handler)
    httpd.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _reset():
    for directory in (ga.STATE_DIR, ga.RUN_DIR):
        os.makedirs(directory, exist_ok=True)
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
    ga.AUTH.__init__()
    ga.AUTH.fails = []


def _signed_in(port):
    _reset()
    client = Client(port)
    ga.AUTH.publish()
    code = json.load(open(os.path.join(ga.RUN_DIR, "setup.json")))["code"]
    assert client.req("POST", "/api/setup/code", {"code": code})[0] == 200
    assert client.req("POST", "/api/setup/password", {"password": GOOD})[0] == 200
    client.raws.clear()
    return client


ADMIN = None
HB = None


def setUpModule():
    global ADMIN, HB
    HB = FakeHomebridge()
    HB.start_thread()
    ADMIN = _admin_server()


def tearDownModule():
    ADMIN.shutdown()
    HB.close_thread()


class HomebridgeAPI(unittest.TestCase):
    def setUp(self):
        HB.reset()
        self.tmp = tempfile.mkdtemp(prefix="gs131-hb-")
        self.creds = os.path.join(self.tmp, "secrets", "homebridge.json")
        self.block = os.path.join(self.tmp, "status", "homebridge.json")
        self.reg = os.path.join(ga.STATE_DIR, "integrations.json")
        self._env = {k: os.environ.get(k) for k in (
            "ADMIN_ALLOW_LOOPBACK_FOR_TESTS", "ADMIN_INTEGRATIONS", "HOMEBRIDGE_BLOCK", "GS_HOMEBRIDGE_CREDS")}
        os.environ["ADMIN_ALLOW_LOOPBACK_FOR_TESTS"] = "1"
        os.environ["ADMIN_INTEGRATIONS"] = self.reg
        os.environ["HOMEBRIDGE_BLOCK"] = self.block
        os.environ["GS_HOMEBRIDGE_CREDS"] = self.creds
        self._resolve = gsi.resolve_host
        self._own = gsi.station_addresses
        self._timeout = gsi.FETCH_TIMEOUT
        self._helper = ga.helper
        self._personas = ga.PERSONAS
        gsi.station_addresses = lambda: ["10.0.0.8"]
        gsi.resolve_host = lambda host: ["127.0.0.1"]
        ga.PERSONAS = os.path.join(self.tmp, "personas.json")
        self.calls = []
        ga.helper = self._helper_stub
        self.c = _signed_in(ADMIN.port)
        self._close_grace = False

    def tearDown(self):
        ga.helper = self._helper
        ga.PERSONAS = self._personas
        gsi.resolve_host = self._resolve
        gsi.station_addresses = self._own
        gsi.FETCH_TIMEOUT = self._timeout
        if self._close_grace:
            try:
                del ga.AUTH.stepup_open
            except AttributeError:
                pass
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)
        ga.AUTH.fails = []

    def _helper_stub(self, *args, stdin=None, timeout=30):
        cmd = args[0] if args else ""
        self.calls.append((cmd, stdin))
        if cmd == "homebridge-set":
            os.makedirs(os.path.dirname(self.creds), exist_ok=True)
            fd = os.open(self.creds, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"username": stdin["username"], "password": stdin["password"]}, fh)
            os.chmod(self.creds, 0o600)
            return True, {"ok": True}
        if cmd == "homebridge-get":
            try:
                doc = json.load(open(self.creds, encoding="utf-8"))
            except (OSError, ValueError):
                return False, {"error": "missing"}
            return True, {"username": doc.get("username"), "password": doc.get("password")}
        if cmd == "homebridge-delete":
            try:
                os.unlink(self.creds)
            except FileNotFoundError:
                pass
            return True, {"ok": True}
        return False, {"error": "unknown"}

    def _base(self):
        return HB.base

    def _prints(self, fn):
        import builtins
        lines = []
        real = builtins.print
        def grab(*args, **kwargs):
            lines.append(" ".join(str(a) for a in args))
            real(*args, **kwargs)
        builtins.print = grab
        try:
            out = fn()
        finally:
            builtins.print = real
        return out, lines

    def _no_secret(self, blob):
        text = blob if isinstance(blob, str) else json.dumps(blob)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(MARKER, text)
        self.assertNotIn("access_token", text)

    def _grace_off(self):
        self._close_grace = True
        ga.AUTH.stepup_open = lambda token: False

    def _verify(self, stepup=GOOD, **extra):
        body = {"base": self._base(), "username": "grok-station", "password": SECRET}
        if stepup is not None:
            body["stepup"] = stepup
        body.update(extra)
        return self.c.req("POST", "/api/integrations/homebridge/verify", body)

    def test_check_verify_pick_save_test_and_secrets_stay_out(self):
        self._grace_off()
        status, body = self.c.req("POST", "/api/integrations/homebridge/check-address", {"base": self._base() + "/"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["base"], self._base())
        self.assertEqual(body["users"], self._base() + "/users")
        self.assertEqual(HB.requests[0]["path"], "/api/auth/settings")
        self.assertEqual(HB.requests[0]["host"], "127.0.0.1:%d" % HB.port)

        def go():
            return self._verify(stepup=None)
        (status, body), lines = self._prints(go)
        self.assertEqual((status, body["error"]), (403, ga.PASSWORD_BAD), body)
        self.assertFalse(os.path.exists(self.creds))
        self.assertFalse(any(cmd == "homebridge-set" for cmd, _ in self.calls))
        self._no_secret(body)
        self._no_secret("\n".join(lines))

        HB.requests = []
        (status, body), lines = self._prints(lambda: self._verify())
        self.assertEqual(status, 200, body)
        self.assertEqual(body["checks"], [
            {"id": "reach", "ok": True}, {"id": "login", "ok": True}, {"id": "admin", "ok": True}])
        self.assertEqual(stat.S_IMODE(os.stat(self.creds).st_mode), 0o600)
        stored = json.load(open(self.creds, encoding="utf-8"))
        self.assertEqual(set(stored), {"username", "password"})
        self.assertEqual(stored["password"], SECRET)
        reg = json.load(open(self.reg, encoding="utf-8"))
        self.assertEqual(reg["integrations"]["homebridge"]["base"], self._base())
        self.assertNotIn("voice", reg["integrations"]["homebridge"])
        self.assertNotIn("username", json.dumps(reg))
        self._no_secret(body)
        self._no_secret("\n".join(lines))
        self._no_secret(json.dumps(reg))
        self.assertFalse(os.path.exists(self.block))

        status, acc = self.c.req("GET", "/api/integrations/homebridge/accessories")
        self.assertEqual(status, 200, acc)
        kinds = {row["type"] for row in acc["accessories"]}
        self.assertEqual(kinds, {"light", "switch", "outlet", "fan", "tv"})
        uids = {row["uid"] for row in acc["accessories"]}
        for banned in ("cam-1", "bell-1", "mot-1", "sec-1", "lock-1", "gar-1", "therm-1"):
            self.assertNotIn(banned, uids)
        self.assertNotIn("No Id", json.dumps(acc))
        by_uid = {row["uid"]: row for row in acc["accessories"]}
        self.assertEqual(by_uid["fan-1"]["char"], "Active")
        self.assertEqual(by_uid["fan-2"]["char"], "On")  # Fanv2 prefers Active, then On
        self.assertEqual(by_uid["tv-1"]["char"], "Active")
        self.assertEqual(set(by_uid["light-1"]), {"uid", "name", "type", "char"})
        self._no_secret(acc)

        # The Homebridge password is not the admin password, and a client-supplied char is ignored.
        self.assertEqual(self.c.req("POST", "/api/integrations/homebridge/devices", {
            "devices": [{"uid": "light-1", "name": "Porch Light", "aliases": [], "char": "Active"}],
            "password": SECRET})[1]["error"], "bad_request")
        self._grace_off()
        status, saved = self.c.req("POST", "/api/integrations/homebridge/devices", {
            "devices": [
                {"uid": "light-1", "name": "Porch Light", "aliases": ["porch"]},
                {"uid": "fan-1", "name": "Garage Fan", "aliases": []},
                {"uid": "a+b", "name": "Plus Light", "aliases": []},
            ]})
        self.assertEqual(status, 403, saved)  # grace was closed; the HB password was not offered
        self.assertEqual([d for d in gsi.load()["devices"] if d["kind"] == "homebridge"], [])

        def save():
            return self.c.req("POST", "/api/integrations/homebridge/devices", {"devices": [
                {"uid": "light-1", "name": "Porch Light", "aliases": ["porch"]},
                {"uid": "fan-1", "name": "Garage Fan", "aliases": []},
                {"uid": "a+b", "name": "Plus Light", "aliases": []},
            ], "password": GOOD})
        (status, saved), lines = self._prints(save)
        self.assertEqual(status, 200, saved)
        self.assertEqual(len(saved["devices"]), 3)
        for pub in saved["devices"]:
            self.assertEqual(set(pub), {"id", "name", "aliases", "type", "caps"})
            self.assertEqual(pub["caps"], ["onoff"])
            self.assertRegex(pub["id"], r"^h-[0-9a-f]{4}$")
        self._no_secret(saved)
        self._no_secret("\n".join(lines))
        disk = gsi.load()
        self.assertNotIn("voice", disk["integrations"]["homebridge"])
        porch = next(d for d in disk["devices"] if d["uid"] == "light-1")
        fan = next(d for d in disk["devices"] if d["uid"] == "fan-1")
        plus = next(d for d in disk["devices"] if d["uid"] == "a+b")
        self.assertEqual(porch["char"], "On")
        self.assertEqual(fan["char"], "Active")
        self.assertEqual(porch["type"], "light")

        status, view = self.c.req("GET", "/api/integrations")
        self.assertEqual(status, 200)
        shown = {d["id"]: d for d in view["devices"]}
        self.assertEqual(shown[porch["id"]]["kind"], "homebridge")
        self.assertEqual(shown[porch["id"]]["uid"], "light-1")
        self.assertNotIn("password", json.dumps(view))
        self.assertNotIn(SECRET, json.dumps(view))
        self.assertEqual(view["integrations"]["homebridge"]["status"], "connected")
        self.assertFalse(view["integrations"]["homebridge"]["voice"])
        self.assertNotIn("homebridge", {c["id"] for c in view["later"]})

        # Test writes the current value. False -> 0. A bool True -> 1, not a guess.
        HB.requests = []
        status, tested = self.c.req("POST", "/api/integrations/devices/%s/test" % porch["id"], {})
        self.assertEqual(status, 200, tested)
        put = next(r for r in HB.requests if r["method"] == "PUT")
        self.assertEqual(put["body"], {"characteristicType": "On", "value": 0})
        self.assertIs(type(put["body"]["value"]), int)
        HB.requests = []
        # bool-1 was not saved yet. Value in the fake is JSON true.
        status, _saved = self.c.req("POST", "/api/integrations/homebridge/devices", {"devices": [
            {"uid": "bool-1", "name": "Bool Light", "aliases": []}], "password": GOOD})
        self.assertEqual(status, 200, _saved)  # grace stays closed in this test
        bool_id = next(d["id"] for d in gsi.load()["devices"] if d["uid"] == "bool-1")
        HB.requests = []
        self.assertEqual(self.c.req("POST", "/api/integrations/devices/%s/test" % bool_id, {})[0], 200)
        put = next(r for r in HB.requests if r["method"] == "PUT")
        self.assertIs(type(put["body"]["value"]), int)
        self.assertEqual(put["body"]["value"], 1)
        self.assertIsNot(put["body"]["value"], True)

        HB.requests = []
        self.assertEqual(self.c.req("POST", "/api/integrations/devices/%s/test" % plus["id"], {})[0], 200)
        put = next(r for r in HB.requests if r["method"] == "PUT")
        self.assertIn("%2B", put["raw_path"])
        self.assertNotIn("+", put["raw_path"])

        # Unknown value is not guessed.
        for row in HB.accessories:
            if row.get("uniqueId") == "light-1":
                row["serviceCharacteristics"][0]["value"] = "on"
        HB.requests = []
        status, body = self.c.req("POST", "/api/integrations/devices/%s/test" % porch["id"], {})
        self.assertEqual((status, body["error"]), (502, "unreachable"))
        self.assertFalse(any(r["method"] == "PUT" for r in HB.requests))

        red = gsi.redact_registry(gsi.load())
        self.assertEqual(red["integrations"]["homebridge"]["base"], "redacted")
        red_porch = next(d for d in red["devices"] if d["id"] == porch["id"])
        self.assertEqual(red_porch["uid"], "redacted")
        self.assertEqual(red_porch["name"], porch["id"])
        self.assertNotIn("Porch", json.dumps(red))
        self._no_secret(red)

        # WLED-only redact stays the old shape when Homebridge is absent.
        wled_only = {"version": 1, "integrations": {"wled": {"enabled": True}}, "devices": [{
            "id": "w-3f9a", "kind": "wled", "host": "wled-kitchen.local", "name": "Kitchen Strip",
            "aliases": ["kitchen lights"], "type": "light", "caps": ["onoff", "bri", "color", "preset"],
            "presets": {"party": 3}}]}
        red_w = gsi.redact_registry(wled_only)
        self.assertEqual(red_w["integrations"], {"wled": {"enabled": True}})
        self.assertEqual(red_w["devices"][0]["host"], "redacted")
        self.assertEqual(red_w["devices"][0]["name"], "w-3f9a")
        self.assertNotIn("homebridge", json.dumps(red_w))

    def test_wrong_stepup_contacts_nothing(self):
        """Verify checks the admin password before it talks to Homebridge (no login oracle)."""
        self._grace_off()
        HB.requests = []
        status, body = self._verify(stepup="wrong-password")
        self.assertEqual(status, 403, body)
        self.assertEqual(HB.requests, [])
        self.assertFalse(os.path.exists(self.creds))

    def test_admin_and_unconfirmed_save_nothing(self):
        self._grace_off()
        HB.mode = "admin"
        def go():
            return self._verify()
        (status, body), lines = self._prints(go)
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"], "admin")
        self.assertEqual(body["message"], ADMIN_MSG)
        self.assertEqual(body["checks"][2], {"id": "admin", "ok": False})
        self.assertTrue(body["checks"][1]["ok"])
        self.assertFalse(os.path.exists(self.creds))
        self.assertFalse(any(cmd == "homebridge-set" for cmd, _ in self.calls))
        self.assertNotIn("homebridge", gsi.load()["integrations"])
        self._no_secret(body)
        self._no_secret("\n".join(lines))

        HB.reset()
        HB.mode = "users500"
        status, body = self._verify()
        self.assertEqual((status, body["error"], body["message"]), (403, "unconfirmed", UNCONFIRMED))
        self.assertNotIn(".", body["message"])
        self.assertFalse(os.path.exists(self.creds))

        HB.reset()
        HB.users_status = 401
        status, body = self._verify()
        self.assertEqual(body["error"], "unconfirmed")
        self.assertFalse(os.path.exists(self.creds))
        self.assertFalse(os.path.exists(self.block))

    def test_two_step_login_is_refused_and_nothing_saved(self):
        """A login that needs a 2-step code is refused at Verify. No code field, nothing stored (0.13.1)."""
        self._grace_off()
        HB.mode = "otp"
        status, body = self._verify()
        self.assertEqual((status, body["error"]), (400, "two_step"), body)
        self.assertEqual(body["message"], ga.HB_TWO_STEP_MSG)
        self.assertFalse(os.path.exists(self.creds))
        self.assertFalse(any(cmd == "homebridge-set" for cmd, _ in self.calls))
        # The API takes no code at all: an otp field is a bad request, and nothing is sent to Homebridge.
        HB.requests = []
        status, body = self._verify(otp="123456")
        self.assertEqual((status, body["error"]), (400, "bad_request"), body)
        self.assertEqual(HB.requests, [])
        self.assertFalse(os.path.exists(self.creds))
        js = open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8").read()
        self.assertNotIn("hbotp", js)
        self.assertNotIn("otp_required", js)
        self.assertNotIn("body.otp", js)

    def test_bad_login_and_not_homebridge(self):
        self._grace_off()
        HB.mode = "badlogin"
        status, body = self._verify()
        self.assertEqual((status, body["error"]), (401, "bad_login"))
        self.assertEqual(body["message"], "Username or password didn't work.")
        self.assertFalse(body["checks"][1]["ok"])
        self.assertFalse(os.path.exists(self.creds))

        HB.reset()
        HB.mode = "plain"
        status, body = self.c.req("POST", "/api/integrations/homebridge/check-address", {"base": self._base()})
        self.assertEqual(body["error"], "not_homebridge")
        self.assertIn("isn't the Homebridge UI", body["message"])
        self.assertIn("8581", body["message"])

        HB.mode = "missing"
        self.assertEqual(self.c.req("POST", "/api/integrations/homebridge/check-address", {"base": self._base()})[1]["error"], "not_homebridge")
        HB.mode = "big"
        self.assertEqual(self.c.req("POST", "/api/integrations/homebridge/check-address", {"base": self._base()})[1]["error"], "not_homebridge")

    def test_redirect_is_not_followed(self):
        HB.mode = "redirect"
        HB.requests = []
        status, body = self.c.req("POST", "/api/integrations/homebridge/check-address", {"base": self._base()})
        self.assertEqual(body["error"], "not_homebridge", body)
        self.assertNotIn("/secret", [r["path"] for r in HB.requests])
        self.assertEqual(len(HB.requests), 1)

    def test_timeout_is_unreachable(self):
        gsi.FETCH_TIMEOUT = 0.2
        HB.mode = "slow"
        HB.delay = 1.0
        status, body = self.c.req("POST", "/api/integrations/homebridge/check-address", {"base": self._base()})
        self.assertEqual(body["error"], "unreachable", body)

    def test_ssrf_refuses_before_connect(self):
        before = len(HB.requests)
        os.environ.pop("ADMIN_ALLOW_LOOPBACK_FOR_TESTS", None)
        status, body = self.c.req("POST", "/api/integrations/homebridge/check-address", {"base": self._base()})
        self.assertEqual((status, body["error"]), (400, "refused"))
        self.assertEqual(len(HB.requests), before)
        os.environ["ADMIN_ALLOW_LOOPBACK_FOR_TESTS"] = "1"
        for base in (
            "http://203.0.113.9", "http://8.8.8.8", "http://169.254.169.254", "http://127.0.0.2",
            "http://[::1]", "http://0.0.0.0", "http://user:%s@%s" % (SECRET, self._base().split("://", 1)[1]),
            self._base() + "/users", self._base() + "?x=1", self._base() + "#frag",
            "http://homebridge.local:8581/extra", "ftp://127.0.0.1",
        ):
            HB.requests = []
            status, body = self.c.req("POST", "/api/integrations/homebridge/check-address", {"base": base})
            self.assertEqual(body["error"], "refused", base)
            self.assertEqual(HB.requests, [], base)
            self.assertNotIn(SECRET, json.dumps(body))

        gsi.station_addresses = lambda: ["10.0.0.8"]
        self.assertEqual(self.c.req("POST", "/api/integrations/homebridge/check-address", {"base": "http://10.0.0.8"})[1]["error"], "refused")
        gsi.station_addresses = lambda: []
        self.assertEqual(self.c.req("POST", "/api/integrations/homebridge/check-address", {"base": self._base()})[1]["error"], "refused")
        gsi.station_addresses = lambda: None
        self.assertEqual(self.c.req("POST", "/api/integrations/homebridge/check-address", {"base": self._base()})[1]["error"], "refused")
        gsi.station_addresses = lambda: ["10.0.0.8"]
        gsi.resolve_host = lambda host: ["203.0.113.9"]
        HB.requests = []
        self.assertEqual(self.c.req("POST", "/api/integrations/homebridge/check-address", {"base": "http://homebridge.local:8581"})[1]["error"], "refused")
        self.assertEqual(HB.requests, [])
        gsi.resolve_host = lambda host: ["10.0.0.8"]
        self.assertEqual(self.c.req("POST", "/api/integrations/homebridge/check-address", {"base": "http://homebridge.local"})[1]["error"], "refused")

        # A public literal is a legal stored shape, and outbound calls still refuse it.
        gsi.save({"version": 1, "integrations": {"homebridge": {"enabled": True, "base": "http://203.0.113.9"}}, "devices": []})
        self.assertEqual(gsi.load()["integrations"]["homebridge"]["base"], "http://203.0.113.9")
        self.assertIsNone(gsi.checked_base("http://203.0.113.9"))
        self.assertEqual(gsi.hb_base_text("https://homebridge.local:443/"), "https://homebridge.local")
        self.assertEqual(gsi.hb_base_text("http://homebridge.local:8581/"), "http://homebridge.local:8581")
        self.assertIsNone(gsi.hb_base_text("http://homebridge.local:8581/users"))
        self.assertIs(gsi.hb_write_value(True), 1)
        self.assertIs(type(gsi.hb_write_value(True)), int)
        self.assertIs(gsi.hb_write_value(False), 0)
        self.assertIsNone(gsi.hb_write_value("on"))

    def test_auth_csrf_and_schema(self):
        anon = Client(ADMIN.port)
        self.assertEqual(anon.req("GET", "/api/integrations/homebridge/accessories")[0], 401)
        self.assertEqual(anon.req("POST", "/api/integrations/homebridge/check-address", {"base": self._base()})[0], 401)
        self.assertEqual(anon.req("POST", "/api/integrations/homebridge/verify", {"base": self._base(), "username": "grok-station", "password": SECRET})[0], 401)
        self.assertEqual(self.c.req("POST", "/api/integrations/homebridge/verify", {"base": self._base(), "username": "grok-station", "password": SECRET}, csrf=False)[0], 403)
        self.assertEqual(self.c.req("DELETE", "/api/integrations/homebridge", {}, csrf=False)[0], 403)
        self.assertEqual(self.c.req("POST", "/api/integrations/homebridge/check-address", {"base": self._base(), "password": SECRET})[1]["error"], "bad_request")
        self.assertEqual(self.c.req("POST", "/api/integrations/homebridge/verify", {"base": self._base(), "username": "bad name", "password": SECRET, "stepup": GOOD})[1]["error"], "bad_request")
        self.assertEqual(self.c.req("POST", "/api/integrations/homebridge/verify", {"base": self._base(), "username": "grok-station", "password": "short", "stepup": GOOD})[1]["error"], "bad_request")
        self.assertFalse(os.path.exists(self.creds))
        many = [{"uid": "light-1", "name": "Porch Light %d" % i, "aliases": []} for i in range(33)]
        self.assertEqual(self.c.req("POST", "/api/integrations/homebridge/devices", {"devices": many, "password": GOOD})[1]["error"], "bad_request")

    def test_name_clash_is_shared_and_delete_removes_the_file(self):
        self._verify()
        gsi.save({"version": 1, "integrations": {
            "wled": {"enabled": True},
            "homebridge": {"enabled": True, "base": self._base()},
        }, "devices": [{
            "id": "w-abcd", "kind": "wled", "host": "127.0.0.1:9", "name": "Kitchen Strip", "aliases": [],
            "type": "light", "caps": list(gsi.CAPS), "presets": {},
        }]})
        # verify opened grace; keep it so the clash is the failure, not the password.
        status, body = self.c.req("POST", "/api/integrations/homebridge/devices", {"devices": [
            {"uid": "light-1", "name": "the Kitchen Strip lights", "aliases": []}]})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["with"], "device")
        json.dump({"personas": [{"slug": "max", "name": "Max", "aliases": []}]}, open(ga.PERSONAS, "w"))
        status, body = self.c.req("POST", "/api/integrations/homebridge/devices", {"devices": [
            {"uid": "light-1", "name": "Max", "aliases": []}]})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["with"], "bot")
        status, saved = self.c.req("POST", "/api/integrations/homebridge/devices", {"devices": [
            {"uid": "light-1", "name": "Porch Light", "aliases": []}]})
        self.assertEqual(status, 200, saved)
        again = self.c.req("POST", "/api/integrations/homebridge/devices", {"devices": [
            {"uid": "light-1", "name": "Hall Switch", "aliases": []}]})
        self.assertEqual(again[1]["error"], "bad_request")  # duplicate uid
        self.assertEqual(self.c.req("POST", "/api/integrations/homebridge/devices", {"devices": [
            {"uid": "lock-1", "name": "Front Door", "aliases": []}]})[1]["error"], "bad_request")

        ident = saved["devices"][0]["id"]
        self._grace_off()
        self.assertEqual(self.c.req("DELETE", "/api/integrations/homebridge", {})[0], 403)
        self.assertTrue(os.path.exists(self.creds))
        self.calls.clear()
        def boom(*args, stdin=None, timeout=30):
            self.calls.append(args[0] if args else "")
            return False, {"error": "couldn't delete"}
        ga.helper = boom
        status, body = self.c.req("DELETE", "/api/integrations/homebridge", {"password": GOOD})
        self.assertEqual(status, 500, body)
        self.assertTrue(any(d["id"] == ident for d in gsi.load()["devices"]))
        ga.helper = self._helper_stub
        def go():
            return self.c.req("DELETE", "/api/integrations/homebridge", {"password": GOOD})
        (status, body), lines = self._prints(go)
        self.assertEqual(status, 200, body)
        self.assertFalse(os.path.exists(self.creds))
        self.assertNotIn("homebridge", gsi.load()["integrations"])
        self.assertFalse(any(d.get("kind") == "homebridge" for d in gsi.load()["devices"]))
        left = []
        for dirpath, _dirs, files in os.walk(self.tmp):
            left.extend(files)
        self.assertNotIn("homebridge.json", left)
        audits = "\n".join(ln for ln in lines if ln.startswith("audit "))
        self.assertIn("home-remove", audits)
        self.assertNotIn("Porch", audits)
        self.assertNotIn(SECRET, audits)
        self.assertIn(hashlib.sha256(ident.encode()).hexdigest()[:4], audits)

    def test_voice_switch_covers_both_and_runtime_admin_flags_the_card(self):
        self._verify()
        self.c.req("POST", "/api/integrations/homebridge/devices", {"devices": [
            {"uid": "light-1", "name": "Porch Light", "aliases": []}], "password": GOOD})
        self._grace_off()
        status, body = self.c.req("PUT", "/api/integrations/wled/voice", {"on": True})
        self.assertEqual(status, 403, body)
        self.assertNotIn("voice", gsi.load()["integrations"]["homebridge"])
        status, body = self.c.req("PUT", "/api/integrations/wled/voice", {"on": True, "password": GOOD})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["integrations"]["homebridge"]["voice"])
        self.assertTrue(gsi.load()["integrations"]["homebridge"]["voice"])
        self.assertNotIn("wled", gsi.load()["integrations"])
        status, body = self.c.req("PUT", "/api/integrations/wled/voice", {"on": False})
        self.assertEqual(status, 200, body)
        self.assertNotIn("voice", gsi.load()["integrations"]["homebridge"])

        # Saving the other kind copies an already-on switch. It does not turn the switch on by itself.
        doc = gsi.load()
        doc["integrations"]["wled"] = {"enabled": True, "voice": True}
        doc["devices"].append({
            "id": "w-abcd", "kind": "wled", "host": "127.0.0.1:9", "name": "Kitchen Strip", "aliases": [],
            "type": "light", "caps": list(gsi.CAPS), "presets": {}})
        gsi.save(doc)
        self.c.req("POST", "/api/integrations/homebridge/devices", {"devices": [
            {"uid": "sw-1", "name": "Hall Switch", "aliases": []}], "password": GOOD})
        self.assertTrue(gsi.load()["integrations"]["homebridge"]["voice"])
        status, body = self.c.req("PUT", "/api/integrations/wled/voice", {"on": False})
        self.assertNotIn("voice", gsi.load()["integrations"]["wled"])
        self.assertNotIn("voice", gsi.load()["integrations"]["homebridge"])

        HB.mode = "admin"
        status, body = self.c.req("GET", "/api/integrations/homebridge/accessories")
        self.assertEqual(body["error"], "admin")
        self.assertEqual(json.load(open(self.block, encoding="utf-8"))["blocked"], "admin")
        view = self.c.req("GET", "/api/integrations")[1]
        self.assertEqual(view["integrations"]["homebridge"]["status"], "admin")
        self.assertFalse(view["integrations"]["homebridge"]["voice"])
        self.assertNotIn(SECRET, json.dumps(view))

        os.unlink(self.block)
        HB.mode = "badlogin"
        status, body = self.c.req("GET", "/api/integrations/homebridge/accessories")
        self.assertEqual(body["error"], "signin")
        self.assertEqual(json.load(open(self.block, encoding="utf-8"))["blocked"], "signin")

        os.unlink(self.block)
        HB.reset()
        HB.mode = "users500"
        status, body = self.c.req("GET", "/api/integrations/homebridge/accessories")
        self.assertEqual(body["error"], "unconfirmed")
        self.assertFalse(os.path.exists(self.block))

    def test_wrapped_accessory_list_and_patch(self):
        self._verify()
        HB.wrap = True
        status, acc = self.c.req("GET", "/api/integrations/homebridge/accessories")
        self.assertEqual(status, 200, acc)
        self.assertTrue(any(row["uid"] == "light-1" for row in acc["accessories"]))
        self.c.req("POST", "/api/integrations/homebridge/devices", {"devices": [
            {"uid": "light-1", "name": "Porch Light", "aliases": ["porch"]}], "password": GOOD})
        ident = gsi.load()["devices"][0]["id"]
        status, renamed = self.c.req("PATCH", "/api/integrations/devices/" + ident, {"name": "Yard Light", "aliases": []})
        self.assertEqual(status, 200, renamed)
        self.assertEqual(renamed["name"], "Yard Light")
        self.assertNotIn("kind", renamed)
        self.assertEqual(gsi.load()["devices"][0]["uid"], "light-1")
        self.assertEqual(self.c.req("DELETE", "/api/integrations/devices/" + ident, {})[0], 200)
        # The login stays. Signed in, no devices.
        doc = gsi.load()
        self.assertEqual(doc["devices"], [])
        self.assertEqual(doc["integrations"]["homebridge"]["base"], self._base())
        self.assertTrue(os.path.exists(self.creds))
        view = self.c.req("GET", "/api/integrations")[1]["integrations"]["homebridge"]
        self.assertEqual(view["status"], "signed_in")
        self.assertEqual(view["base"], self._base())
