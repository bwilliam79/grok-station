"""WLED integrations admin API. Real handler, fake light on 127.0.0.1.

169.254.169.254 is link-local and is refused anyway (cloud metadata). Other
169.254.0.0/16 addresses pass the address check. ADMIN_ALLOW_LOOPBACK_FOR_TESTS=1
allows exactly 127.0.0.1, even when that address is also one of this machine's
own addresses. It is never set in a unit file. There is no support-bundle route;
redact_registry() is what a bundle should write.
"""
import hashlib, http.client, io, json, os, shutil, stat, struct, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.join(os.path.dirname(__file__), "..")
if "grok_admin" not in sys.modules:
    _own = tempfile.mkdtemp(prefix="gs130-int-")
    os.environ.setdefault("ADMIN_STATE_DIR", os.path.join(_own, "state"))
    os.environ.setdefault("ADMIN_RUN_DIR", os.path.join(_own, "run"))
    os.environ.setdefault("ADMIN_PERSONAS", os.path.join(_own, "personas.json"))
    os.environ.setdefault("ADMIN_RELAY_ENV", os.path.join(_own, "missing-admin-relay.env"))
    os.environ.setdefault("ADMIN_WAKE_CTL", "http://127.0.0.1:9")
    os.makedirs(os.environ["ADMIN_STATE_DIR"], exist_ok=True)
    os.makedirs(os.environ["ADMIN_RUN_DIR"], exist_ok=True)
    sys.path.insert(0, os.path.join(ROOT, "admin"))

import grok_admin as ga  # noqa: E402
import gs_integrations as gsi  # noqa: E402

GOOD = "kitchen table lamp 42"
SECRET = "device-secret-should-not-echo"
APPLE = "Use Homebridge for these devices. Apple Home has no API outside Apple devices."
STATE = {"on": False, "bri": 40, "seg": [{"id": 0}]}
WHITE = {"on": True, "bri": 255, "seg": [{"col": [[255, 255, 255]]}]}


class _WLED(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _hit(self):
        self.server.hits.append((self.command, self.path.split("?")[0]))

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._hit()
        path = self.path.split("?")[0]
        if self.server.mode == "redirect" and path == "/json/info":
            port = self.server.server_address[1]
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:%d/secret" % port)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/secret":
            return self._json(200, {"name": "Secret", "ver": "0.1.0", "leds": {"count": 1}, "psk": SECRET})
        if path == "/json/info":
            if self.server.mode == "plain":
                raw = b"not-json " + SECRET.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if self.server.mode == "huge":
                return self._json(200, {"name": "Kitchen Strip", "ver": "0.14.4", "leds": {"count": 1}, "pad": "x" * 70000})
            return self._json(200, {"name": "Kitchen Strip", "ver": "0.14.4", "leds": {"count": 120}, "psk": SECRET})
        if path == "/json/state":
            return self._json(200, dict(self.server.state))
        if path == "/presets.json":
            return self._json(200, {"3": {"n": "Party", "id": 3, "note": SECRET}})
        return self._json(404, {"error": SECRET})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        self.server.hits.append(("POST", self.path.split("?")[0], raw))
        if self.path.split("?")[0] == "/json/state":
            return self._json(200, {"success": True, "psk": SECRET})
        return self._json(404, {"error": SECRET})


def _free_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _WLED)
    httpd.hits = []
    httpd.mode = "ok"
    httpd.state = dict(STATE)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _admin_server():
    httpd = ga.ThreadingHTTPServer(("127.0.0.1", 0), ga.Handler)
    httpd.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class Client:
    def __init__(self):
        self.jar = {}

    def req(self, method, path, body=None, csrf=True, ctype="application/json", data=None):
        conn = http.client.HTTPConnection("127.0.0.1", ADMIN.port, timeout=10)
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


def _signed_in():
    _reset()
    client = Client()
    ga.AUTH.publish()
    code = json.load(open(os.path.join(ga.RUN_DIR, "setup.json")))["code"]
    assert client.req("POST", "/api/setup/code", {"code": code})[0] == 200
    assert client.req("POST", "/api/setup/password", {"password": GOOD})[0] == 200
    return client


def _dns_name(text):
    out = b""
    for label in text.split("."):
        raw = label.encode("ascii")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _sample_packet():
    """One PTR, one SRV (compressed owner), one A. Host name is inside the SRV rdata."""
    owner = _dns_name("_wled._tcp.local")
    target = _dns_name("strip._wled._tcp.local")
    host = _dns_name("wled-kitchen.local")
    body = bytearray()
    body += owner
    body += struct.pack("!HHIH", 12, 1, 60, len(target))
    target_at = 12 + len(body)
    body += target
    body += struct.pack("!H", 0xC000 | target_at)
    rdata = struct.pack("!HHH", 0, 0, 8080) + host
    body += struct.pack("!HHIH", 33, 1, 60, len(rdata))
    host_at = 12 + len(body) + 6
    body += rdata
    body += struct.pack("!H", 0xC000 | host_at)
    body += struct.pack("!HHIH", 1, 1, 60, 4) + bytes((203, 0, 113, 9))
    return struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 2) + bytes(body)


ADMIN = None
LIGHT = None


def setUpModule():
    global ADMIN, LIGHT
    LIGHT = _free_server()
    ADMIN = _admin_server()


def tearDownModule():
    ADMIN.shutdown()
    LIGHT.shutdown()


class IntegrationsAPI(unittest.TestCase):
    def setUp(self):
        self._blink = gsi.BLINK_S
        self._mdns = gsi.mdns_browse
        self._resolve = gsi.resolve_host
        self._own = gsi.station_addresses
        self._relay = ga.relay_call
        gsi.BLINK_S = 0
        ga.relay_call = lambda *args, **kwargs: None
        os.environ["ADMIN_ALLOW_LOOPBACK_FOR_TESTS"] = "1"
        os.environ["ADMIN_INTEGRATIONS"] = os.path.join(ga.STATE_DIR, "integrations.json")
        # Own state: whichever test imported grok_admin first may have left PERSONAS at a system path.
        self._ga_personas = ga.PERSONAS
        ga.PERSONAS = os.path.join(ga.STATE_DIR, "personas-gs130.json")
        self.personas = ga.PERSONAS
        self._persona_existed = os.path.exists(self.personas)
        self._persona_body = open(self.personas).read() if self._persona_existed else None
        LIGHT.hits = []
        LIGHT.mode = "ok"
        LIGHT.state = dict(STATE)
        self.c = _signed_in()
        os.environ["ADMIN_INTEGRATIONS"] = os.path.join(ga.STATE_DIR, "integrations.json")

    def tearDown(self):
        ga.PERSONAS = self._ga_personas
        gsi.BLINK_S = self._blink
        gsi.mdns_browse = self._mdns
        gsi.resolve_host = self._resolve
        gsi.station_addresses = self._own
        ga.relay_call = self._relay
        ga.AUTH.fails = []
        os.environ.pop("ADMIN_ALLOW_LOOPBACK_FOR_TESTS", None)
        os.environ.pop("ADMIN_INTEGRATIONS", None)
        if self._persona_existed:
            open(self.personas, "w").write(self._persona_body)
        elif os.path.exists(self.personas):
            os.unlink(self.personas)

    def _host(self):
        return "127.0.0.1:%d" % LIGHT.server_address[1]

    def _save(self, name="Kitchen Strip", aliases=None, host=None):
        item = {"host": host or self._host(), "name": name, "aliases": [] if aliases is None else aliases}
        # 0.13.0 (Nix I1): adding a device needs the admin password (or an open grace window).
        return self.c.req("POST", "/api/integrations/wled/devices", {"devices": [item], "password": GOOD})

    def _prints(self, fn):
        import builtins
        lines = []
        real = builtins.print
        def grab(*args, **kwargs):
            lines.append(" ".join(str(a) for a in args))
        builtins.print = grab
        try:
            out = fn()
        finally:
            builtins.print = real
        return out, lines

    def test_discover_save_rename_blink_delete(self):
        port = LIGHT.server_address[1]
        def browse(timeout=gsi.DISCOVER_S):
            return [
                {"name": "ignored", "host": "127.0.0.1", "port": port},
                {"name": "public", "host": "203.0.113.50", "port": 80},
            ]
        gsi.mdns_browse = browse
        status, found = self.c.req("POST", "/api/integrations/wled/discover", {})
        self.assertEqual(status, 200, found)
        self.assertEqual(len(found["devices"]), 1)
        row = found["devices"][0]
        self.assertEqual(row["name"], "Kitchen Strip")
        self.assertEqual(row["firmware"], "0.14.4")
        self.assertEqual(row["leds"], 120)
        self.assertEqual(row["host"], self._host())
        self.assertNotIn(SECRET, json.dumps(found))

        def go():
            return self._save(aliases=["kitchen lights", "the Kitchen Strip lights"])
        (status, saved), lines = self._prints(go)
        self.assertEqual(status, 200, saved)
        self.assertEqual(len(saved["devices"]), 1)
        pub = saved["devices"][0]
        self.assertEqual(set(pub), {"id", "name", "aliases", "type", "caps"})
        self.assertNotIn("host", pub)
        self.assertNotIn(SECRET, json.dumps(saved))
        self.assertEqual(pub["aliases"], ["kitchen lights"])  # same spoken key as the name is dropped
        self.assertEqual(pub["caps"], ["onoff", "bri", "color", "preset"])
        self.assertRegex(pub["id"], r"^w-[0-9a-f]{4}$")
        ident = pub["id"]

        path = gsi.integrations_path()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
        doc = json.load(open(path))
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["integrations"], {"wled": {"enabled": True}})
        self.assertEqual(doc["devices"][0]["host"], self._host())
        self.assertEqual(doc["devices"][0]["presets"], {"party": 3})
        self.assertNotIn(SECRET, json.dumps(doc))
        self.assertFalse(any(n.startswith(".tmp-") for n in os.listdir(os.path.dirname(path))))
        audits = [ln for ln in lines if ln.startswith("audit ")]
        digest = hashlib.sha256(ident.encode()).hexdigest()[:4]
        self.assertTrue(any("action=home-save" in ln and "last4=%s" % digest in ln for ln in audits), audits)
        self.assertNotIn("Kitchen", "\n".join(audits))
        self.assertNotIn("127.0.0.1", "\n".join(audits))

        status, view = self.c.req("GET", "/api/integrations")
        self.assertEqual(status, 200)
        self.assertNotIn("127.0.0.1", json.dumps(view))
        self.assertNotIn("host", view["devices"][0])
        self.assertNotIn("presets", view["devices"][0])
        self.assertEqual(view["integrations"]["wled"]["status"], "connected")
        later = {card["id"]: card for card in view["later"]}
        self.assertNotIn("homebridge", later)
        self.assertEqual(view["integrations"]["homebridge"]["status"], "not_set_up")
        self.assertNotIn("username", json.dumps(view))
        self.assertNotIn("password", json.dumps(view))
        self.assertEqual(later["matter"]["name"], "Matter")
        self.assertEqual(later["nest"]["name"], "Google Nest thermostat")
        self.assertEqual(later["cameras"]["name"], "Cameras (RTSP)")
        self.assertEqual(later["apple_home"]["note"], APPLE)

        status, renamed = self.c.req("PATCH", "/api/integrations/devices/" + ident, {"name": "Porch Light", "aliases": ["porch"]})
        self.assertEqual(status, 200, renamed)
        self.assertEqual(renamed["name"], "Porch Light")
        self.assertNotIn("host", renamed)
        self.assertEqual(json.load(open(path))["devices"][0]["host"], self._host())

        LIGHT.hits = []
        status, tested = self.c.req("POST", "/api/integrations/devices/%s/test" % ident, {})
        self.assertEqual(status, 200, tested)
        posts = [json.loads(item[2]) for item in LIGHT.hits if item[0] == "POST"]
        self.assertEqual(posts[0], WHITE)
        self.assertEqual(posts[1]["on"], False)
        self.assertEqual(posts[1]["bri"], 40)
        self.assertNotIn(SECRET, json.dumps(tested))

        status, gone = self.c.req("DELETE", "/api/integrations/devices/" + ident, {})
        self.assertEqual(status, 200, gone)
        disk = gsi.load()
        self.assertEqual(disk["devices"], [])
        self.assertNotIn("wled", disk["integrations"])

    def test_auth_csrf_size_and_schema(self):
        anon = Client()
        self.assertEqual(anon.req("GET", "/api/integrations")[0], 401)
        status, body = anon.req("POST", "/api/integrations/wled/discover", {})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "sign in")
        self.assertEqual(self.c.req("POST", "/api/integrations/wled/discover", {}, csrf=False)[0], 403)
        self.assertEqual(self.c.req("POST", "/api/integrations/wled/probe", {"host": self._host()}, ctype="text/plain")[0], 403)
        self.assertEqual(self.c.req("PATCH", "/api/integrations/devices/w-abcd", {"name": "Porch Light"}, csrf=False)[0], 403)
        self.assertEqual(self.c.req("DELETE", "/api/integrations/wled", {}, csrf=False)[0], 403)
        origin = self.c.req("POST", "/api/integrations/wled/discover", {}, csrf=False)
        self.assertEqual(origin[1]["error"], "bad request origin")

        prefix, suffix = b'{"pad":"', b'"}'
        exact = prefix + b"a" * (16384 - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(exact), 16384)
        status, body = self.c.req("POST", "/api/integrations/wled/probe", data=exact)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_request")
        over = prefix + b"a" * (16385 - len(prefix) - len(suffix)) + suffix
        status, body = self.c.req("POST", "/api/integrations/wled/probe", data=over)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad request")
        self.assertNotEqual(status, 413)

        self.assertEqual(self.c.req("POST", "/api/integrations/wled/discover", {"x": 1})[1]["error"], "bad_request")
        self.assertEqual(self.c.req("POST", "/api/integrations/wled/probe", {"host": self._host(), "name": "Kitchen Strip"})[1]["error"], "bad_request")
        self.assertEqual(self.c.req("POST", "/api/integrations/wled/blink", {})[1]["error"], "bad_request")
        status, body = self.c.req("POST", "/api/integrations/wled/devices", {"host": self._host(), "name": "Kitchen Strip", "aliases": []})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_request")  # 0.13.0 (Nix I1): an object is parsed; it needs "devices"
        bad = {"host": self._host(), "name": "Kitchen Strip", "aliases": "porch"}
        self.assertEqual(self.c.req("POST", "/api/integrations/wled/devices", [bad])[1]["error"], "bad_request")
        self.assertEqual(self.c.req("POST", "/api/integrations/wled/devices", [])[1]["error"], "bad_request")
        self.assertEqual(gsi.load()["devices"], [])

    def test_ssrf_refuses_public_loopback_metadata_and_self(self):
        os.environ.pop("ADMIN_ALLOW_LOOPBACK_FOR_TESTS", None)
        status, body = self.c.req("POST", "/api/integrations/wled/probe", {"host": self._host()})
        self.assertEqual((status, body["error"]), (400, "refused"))
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertFalse(any(path == "/json/info" for _m, path in LIGHT.hits))

        os.environ["ADMIN_ALLOW_LOOPBACK_FOR_TESTS"] = "1"
        for host in ("203.0.113.9", "8.8.8.8", "169.254.169.254", "127.0.0.2", "[::1]", "http://127.0.0.1/json/info", "0.0.0.0"):
            status, body = self.c.req("POST", "/api/integrations/wled/probe", {"host": host})
            self.assertEqual(body["error"], "refused", host)
            self.assertNotIn(SECRET, json.dumps(body))
            self.assertNotIn("203.0.113", json.dumps(body))
        gsi.station_addresses = lambda: ["10.0.0.8"]
        self.assertEqual(self.c.req("POST", "/api/integrations/wled/probe", {"host": "10.0.0.8"})[1]["error"], "refused")
        gsi.station_addresses = self._own
        gsi.resolve_host = lambda host: ["203.0.113.9"]
        self.assertEqual(self.c.req("POST", "/api/integrations/wled/probe", {"host": "wled-kitchen.local"})[1]["error"], "refused")
        gsi.resolve_host = lambda host: ["10.0.0.20"]
        self.assertTrue(gsi.host_ok("wled-kitchen.local"))
        gsi.resolve_host = lambda host: []
        self.assertFalse(gsi.host_ok("wled-kitchen.local"))
        self.assertTrue(gsi.host_ok("169.254.1.1"))
        self.assertTrue(gsi.host_ok("[fd12:3456::1]"))
        self.assertTrue(gsi.host_ok("10.0.0.20"))
        self.assertFalse(gsi.host_ok("224.0.0.1"))
        self.assertFalse(gsi.host_ok("[::ffff:203.0.113.9]"))
        self.assertTrue(gsi.host_ok("[::ffff:10.0.0.5]"))
        self.assertFalse(any(path == "/json/info" for method, path in LIGHT.hits if method == "GET"))

        # A refused host in the batch writes nothing, including the good row beside it.
        status, body = self.c.req("POST", "/api/integrations/wled/devices", {"devices": [
            {"host": self._host(), "name": "Kitchen Strip", "aliases": []},
            {"host": "203.0.113.9", "name": "Porch Light", "aliases": []},
        ], "password": GOOD})
        self.assertEqual(body["error"], "refused", body)
        self.assertEqual(gsi.load()["devices"], [])

    def test_redirect_is_not_followed_and_bodies_are_not_echoed(self):
        LIGHT.mode = "redirect"
        status, body = self.c.req("POST", "/api/integrations/wled/probe", {"host": self._host()})
        self.assertEqual(body["error"], "not_wled", body)
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertNotIn("/secret", [path for _m, path in LIGHT.hits])
        LIGHT.mode = "plain"
        LIGHT.hits = []
        status, body = self.c.req("POST", "/api/integrations/wled/probe", {"host": self._host()})
        self.assertEqual(body["error"], "not_wled")
        self.assertNotIn("not-json", json.dumps(body))
        LIGHT.mode = "huge"
        status, body = self.c.req("POST", "/api/integrations/wled/probe", {"host": self._host()})
        self.assertEqual(body["error"], "not_wled")
        self.assertLess(len(json.dumps(body)), 200)

    def test_clash_refuses_both_ways(self):
        json.dump({"personas": [{"slug": "max", "name": "Max", "aliases": ["Buddy"]}]}, open(self.personas, "w"))
        status, body = self._save(name="the Max light")
        self.assertEqual(status, 409, body)
        self.assertEqual(body, {"error": "clash", "with": "bot", "name": "Max"})
        status, body = self._save(name="Stop")
        self.assertEqual(body["with"], "reserved")
        self.assertEqual(body["name"], "stop")
        status, body = self._save(name="Hey Grok")
        self.assertEqual(body["with"], "bot")
        self.assertEqual(body["name"], "Hey Grok")
        status, body = self._save(aliases=["next"])
        self.assertEqual(body["with"], "reserved")
        self.assertEqual(gsi.load()["devices"], [])

        status, saved = self._save()
        self.assertEqual(status, 200, saved)
        status, body = self.c.req("POST", "/api/integrations/wled/devices", [
            {"host": self._host(), "name": "Hall Strip", "aliases": []},
            {"host": self._host(), "name": "Porch Light", "aliases": []},
        ])
        self.assertEqual(body["error"], "bad_request")  # same host, nothing new saved
        status, body = self.c.req("POST", "/api/integrations/wled/devices", [
            {"host": "10.0.0.21", "name": "Porch Light", "aliases": []},
            {"host": "10.0.0.22", "name": "the porch lights", "aliases": []},
        ])
        # Clash is decided before any connection, so the unanswered addresses are not dialed.
        self.assertEqual(status, 409, body)
        self.assertEqual(body["with"], "device")
        self.assertEqual(body["name"], "Porch Light")
        self.assertEqual(len(gsi.load()["devices"]), 1)

        status, body = self.c.req("POST", "/api/bots", {"slug": "porch", "name": "Kitchen Strip", "password": GOOD})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["with"], "device")
        self.assertEqual(body["name"], "Kitchen Strip")
        status, body = self.c.req("PATCH", "/api/bots/porch", {"name": "the kitchen strip lights"})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["with"], "device")

    def test_delete_wled_needs_stepup(self):
        ident = "w-abcd"  # seeded directly: saving with the password would open the grace window
        gsi.save({"version": 1, "integrations": {"wled": {"enabled": True}}, "devices": [
            {"id": ident, "kind": "wled", "host": self._host(), "name": "Kitchen Strip", "aliases": [],
             "type": "light", "caps": list(gsi.CAPS), "presets": {}}]})
        status, body = self.c.req("DELETE", "/api/integrations/wled", {})
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"], ga.PASSWORD_BAD)
        self.assertEqual(len(gsi.load()["devices"]), 1)
        status, body = self.c.req("DELETE", "/api/integrations/wled", {"password": "nope-nope-nope"})
        self.assertEqual(status, 403)
        self.assertEqual(len(gsi.load()["devices"]), 1)
        def go():
            return self.c.req("DELETE", "/api/integrations/wled", {"password": GOOD})
        (status, body), lines = self._prints(go)
        self.assertEqual(status, 200, body)
        self.assertEqual(gsi.load()["devices"], [])
        audits = "\n".join(ln for ln in lines if ln.startswith("audit "))
        self.assertIn("action=home-remove", audits)
        self.assertIn("action=home-remove-wled", audits)
        self.assertIn("last4=%s" % hashlib.sha256(ident.encode()).hexdigest()[:4], audits)
        self.assertIn("last4=%s" % hashlib.sha256(b"wled").hexdigest()[:4], audits)
        self.assertNotIn("Kitchen", audits)
        # The correct password opened the grace window. A second delete does not ask again.
        self.assertEqual(self.c.req("DELETE", "/api/integrations/wled", {})[0], 200)
        self.assertIsNotNone(self.c.req("GET", "/api/state")[1]["stepup_until"])

    def test_registry_helpers_and_dns_parser(self):
        self.assertIn(b"\x00\x0c\x80\x01", gsi.mdns_query())
        found = gsi.parse_mdns(_sample_packet())
        self.assertEqual(found, [{"name": "strip", "host": "wled-kitchen.local", "port": 8080}])
        self.assertEqual(gsi.parse_mdns(b"\x00" * 11), [])
        self.assertEqual(gsi.parse_mdns(b"this is not dns"), [])
        loop = struct.pack("!HHHHHH", 0, 0, 0, 1, 0, 0) + bytes((0xC0, 0x0C)) + struct.pack("!HHIH", 12, 1, 0, 1) + b"\x00"
        self.assertEqual(gsi.parse_mdns(loop), [])
        self.assertEqual(gsi.parse_mdns(struct.pack("!HHHHHH", 0, 0, 0, 1, 0, 0) + b"\x50abcd"), [])
        listed = gsi.mdns_browse(0.05)
        self.assertIsInstance(listed, list)

        doc = {"version": 1, "integrations": {"wled": {"enabled": True}}, "devices": [{
            "id": "w-3f9a", "kind": "wled", "host": "wled-kitchen.local", "name": "Kitchen Strip",
            "aliases": ["kitchen lights"], "type": "light", "caps": ["onoff", "bri", "color", "preset"],
            "presets": {"party": 3}}]}
        red = gsi.redact_registry(doc)
        self.assertEqual(red["devices"][0]["host"], "redacted")
        self.assertEqual(red["devices"][0]["name"], "w-3f9a")
        self.assertEqual(red["devices"][0]["aliases"], ["w-3f9a"])
        self.assertNotIn("party", red["devices"][0]["presets"])
        self.assertNotIn("Kitchen", json.dumps(red))
        self.assertEqual(gsi.spoken_key("The Kitchen Lights!"), "kitchen")
        self.assertEqual(gsi.spoken_key("never mind"), "never mind")

        path = os.path.join(ga.STATE_DIR, "atomic.json")
        gsi.atomic_write(path, b"{}\n", 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
        self.assertEqual(open(path, "rb").read(), b"{}\n")


class IntegrationsUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "admin", "web", "app.js"), encoding="utf-8") as fh:
            cls.js = fh.read()

    def test_nav_more_menu_and_copy(self):
        js = self.js
        self.assertIn('["network", "Network"], ["integrations", "Integrations"]]', js)
        self.assertIn('integrations: ["Integrations", pageIntegrations]', js)
        self.assertIn('const TABS = [["home", "Home", "home"], ["bots", "Bots", "bots"], ["system", "System", "system"], ["more", "More", "more"]]', js)
        self.assertIn('const tabPage = page === "integrations" ? "more" : page;', js)
        more = js.split("function pageMore", 1)[1].split("function spokenKey", 1)[0]
        self.assertIn('["integrations", "Integrations"]', more)
        page = js.split("function spokenKey", 1)[1].split("\nfunction deployScript", 1)[0]
        for text in (
            "Set up", "Manage", "Add lights", "Looking for WLED lights on your network.",
            "No WLED lights found.", "The station and the lights have to be on the same network.",
            "Add by address", "That address didn't answer like a WLED light.",
            "That address isn't on your home network.", "Clashes with bot ", "Used by another device",
            "Clashes with ", "Recommended, not required.", "Settings PIN", "OTA lock",
            "Turn on Voice home control, then try it: say 'Hey Grok, turn on ", "Voice home control", "Turn on voice home control?",
            "Say the light's name; 'turn off the lights' on its own isn't supported yet.",
            "Offline since ", APPLE, "Google Nest thermostat", "Cameras (RTSP)", "Matter",
            'role="checkbox"', "Blink", "wled-kitchen.local",
            "Connect Homebridge",
            "Grok Station uses its own Homebridge login that can control devices but can't change Homebridge settings.",
            "Open Homebridge Users", 'rel="noopener noreferrer"', "Your Homebridge address",
            "http://homebridge.local:8581", "Found Homebridge at this address.",
            "Leave Admin off", "Enter that login", "Reached Homebridge", "Login works", "Not an admin",
            "This login is an admin. Grok Station won't store an admin login.",
            "Couldn't confirm this login isn't an admin", "Pick devices", "Fix sign-in",
            "This login is an admin. Homebridge control is off.", "Username or password didn't work.",
            "grok-station",
        ):
            self.assertIn(text, page, text)
        self.assertNotIn("Coming next", page)
        self.assertNotIn("Turn on Voice home control in Manage", page)
        self.assertIn("esc(d.name)", page)
        self.assertIn("esc(dev.name", page)
        self.assertNotIn("<select", page)
        self.assertNotIn('type="checkbox"', page)
