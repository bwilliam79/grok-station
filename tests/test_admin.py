"""Admin page API (admin/grok_admin.py): setup code, password rules, lockout, sessions, CSRF, LAN-only, write-only
secrets, wake-word overrides, Pi-only speakers, reset (CLI + boot file), static files. In-process server on a free port;
the root helper and wake listener are fakes, so nothing touches the system. No browser."""
import http.client, json, os, re, shutil, socket, stat, subprocess, sys, tempfile, threading, time, unittest

TMP = tempfile.mkdtemp(prefix="gs-admin-")
ROOT = os.path.join(os.path.dirname(__file__), "..")
HELPER = os.path.join(TMP, "helper.py")
with open(HELPER, "w") as f:  # fake grok-station-helper: logs argv + stdin, answers like the real one
    f.write(r'''import json, os, sys
a = sys.argv[1:]; raw = sys.stdin.read()
open(os.environ["FAKE_LOG"], "a").write(json.dumps({"argv": a, "stdin": raw}) + "\n")
if a[0] == "secret-status": print(json.dumps({"secrets": {"relay_token": {"set": True, "last4": "wxyz"}}}))
elif a[0] == "relay-test":
    j = json.loads(raw or "{}")
    if j.get("token") == "bad-token-bad-token-1": print(json.dumps({"error": "The voice service didn't accept the station token.", "checks": {"tls": True, "token": False}})); sys.exit(1)
    print(json.dumps({"ok": True, "checks": {"tls": True, "token": True, "xai_key": True}}))
elif a[0] == "env-get": print(json.dumps({"values": {"GROK_RELAY_URL": "wss://relay.example:8443/"}}))
elif a[0] == "net-status": print(json.dumps({"connected": True, "wired": False, "wifi": {"ssid": "home", "signal": 70}, "ips": {"wlan0": "192.0.2.5"}, "saved": [], "saved_in_range": True}))
elif a[0] == "settings-set": print(json.dumps({"ok": True, "restart_wake": a[1] == "audio"}))
else: print(json.dumps({"ok": True}))
''')
WAKE_CFG = os.path.join(TMP, "wakewords.json")
os.makedirs(os.path.join(TMP, "models"))
open(os.path.join(TMP, "models", "hey_finn.onnx"), "w").close()
json.dump({"models_dir": os.path.join(TMP, "models"), "words": [{"name": "hey_finn", "persona": "finn", "threshold": 0.5}],
           "enroll": [{"name": "hey_hank", "persona": "hank"}], "fallback": {"name": "hey_jarvis", "threshold": 0.5}}, open(WAKE_CFG, "w"))
os.environ.update(ADMIN_STATE_DIR=os.path.join(TMP, "state"), ADMIN_RUN_DIR=os.path.join(TMP, "run"), ADMIN_HELPER=f"{sys.executable} {HELPER}",
                  ADMIN_WAKE_CTL="http://127.0.0.1:9", WAKE_CONFIG=WAKE_CFG, WAKE_ENROLLED_DIR=os.path.join(TMP, "enrolled"),
                  ADMIN_PERSONAS=os.path.join(TMP, "personas.json"), FAKE_LOG=os.path.join(TMP, "helper.log"),
                  ADMIN_RELAY_ENV=os.path.join(TMP, "missing-admin-relay.env"))
os.makedirs(os.environ["ADMIN_STATE_DIR"]); os.makedirs(os.environ["ADMIN_RUN_DIR"])
sys.path.insert(0, os.path.join(ROOT, "admin"))
import grok_admin as ga  # noqa: E402
ga.EXIT = lambda code: None  # renaming would restart the service
RESTARTS = []
ga.restart_soon = lambda delay=2.0: RESTARTS.append(delay)

CLOCK = [time.time()]
ga.now = lambda: CLOCK[0]


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


PORT = free_port()
SRV = ga.ThreadingHTTPServer(("127.0.0.1", PORT), ga.Handler)
threading.Thread(target=SRV.serve_forever, daemon=True).start()


class C:
    """Tiny client with a cookie jar."""
    def __init__(self): self.jar = {}

    def req(self, method, p, body=None, csrf=True, ctype="application/json", raw=False):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        h = {}
        if self.jar:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.jar.items())
        data = None
        if method != "GET":
            data = json.dumps(body or {}).encode()
            h["Content-Type"] = ctype
            if csrf:
                h["X-GS-Admin"] = "1"
        c.request(method, p, data, h)
        r = c.getresponse()
        out = r.read()
        for k, v in r.getheaders():
            if k.lower() == "set-cookie":
                name, val = v.split(";")[0].split("=", 1)
                if val:
                    self.jar[name] = val
                else:
                    self.jar.pop(name, None)
        self.last = r
        if raw:
            return r.status, out
        try:
            return r.status, json.loads(out or b"{}")
        except ValueError:
            return r.status, {}


def setup_doc():
    ga.AUTH.publish()
    return json.load(open(os.path.join(os.environ["ADMIN_RUN_DIR"], "setup.json")))


def reset_state():
    for d in (os.environ["ADMIN_STATE_DIR"], os.environ["ADMIN_RUN_DIR"]):
        for n in os.listdir(d):
            os.unlink(os.path.join(d, n))
    ga.AUTH.__init__()


GOOD = "kitchen table lamp 42"


def signed_in():
    reset_state()
    c = C()
    code = setup_doc()["code"]
    assert c.req("POST", "/api/setup/code", {"code": code})[0] == 200
    assert c.req("POST", "/api/setup/password", {"password": GOOD})[0] == 200
    return c


class SetupCode(unittest.TestCase):
    def setUp(self): reset_state()

    def test_code_shape_and_published_for_display(self):
        d = setup_doc()
        self.assertEqual(d["mode"], "setup")
        self.assertRegex(d["code"], r"^[2-9A-HJ-NP-Z]{6}$")  # no 0/O/1/I
        self.assertNotRegex(d["code"], "[01OI]")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(os.environ["ADMIN_RUN_DIR"], "setup.json")).st_mode), 0o640)
        st, j = C().req("GET", "/api/state")
        self.assertTrue(j["setup_required"]) ; self.assertNotIn("code", json.dumps(j))  # the code is never sent to a browser

    def test_code_stable_within_boot_new_after_restart(self):
        a = setup_doc()["code"]
        self.assertEqual(setup_doc()["code"], a)
        ga.AUTH.__init__()  # new process, same tmpfs: keeps the code on screen
        self.assertEqual(setup_doc()["code"], a)
        os.unlink(os.path.join(os.environ["ADMIN_RUN_DIR"], "setup.json"))  # reboot clears /run
        ga.AUTH.__init__()
        self.assertRegex(setup_doc()["code"], r"^[2-9A-HJ-NP-Z]{6}$")  # fresh code after a reboot

    def test_no_default_password_login_refused_before_setup(self):
        for pw in ("", "admin", "password", "grok-station"):
            st, _ = C().req("POST", "/api/login", {"username": "admin", "password": pw})
            self.assertIn(st, (401, 429))
        self.assertFalse(os.path.exists(os.path.join(os.environ["ADMIN_STATE_DIR"], "auth.json")))

    def test_code_case_insensitive_spaces_ok_and_one_time(self):
        c = C()
        code = setup_doc()["code"]
        st, _ = c.req("POST", "/api/setup/code", {"code": f"{code[:3].lower()} {code[3:].lower()}"})
        self.assertEqual(st, 200)
        self.assertEqual(c.req("POST", "/api/setup/password", {"password": GOOD})[0], 200)
        self.assertFalse(os.path.exists(os.path.join(os.environ["ADMIN_RUN_DIR"], "setup.json")))  # display + wake unlock
        st, _ = C().req("POST", "/api/setup/code", {"code": code})
        self.assertEqual(st, 403)

    def test_password_needs_code_ticket(self):
        st, j = C().req("POST", "/api/setup/password", {"password": GOOD})
        self.assertEqual(st, 400)
        self.assertTrue(ga.AUTH.setup_required())

    def test_password_rules_and_hash_only(self):
        c = C()
        c.req("POST", "/api/setup/code", {"code": setup_doc()["code"]})
        for bad in ("short", "123456789", "aaaaaaaaaaaa", "x" * 300):
            st, j = c.req("POST", "/api/setup/password", {"password": bad})
            self.assertEqual(st, 400, bad)
        self.assertEqual(c.req("POST", "/api/setup/password", {"password": GOOD})[0], 200)
        f = os.path.join(os.environ["ADMIN_STATE_DIR"], "auth.json")
        raw = open(f).read()
        self.assertNotIn(GOOD, raw)
        rec = json.loads(raw)["password"]
        self.assertTrue({"salt", "hash", "n"} <= set(rec))
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        self.assertEqual(c.req("GET", "/api/home")[0], 200)  # signed in straight after


class Lockout(unittest.TestCase):
    def test_five_wrong_codes_lock_for_15_minutes(self):
        reset_state()
        code = setup_doc()["code"]
        wrong = "".join("A" if ch != "A" else "B" for ch in code)
        c = C()
        for i in range(5):
            st, _ = c.req("POST", "/api/setup/code", {"code": wrong})
        self.assertEqual(st, 429)
        self.assertEqual(c.req("POST", "/api/setup/code", {"code": code})[0], 429)  # even the right code
        CLOCK[0] += 901
        self.assertEqual(c.req("POST", "/api/setup/code", {"code": code})[0], 200)

    def test_five_wrong_passwords_lock(self):
        signed_in()
        c = C()
        for i in range(5):
            st, _ = c.req("POST", "/api/login", {"username": "admin", "password": "nope-nope-nope"})
        self.assertEqual(st, 429)
        self.assertEqual(c.req("POST", "/api/login", {"username": "admin", "password": GOOD})[0], 429)
        CLOCK[0] += 901
        self.assertEqual(c.req("POST", "/api/login", {"username": "admin", "password": GOOD})[0], 200)


class Sessions(unittest.TestCase):
    def test_signed_out_gets_401(self):
        signed_in()
        for p in ("/api/home", "/api/secrets", "/api/network", "/api/system", "/api/wakewords", "/api/preview"):
            self.assertEqual(C().req("GET", p)[0], 401, p)

    def test_cookie_flags(self):
        reset_state()
        c = C()
        c.req("POST", "/api/setup/code", {"code": setup_doc()["code"]})
        cookie = c.last.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie); self.assertIn("SameSite=Strict", cookie); self.assertIn("Secure", cookie)
        self.assertIn("max-age=31536000", c.last.getheader("Strict-Transport-Security"))

    def test_logout_and_password_change_end_sessions(self):
        c = signed_in()
        other = C(); other.req("POST", "/api/login", {"username": "admin", "password": GOOD})
        self.assertEqual(other.req("GET", "/api/home")[0], 200)
        st, _ = c.req("POST", "/api/password", {"old": GOOD, "new": "a brand new pass 7"})
        self.assertEqual(st, 200)
        self.assertEqual(other.req("GET", "/api/home")[0], 401)
        self.assertEqual(c.req("GET", "/api/home")[0], 200)
        c.req("POST", "/api/logout")
        self.assertEqual(c.req("GET", "/api/home")[0], 401)

    def test_wrong_old_password_refused(self):
        c = signed_in()
        self.assertEqual(c.req("POST", "/api/password", {"old": "wrong one!!", "new": "a brand new pass 7"})[0], 400)


class Csrf(unittest.TestCase):
    def test_writes_need_header_and_json(self):
        c = signed_in()
        self.assertEqual(c.req("POST", "/api/system/restart-voice", csrf=False)[0], 403)
        self.assertEqual(c.req("POST", "/api/system/restart-voice", ctype="text/plain")[0], 403)
        self.assertEqual(c.req("POST", "/api/logout", csrf=False)[0], 403)

    def test_security_headers(self):
        st, _ = C().req("GET", "/api/state")
        h = C(); h.req("GET", "/", raw=True)
        self.assertIn("frame-ancestors 'none'", h.last.getheader("Content-Security-Policy"))
        self.assertEqual(h.last.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(h.last.getheader("Cache-Control"), "no-store")


class LanOnly(unittest.TestCase):
    def test_lan_check(self):
        for ip in ("127.0.0.1", "192.168.77.20", "10.42.0.7", "172.16.3.4", "fe80::1", "::ffff:192.168.0.9", "fd00::5"):
            self.assertTrue(ga.lan_ok(ip), ip)
        for ip in ("8.8.8.8", "1.1.1.1", "2001:4860::8888", "::ffff:8.8.8.8", "junk"):
            self.assertFalse(ga.lan_ok(ip), ip)


class Secrets(unittest.TestCase):
    def test_write_only_last4(self):
        c = signed_in()
        tok = "abcdefghijklmnopqrstuvwxyz0123456789-SECRET"
        st, j = c.req("POST", "/api/secrets/relay_token", {"value": tok, "url": "wss://relay.example:8443/"})
        self.assertEqual(st, 200, j)
        st, j = c.req("GET", "/api/secrets")
        s = json.dumps(j)
        self.assertNotIn(tok, s); self.assertNotIn("SECRET", s)
        self.assertEqual(j["relay_token"]["last4"], "wxyz")  # live last4 from the helper
        self.assertTrue(j["relay_token"]["set"])
        meta = open(os.path.join(os.environ["ADMIN_STATE_DIR"], "secrets.json")).read()
        self.assertNotIn(tok, meta)
        self.assertIn("CRET", meta)  # last 4 only

    def test_bad_token_not_saved(self):
        c = signed_in()
        open(os.environ["FAKE_LOG"], "w").close()
        st, j = c.req("POST", "/api/secrets/relay_token", {"value": "bad-token-bad-token-1"})
        self.assertEqual(st, 400)
        self.assertIn("didn't accept", j["error"])
        log = [json.loads(l)["argv"][0] for l in open(os.environ["FAKE_LOG"])]
        self.assertNotIn("relay-save", log)  # old token keeps working

    def test_validation_and_xai_key_lives_on_relay(self):
        c = signed_in()
        self.assertEqual(c.req("POST", "/api/secrets/relay_token", {"value": "short"})[0], 400)
        self.assertEqual(c.req("POST", "/api/secrets/relay_token", {"url": "http://evil.example/"})[0], 400)
        st, j = c.req("POST", "/api/secrets/xai_key", {"value": "xai-123"})
        self.assertEqual(st, 409)
        self.assertIn("voice service computer", c.req("GET", "/api/secrets")[1]["xai_key"]["where"])

    def test_no_secrets_in_request_log(self):
        out = []
        import builtins
        real = builtins.print
        builtins.print = lambda *a, **k: out.append(" ".join(map(str, a)))
        try:
            c = signed_in()
            c.req("POST", "/api/secrets/relay_token", {"value": "abcdefghijklmnopqrstuvwxyz-LOGTEST"})
            c.req("POST", "/api/login", {"username": "admin", "password": "pw-in-log-test"})
        finally:
            builtins.print = real
        blob = "\n".join(out)
        self.assertNotIn("LOGTEST", blob); self.assertNotIn("pw-in-log-test", blob); self.assertNotIn(GOOD, blob)


class WakeWordsAndSpeakers(unittest.TestCase):
    def test_overrides(self):
        c = signed_in()
        words = {w["name"]: w for w in c.req("GET", "/api/wakewords")[1]["words"]}
        self.assertTrue(words["hey_finn"]["enabled"]); self.assertFalse(words["hey_hank"]["has_model"])
        self.assertTrue(words["hey_jarvis"]["backup"])
        self.assertEqual(c.req("PUT", "/api/wakewords/hey_finn", {"enabled": False, "sensitivity": 0.7})[0], 200)
        ov = json.load(open(os.path.join(os.environ["ADMIN_STATE_DIR"], "wake-overrides.json")))
        self.assertEqual(ov["hey_finn"], {"enabled": False, "threshold": 0.3})
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(os.environ["ADMIN_STATE_DIR"], "wake-overrides.json")).st_mode), 0o640)
        self.assertEqual(c.req("PUT", "/api/wakewords/hey_hank", {"enabled": True})[0], 409)  # no model yet
        code, out = c.req("PUT", "/api/wakewords/hey_jarvis", {"enabled": False})   # default word can't be turned off
        self.assertEqual((code, out["error"]), (409, "Jarvis is the station's default wake word and can't be turned off"))
        self.assertTrue(next(w for w in c.req("GET", "/api/wakewords")[1]["words"] if w["name"] == "hey_jarvis")["enabled"])
        self.assertEqual(c.req("PUT", "/api/wakewords/hey_finn", {"sensitivity": 2})[0], 400)

    def test_speaker_names_stay_on_pi(self):
        c = signed_in()
        st, j = c.req("POST", "/api/speakers", {"name": "  Alex   Example "})
        self.assertEqual(st, 200)
        self.assertRegex(j["id"], r"^spk-[0-9a-f]{4}$")
        sent = []
        ga_wake = ga.wake
        ga.wake = lambda m, p, body=None, timeout=3: (sent.append(body), (202, {"ok": True}))[1]
        try:
            st, _ = c.req("POST", "/api/enroll", {"phrase": "hey_hank", "count": 10, "speaker": j["id"]})
        finally:
            ga.wake = ga_wake
        self.assertEqual(st, 202)
        self.assertNotIn("Alex", json.dumps(sent))  # nothing about the speaker leaves for training
        self.assertNotIn(j["id"], json.dumps(sent))
        sp = json.load(open(os.path.join(os.environ["ADMIN_STATE_DIR"], "speakers.json")))
        self.assertEqual(sp[j["id"]]["name"], "Alex Example")
        self.assertEqual(c.req("POST", "/api/enroll", {"phrase": "hey_hank", "count": 5, "speaker": j["id"]})[0], 400)
        self.assertEqual(c.req("POST", "/api/enroll", {"phrase": "Hey Hank!", "count": 10})[0], 400)


class SystemAndSettings(unittest.TestCase):
    def test_reboot_needs_confirm_update_needs_sha(self):
        c = signed_in()
        self.assertEqual(c.req("POST", "/api/system/reboot", {})[0], 400)
        self.assertEqual(c.req("POST", "/api/system/reboot", {"confirm": True})[0], 202)
        self.assertEqual(c.req("POST", "/api/system/update", {"sha": "main; rm -rf /"})[0], 400)

    def test_busy_guard(self):
        c = signed_in()
        old = ga.wake
        ga.wake = lambda m, p, body=None, timeout=3: (200, {"session": True, "enrolling": False})
        try:
            self.assertEqual(c.req("POST", "/api/system/restart-voice")[0], 409)
            self.assertEqual(c.req("POST", "/api/system/reboot", {"confirm": True})[0], 409)
            self.assertEqual(c.req("POST", "/api/system/update", {"sha": "a" * 40})[0], 409)
        finally:
            ga.wake = old

    def test_logs_filter_allowlisted(self):
        c = signed_in()
        self.assertEqual(c.req("GET", "/api/system/logs?part=sshd")[0], 400)
        self.assertEqual(c.req("GET", "/api/system/logs?part=grok-station-wake&since=forever")[0], 400)

    def test_hostname_validation(self):
        c = signed_in()
        for bad in ("-x", "a_b", "x" * 40, "kitchen;reboot"):
            self.assertEqual(c.req("POST", "/api/network/hostname", {"hostname": bad})[0], 400, bad)
        self.assertEqual(c.req("POST", "/api/network/hostname", {"hostname": "kitchen"})[0], 200)

    def test_wifi_join_validation(self):
        c = signed_in()
        self.assertEqual(c.req("POST", "/api/network/join", {"ssid": "", "password": "12345678"})[0], 400)
        self.assertEqual(c.req("POST", "/api/network/join", {"ssid": "home", "password": "short"})[0], 400)
        self.assertEqual(c.req("POST", "/api/network/join", {"ssid": "home", "password": "longenough"})[0], 200)

    def test_no_daily_limits_anywhere(self):
        blob = "".join(open(os.path.join(ROOT, p)).read() for p in ("admin/grok_admin.py", "admin/web/app.js", "admin/grok-station-helper"))
        self.assertNotRegex(blob, r"(?i)MAX_MINUTES_DAY|MAX_SESSIONS_DAY|per day|daily limit|minutes/day")

    def test_wizard_state(self):
        c = signed_in()
        self.assertEqual(c.req("GET", "/api/state")[1]["wizard"], {"step": 2, "done": False})
        self.assertEqual(c.req("POST", "/api/wizard", {"step": 9})[0], 400)
        c.req("POST", "/api/wizard", {"step": 5})
        self.assertEqual(c.req("GET", "/api/state")[1]["wizard"]["step"], 5)  # resume at the same step


class Reset(unittest.TestCase):
    def cli(self, *args, boot_dir=None):
        src = open(os.path.join(ROOT, "tools", "grok-station")).read()
        src = src.replace("/var/lib/grok-station/admin/auth.json", os.path.join(os.environ["ADMIN_STATE_DIR"], "auth.json"))
        src = src.replace("/run/grok-station-admin/setup.json", os.path.join(os.environ["ADMIN_RUN_DIR"], "setup.json"))
        src = src.replace('[ "$(id -u)" = 0 ]', "true").replace("systemctl restart grok-station-admin", "true").replace("logger ", "true ")
        src = src.replace("/usr/bin/python3", sys.executable)
        if boot_dir:
            src = src.replace("/boot/firmware/grok-station-reset /boot/grok-station-reset", f"{boot_dir}/grok-station-reset")
        f = os.path.join(TMP, "cli.sh"); open(f, "w").write(src)
        return subprocess.run(["sh", f, *args], capture_output=True, text=True)

    def check_reset(self, c):
        self.assertTrue(ga.AUTH.setup_required())
        d = setup_doc()
        self.assertEqual(d["mode"], "reset")
        self.assertEqual(c.req("GET", "/api/home")[0], 401)  # old sessions are dead
        c2 = C()
        self.assertEqual(c2.req("POST", "/api/setup/code", {"code": d["code"]})[0], 200)
        self.assertEqual(c2.req("POST", "/api/setup/password", {"password": "after reset pass 9"})[0], 200)
        st, j = c2.req("GET", "/api/state")
        self.assertEqual(j["wizard"], {"step": 8, "done": True})  # other settings (and the finished wizard) kept
        self.assertTrue(os.path.exists(os.path.join(os.environ["ADMIN_STATE_DIR"], "speakers.json")))

    def prep(self):
        c = signed_in()
        c.req("POST", "/api/wizard", {"step": 8, "done": True})
        c.req("POST", "/api/speakers", {"name": "Keep Me"})
        return c

    def test_cli_reset_admin_wipes_only_password(self):
        c = self.prep()
        r = self.cli("reset-admin")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.check_reset(c)

    def test_boot_partition_file_deleted_after_use(self):
        c = self.prep()
        boot = os.path.join(TMP, "boot"); os.makedirs(boot, exist_ok=True)
        open(os.path.join(boot, "grok-station-reset"), "w").close()
        r = self.cli("boot-check", boot_dir=boot)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(boot, "grok-station-reset")))
        self.check_reset(c)

    def test_boot_check_without_file_does_nothing(self):
        self.prep()
        boot = os.path.join(TMP, "boot2"); os.makedirs(boot, exist_ok=True)
        self.cli("boot-check", boot_dir=boot)
        self.assertFalse(ga.AUTH.setup_required())


class Static(unittest.TestCase):
    def test_spa_files_and_traversal(self):
        for p, t in (("/", "text/html"), ("/app.js", "text/javascript"), ("/admin.css", "text/css"),
                     ("/assets/mark.svg", "image/svg+xml"), ("/favicon.ico", "image/x-icon"),
                     ("/assets/favicon.ico", "image/x-icon")):
            c = C(); st, body = c.req("GET", p, raw=True)
            self.assertEqual(st, 200, p); self.assertTrue(c.last.getheader("Content-Type").startswith(t), (p, c.last.getheader("Content-Type")))
            self.assertGreater(len(body), 16, p)
        for p in ("/assets/../grok_admin.py", "/assets/..%2Fgrok_admin.py", "/grok_admin.py", "/etc/passwd"):
            self.assertEqual(C().req("GET", p, raw=True)[0], 404, p)

    def test_brand_title_and_icons(self):
        st, body = C().req("GET", "/", raw=True)
        self.assertEqual(st, 200)
        self.assertIn(b"<title>Grok Station - Admin</title>", body)
        self.assertIn(b'name="theme-color" content="#0D0B09"', body)
        for needle in (b"favicon.svg", b"favicon-32.png", b"favicon-16.png", b"favicon-180.png", b"apple-touch-icon"):
            self.assertIn(needle, body)
        for p, t in (("/assets/favicon.svg", "image/svg+xml"), ("/assets/favicon-32.png", "image/png"),
                     ("/assets/favicon-16.png", "image/png"), ("/assets/favicon-180.png", "image/png")):
            c = C(); st, raw = c.req("GET", p, raw=True)
            self.assertEqual(st, 200, p)
            self.assertTrue(c.last.getheader("Content-Type").startswith(t), (p, c.last.getheader("Content-Type")))
            self.assertGreater(len(raw), 16, p)
        js = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        css = open(os.path.join(ROOT, "admin", "web", "admin.css")).read()
        self.assertIn("Grok Station", js)
        self.assertIn('/assets/mark.svg', js)
        self.assertNotIn("grok.png", js)
        self.assertNotIn("grok.png", css)
        self.assertIn("mark.svg", css)

    def test_old_mark_files_are_gone(self):
        """*grok*.png, *orbit*, and the old Grok mark names must not be in the tree."""
        old = {"grok.png", "grok-mark.svg", "grok-src.svg", "grok.plymouth", "grok.script"}
        bad = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", ".worktrees")]
            for name in filenames:
                low = name.lower()
                if (low.endswith(".png") and "grok" in low) or "orbit" in low or low in old:
                    bad.append(os.path.join(dirpath, name))
        self.assertEqual(bad, [])

    def test_spa_keeps_secrets_out_of_storage(self):
        js = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        self.assertNotRegex(js, r"localStorage|sessionStorage|document\.cookie|indexedDB")
        self.assertIn('"X-GS-Admin": "1"', js)


class DisplaySetupScreens(unittest.TestCase):
    def test_setup_file_parsing(self):
        sys.path.insert(0, os.path.join(ROOT, "app"))
        os.environ["GROK_SETUP_FILE"] = os.path.join(TMP, "setup-d.json")
        try:
            import importlib, grok_station as gs
            importlib.reload(gs)
        except ImportError:
            self.skipTest("pygame missing")
        self.assertIsNone(gs.read_setup())
        json.dump({"mode": "setup", "code": "K7P4QX"}, open(os.environ["GROK_SETUP_FILE"], "w"))
        self.assertEqual(gs.read_setup()["code"], "K7P4QX")
        json.dump({"mode": "setup", "code": "K7P4Q0"}, open(os.environ["GROK_SETUP_FILE"], "w"))  # 0 isn't in the alphabet
        self.assertIsNone(gs.read_setup())
        json.dump({"mode": "nope", "code": "K7P4QX"}, open(os.environ["GROK_SETUP_FILE"], "w"))
        self.assertIsNone(gs.read_setup())


class Bots(unittest.TestCase):
    """Bots page: proxied to the relay (faked here); password re-entry; keys never kept, logged or echoed on the Pi."""
    URL, KEY = "https://api2.cursor.sh/v1/hooks/abc123", "sk-test-0123456789-WXYZ"

    def setUp(self):
        self.c = signed_in()
        self.calls = []
        self._rc = ga.relay_call
        def fake(method, path, payload=None, timeout=10):
            self.calls.append((method, path, payload))
            if path == "/admin/bots" and method == "GET":
                return 200, {"bots": [{"slug": "kai", "name": "Kai", "voice": "eve", "source": "registry",
                                       "webhook": {"configured": True, "last4": "WXYZ", "saved_at": 1},
                                       "status": {"state": "connected", "last_answered_at": 1, "last_error": None, "last_error_at": None}}],
                             "voices": ["eve"]}
            if path == "/admin/bots" and method == "POST":
                return 201, {"ok": True, "slug": payload["slug"], "voice_token": "vt_" + "A" * 43}
            if path.endswith("/webhook") and method == "PUT":
                return 200, {"ok": True, "last4": payload["key"][-4:]}
            if path.endswith("/test") and method == "POST":
                return 202, {"test_id": "t1abc"}
            return 200, {"ok": True}
        ga.relay_call = fake

    def tearDown(self):
        ga.relay_call = self._rc

    def disk_blob(self):
        out = b""
        for d in (os.environ["ADMIN_STATE_DIR"], os.environ["ADMIN_RUN_DIR"]):
            for n in os.listdir(d):
                try:
                    out += open(os.path.join(d, n), "rb").read()
                except OSError:
                    pass
        return out

    def test_list_proxied(self):
        st, j = self.c.req("GET", "/api/bots")
        self.assertEqual(st, 200); self.assertEqual(j["bots"][0]["webhook"]["last4"], "WXYZ")

    def test_save_needs_password_and_key_is_not_kept(self):
        body = {"url": self.URL, "key": self.KEY}
        self.assertEqual(self.c.req("PUT", "/api/bots/kai/webhook", body)[0], 403)            # no password
        self.assertEqual(self.c.req("PUT", "/api/bots/kai/webhook", dict(body, password="wrong-wrong-1"))[0], 403)
        self.assertEqual(len([c for c in self.calls if c[0] == "PUT"]), 0)                    # relay never called
        st, j = self.c.req("PUT", "/api/bots/kai/webhook", dict(body, password=GOOD))
        self.assertEqual(st, 200); self.assertNotIn(self.KEY, json.dumps(j))
        self.assertNotIn(self.KEY.encode(), self.disk_blob()); self.assertNotIn(self.URL.encode(), self.disk_blob())

    def test_url_allowlist_rejected_locally(self):
        for bad in ("http://api2.cursor.sh/x", "https://api2.cursor.sh.evil.example/x", "https://u:p@api2.cursor.sh/x",
                    "https://api2.cursor.sh:8443/x", "https://example.com/x"):
            st, j = self.c.req("PUT", "/api/bots/kai/webhook", {"url": bad, "key": self.KEY, "password": GOOD})
            self.assertEqual(st, 400, bad); self.assertNotIn(self.KEY, json.dumps(j))
        self.assertEqual(len([c for c in self.calls if c[0] == "PUT"]), 0)

    def test_add_rename_remove_need_password_token_once(self):
        self.assertEqual(self.c.req("POST", "/api/bots", {"slug": "zed", "name": "Zed", "voice": "eve"})[0], 403)
        st, j = self.c.req("POST", "/api/bots", {"slug": "zed", "name": "Zed", "voice": "eve", "password": GOOD})
        self.assertIn(st, (200, 201)); self.assertTrue(j["voice_token"].startswith("vt_"))
        self.assertNotIn(j["voice_token"].encode(), self.disk_blob())
        self.assertEqual(self.c.req("PATCH", "/api/bots/zed", {"name": "Zane"})[0], 403)
        self.assertEqual(self.c.req("PATCH", "/api/bots/zed", {"name": "Zane", "password": GOOD})[0], 200)
        self.assertEqual(self.c.req("DELETE", "/api/bots/zed", {"password": GOOD})[0], 200)

    def test_test_needs_no_password_and_unconfigured_message(self):
        st, j = self.c.req("POST", "/api/bots/kai/test", {})
        self.assertEqual((st, j.get("test_id")), (202, "t1abc"))
        ga.relay_call = lambda *a, **k: None
        st, j = self.c.req("GET", "/api/bots")
        self.assertEqual(st, 200); self.assertEqual(j["message"], ga.RELAY_UNCONFIGURED)

    def test_page_has_bots_and_no_personas_or_t1_ui(self):
        js = open(os.path.join(os.path.dirname(__file__), "..", "admin", "web", "app.js")).read()
        self.assertIn('["bots", "Bots"]', js); self.assertNotIn('["personas"', js)
        self.assertIn('autocomplete="off"', js); self.assertIn("Waiting for", js)
        for s in ("Voice note", "yes, save it", "call button"):
            self.assertNotIn(s, js)


if __name__ == "__main__":
    unittest.main()
