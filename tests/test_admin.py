"""Admin page API (admin/grok_admin.py): setup code, password rules, lockout, sessions, CSRF, LAN-only, write-only
secrets, wake-word overrides, Pi-only speakers, reset (CLI + boot file), static files. In-process server on a free port;
the root helper and wake listener are fakes, so nothing touches the system. No browser."""
import http.client, io, json, os, re, shutil, socket, stat, subprocess, sys, tempfile, threading, time, unittest
from contextlib import redirect_stdout

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
open(os.path.join(TMP, "models", "hey_grok.onnx"), "w").close()
json.dump({"models_dir": os.path.join(TMP, "models"), "words": [{"name": "hey_grok", "persona": "grok", "threshold": 0.5},
           {"name": "hey_finn", "persona": "finn", "threshold": 0.5}],
           "enroll": [{"name": "hey_hank", "persona": "hank"}]}, open(WAKE_CFG, "w"))
os.environ.update(ADMIN_STATE_DIR=os.path.join(TMP, "state"), ADMIN_RUN_DIR=os.path.join(TMP, "run"), ADMIN_HELPER=f"{sys.executable} {HELPER}",
                  ADMIN_WAKE_CTL="http://127.0.0.1:9", WAKE_CONFIG=WAKE_CFG, WAKE_ENROLLED_DIR=os.path.join(TMP, "enrolled"),
                  ADMIN_PERSONAS=os.path.join(TMP, "personas.json"), FAKE_LOG=os.path.join(TMP, "helper.log"),
                  ADMIN_RELAY_ENV=os.path.join(TMP, "missing-admin-relay.env"),
                  ADMIN_XAI_KEY_FILE=os.path.join(TMP, "xai_api_key"))
open(os.environ["ADMIN_XAI_KEY_FILE"], "w").write("set\n")  # most tests: key already set
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

    def req(self, method, p, body=None, csrf=True, ctype="application/json", raw=False, data=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        h = {}
        if self.jar:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.jar.items())
        payload = None
        if method != "GET":
            payload = data if data is not None else json.dumps(body or {}).encode()
            h["Content-Type"] = ctype
            if csrf:
                h["X-GS-Admin"] = "1"
        if headers:
            h.update(headers)
        c.request(method, p, payload, h)
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
            p = os.path.join(d, n)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.unlink(p)
    ga.AUTH.__init__()
    ga.reset_pair_runtime()


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
    def test_relay_token_and_xai_key_routes_are_gone(self):
        c = signed_in()
        tok = "abcdefghijklmnopqrstuvwxyz0123456789-SECRET"
        st, j = c.req("POST", "/api/secrets/relay_token", {"value": tok, "url": "wss://relay.example.invalid:8443/"})
        self.assertEqual(st, 404, j)
        st, j = c.req("POST", "/api/secrets/xai_key", {"value": "xai-123"})
        self.assertEqual(st, 404, j)
        st, j = c.req("GET", "/api/secrets")
        self.assertEqual(st, 200)
        self.assertNotIn("relay_token", j)
        self.assertNotIn("relay_url", j)
        self.assertNotIn("xai_key", j)
        self.assertNotIn(tok, json.dumps(j))
        self.assertFalse(os.path.exists(os.path.join(os.environ["ADMIN_STATE_DIR"], "secrets.json")))

    def test_removed_save_does_not_call_helper(self):
        c = signed_in()
        open(os.environ["FAKE_LOG"], "w").close()
        self.assertEqual(c.req("POST", "/api/secrets/relay_token", {"value": "bad-token-bad-token-1"})[0], 404)
        log = open(os.environ["FAKE_LOG"]).read()
        self.assertNotIn("relay-save", log)

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
        self.assertTrue(words["hey_grok"]["builtin"]); self.assertNotIn("hey_jarvis", words)
        self.assertEqual(c.req("PUT", "/api/wakewords/hey_finn", {"enabled": False, "sensitivity": 0.7})[0], 200)
        ov = json.load(open(os.path.join(os.environ["ADMIN_STATE_DIR"], "wake-overrides.json")))
        self.assertEqual(ov["hey_finn"], {"enabled": False, "threshold": 0.3})
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(os.environ["ADMIN_STATE_DIR"], "wake-overrides.json")).st_mode), 0o640)
        self.assertEqual(c.req("PUT", "/api/wakewords/hey_hank", {"enabled": True})[0], 409)  # no model yet
        code, out = c.req("PUT", "/api/wakewords/hey_grok", {"enabled": False})   # built-in word can't be turned off
        self.assertEqual((code, out["error"]), (409, "Hey Grok is the station's built-in wake word and can't be turned off"))
        self.assertTrue(next(w for w in c.req("GET", "/api/wakewords")[1]["words"] if w["name"] == "hey_grok")["enabled"])
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
            st, _ = c.req("POST", "/api/enroll", {"phrase": "hey_hank", "count": 25, "speaker": j["id"]})
        finally:
            ga.wake = ga_wake
        self.assertEqual(st, 202)
        self.assertNotIn("Alex", json.dumps(sent))  # nothing about the speaker leaves for training
        self.assertNotIn(j["id"], json.dumps(sent))
        sp = json.load(open(os.path.join(os.environ["ADMIN_STATE_DIR"], "speakers.json")))
        self.assertEqual(sp[j["id"]]["name"], "Alex Example")
        models = json.load(open(os.path.join(os.environ["ADMIN_STATE_DIR"], "models.json")))
        self.assertEqual(models["hey_hank"]["speakers"], [j["id"]])
        self.assertNotIn("Alex", json.dumps(models))
        self.assertEqual(c.req("POST", "/api/enroll", {"phrase": "hey_hank", "count": 5, "speaker": j["id"]})[0], 400)
        # "Hey Hank!" is the known word hey_hank, so this re-records it (the listener is down here).
        st, out = c.req("POST", "/api/enroll", {"phrase": "Hey Hank!", "count": 25})
        self.assertEqual((st, out.get("id")), (503, "hey_hank"))


class SystemAndSettings(unittest.TestCase):
    def test_reboot_needs_confirm_and_old_update_is_gone(self):
        c = signed_in()
        self.assertEqual(c.req("POST", "/api/system/reboot", {})[0], 400)
        self.assertEqual(c.req("POST", "/api/system/reboot", {"confirm": True})[0], 202)
        self.assertEqual(c.req("GET", "/api/system/update")[0], 404)
        self.assertEqual(c.req("POST", "/api/system/update", {"sha": "a" * 40})[0], 404)

    def test_busy_guard(self):
        c = signed_in()
        old = ga.wake
        ga.wake = lambda m, p, body=None, timeout=3: (200, {"session": True, "enrolling": False})
        try:
            self.assertEqual(c.req("POST", "/api/system/restart-voice")[0], 409)
            self.assertEqual(c.req("POST", "/api/system/reboot", {"confirm": True})[0], 409)
            self.assertEqual(c.req("POST", "/api/system/update", {"sha": "a" * 40})[0], 404)
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
        self.assertEqual(c.req("GET", "/api/state")[1]["wizard"], {"step": 3, "done": False})
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

    def test_default_avatar_route_is_read_only_and_strict(self):
        for n in (1, 9, 18):
            p = "/avatars/default-%02d.svg" % n
            c = C(); st, body = c.req("GET", p, raw=True)
            self.assertEqual(st, 200, p)
            self.assertTrue(c.last.getheader("Content-Type").startswith("image/svg+xml"), c.last.getheader("Content-Type"))
            self.assertIn(b"<svg", body)
        for p in ("/avatars/default-00.svg", "/avatars/default-19.svg", "/avatars/default-1.svg",
                  "/avatars/default-01.png", "/avatars/default-01.SVG", "/avatars/default-01.svg/",
                  "/avatars/../default-01.svg", "/avatars/%2e%2e/default-01.svg", "/avatars/default-01.svg%00",
                  "/avatars/", "/avatars", "/avatars/default-01.svg/../../etc/passwd"):
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
        # the add's password opened step-up: rename passes without re-entry, remove still prompts
        self.assertEqual(self.c.req("PATCH", "/api/bots/zed", {"name": "Zane"})[0], 200)
        self.assertEqual(self.c.req("DELETE", "/api/bots/zed", {})[0], 403)
        self.assertEqual(self.c.req("DELETE", "/api/bots/zed", {"password": GOOD})[0], 200)

    def test_voice_patch_forwards_with_the_rename_stepup(self):
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"voice": "not a voice"})[0], 400)
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {})[0], 400)
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"voice": "eve"})[0], 403)
        self.assertFalse(any(c[0] == "PATCH" for c in self.calls))
        st, j = self.c.req("PATCH", "/api/bots/kai", {"voice": "eve", "password": GOOD})
        self.assertEqual(st, 200, j)
        self.assertEqual(self.calls[-1], ("PATCH", "/admin/bots/kai", {"voice": "eve", "actor": "admin"}))
        st, j = self.c.req("PATCH", "/api/bots/kai", {"name": "Kay", "voice": "rex"})
        self.assertEqual(st, 200, j)
        self.assertEqual(self.calls[-1], ("PATCH", "/admin/bots/kai", {"name": "Kay", "voice": "rex", "actor": "admin"}))

    def test_test_needs_no_password_and_unconfigured_message(self):
        st, j = self.c.req("POST", "/api/bots/kai/test", {})
        self.assertEqual((st, j.get("test_id")), (202, "t1abc"))
        ga.relay_call = lambda *a, **k: None
        st, j = self.c.req("GET", "/api/bots")
        self.assertEqual(st, 200); self.assertEqual(j["message"], ga.RELAY_UNCONFIGURED)

    def test_page_has_bots_and_no_personas_or_t1_ui(self):
        js = open(os.path.join(os.path.dirname(__file__), "..", "admin", "web", "app.js")).read()
        self.assertIn('["bots", "Bots"]', js); self.assertNotIn('["personas"', js)
        self.assertIn("Refresh avatars", js); self.assertIn("/api/bots/refresh-avatars", js)
        self.assertIn("Unlocked for ", js); self.assertIn("?v=", js)
        self.assertNotIn("GROK_STATION_LAT", js); self.assertNotIn("GROK_STATION_LON", js)
        self.assertIn('autocomplete="off"', js); self.assertIn("Waiting for", js)
        for s in ("Voice note", "yes, save it", "call button"):
            self.assertNotIn(s, js)
        self.assertIn("data-voice", js)
        self.assertIn("openVoiceStatus", js)
        self.assertIn('"/api/enroll/check"', js)
        self.assertIn('"/api/enroll/status"', js)

    def test_refresh_avatars_proxies_wake_without_password(self):
        calls = []
        def fake(method, p, body=None, timeout=3):
            calls.append((method, p, body))
            return (202, {"running": True}) if len(calls) > 1 else (202, {"started": True})
        old = ga.wake
        ga.wake = fake
        try:
            st, j = self.c.req("POST", "/api/bots/refresh-avatars", {})
            self.assertEqual((st, j), (202, {"started": True}))
            st, j = self.c.req("POST", "/api/bots/refresh-avatars", {})
            self.assertEqual((st, j), (202, {"running": True}))
        finally:
            ga.wake = old
        self.assertEqual(calls, [("POST", "/personas/sync", {}), ("POST", "/personas/sync", {})])
        self.assertEqual(C().req("POST", "/api/bots/refresh-avatars", {})[0], 401)




class WakePhraseAndStatus(unittest.TestCase):
    """Custom wake phrase: normalization, quality checks, persona mapping, training status."""

    def setUp(self):
        reset_state()
        if os.path.exists(ga.ENROLLED):
            os.remove(ga.ENROLLED)
        self.wake = ga.wake
        self.sent = []

    def tearDown(self):
        ga.wake = self.wake
        if os.path.exists(ga.ENROLLED):
            os.remove(ga.ENROLLED)

    def enroll_up(self, replies):
        """Wake listener stand-in. `replies` is consumed in order; the last one sticks."""
        box = {"i": 0}

        def fake(method, p, body=None, timeout=3):
            self.sent.append((method, p, body))
            i = min(box["i"], len(replies) - 1)
            box["i"] += 1
            return replies[i]

        ga.wake = fake

    def test_normalize_phrase(self):
        self.assertEqual(ga.normalize_phrase("  Hey   Kitchen! "), ("hey_kitchen", ["hey", "kitchen"]))
        self.assertEqual(ga.normalize_phrase("Hey, Kitchen"), ("hey_kitchen", ["hey", "kitchen"]))
        self.assertEqual(ga.normalize_phrase("OKAY FINN"), ("okay_finn", ["okay", "finn"]))
        self.assertEqual(ga.normalize_phrase("hey  kitchen's  light"), ("hey_kitchens_light", ["hey", "kitchens", "light"]))
        self.assertEqual(ga.normalize_phrase("hey_hank"), ("hey_hank", ["hey", "hank"]))  # id the Record button posts
        self.assertEqual(ga.normalize_phrase("don\u2019t stop"), ("dont_stop", ["dont", "stop"]))
        self.assertEqual(ga.normalize_phrase(""), ("", []))
        self.assertTrue(ga.phrase_ok("hey_finn"))
        self.assertTrue(ga.phrase_ok("okay_finn"))
        self.assertFalse(ga.phrase_ok("hey"))
        self.assertLessEqual(len("hey_kitchen"), ga.PHRASE_MAX)

    def test_each_quality_reason(self):
        self.assertEqual(ga.phrase_problem("cat"), ga.TOO_SHORT)
        self.assertEqual(ga.phrase_problem("kitchen"), ga.TOO_SHORT)          # one word
        self.assertEqual(ga.phrase_problem("hi bob"), ga.TOO_SHORT)           # two words, five letters
        self.assertEqual(ga.phrase_problem("hey"), "“Hey” is only common words like “hey” or “okay”. Add a word that's yours.")
        self.assertEqual(ga.phrase_problem("okay"), "“Okay” is only common words like “hey” or “okay”. Add a word that's yours.")
        self.assertEqual(ga.phrase_problem("hey okay"), "“Hey Okay” is only common words like “hey” or “okay”. Add a word that's yours.")
        self.assertEqual(ga.phrase_problem("alpha beta gamma delta epsilon"), ga.TOO_LONG)
        self.assertEqual(ga.phrase_problem("extraordinarily supercalifragilistic"), ga.TOO_LONG_ID)
        self.assertEqual(ga.phrase_problem("Hey Finn!"), "“Hey Finn” is already a wake word.")
        self.assertEqual(ga.phrase_problem("hey_grok"), "“Hey Grok” is already a wake word.")
        self.assertEqual(ga.phrase_problem("hey fin"), "Too close to “Hey Finn”. Pick something that sounds different.")
        # turned off on the admin page, still a known word
        ga.write_json(ga.path("wake-overrides.json"), {"hey_finn": {"enabled": False, "threshold": 0.3}}, 0o640)
        self.assertEqual(ga.phrase_problem("hey fin"), "Too close to “Hey Finn”. Pick something that sounds different.")
        self.assertIsNone(ga.phrase_problem("okay finn"))
        self.assertIsNone(ga.phrase_problem("Hey Kitchen!"))
        c = signed_in()
        st, j = c.req("POST", "/api/enroll/check", {"phrase": "Hey Kitchen!"})
        self.assertEqual((st, j), (200, {"ok": True, "id": "hey_kitchen", "label": "Hey Kitchen"}))
        st, j = c.req("POST", "/api/enroll/check", {"phrase": "hey fin"})
        self.assertEqual((st, j["error"]), (400, "Too close to “Hey Finn”. Pick something that sounds different."))
        st, j = c.req("POST", "/api/enroll", {"phrase": "cat", "count": 5, "persona": "grok"})
        self.assertEqual((st, j["error"]), (400, "Pick a wake word and 20 to 30 clips."))

    def test_mapping_saved_and_rerecord_keeps_persona(self):
        c = signed_in()
        st, sp = c.req("POST", "/api/speakers", {"name": "  Alex   Example "})
        self.enroll_up([(202, {"ok": True}), (503, {"error": "wake listener unreachable"})])
        CLOCK[0] = 1_700_000_000
        st, j = c.req("POST", "/api/enroll", {"phrase": "Hey Kitchen!", "count": 25, "persona": "kitchen-bot",
                                              "speaker": sp["id"]})
        self.assertEqual(st, 202)
        self.assertEqual(j["id"], "hey_kitchen")
        self.assertEqual(self.sent[-1][0:2], ("POST", "/enroll"))
        self.assertEqual(self.sent[-1][2], {"phrase": "hey_kitchen", "count": 25, "topup": False, "persona": "kitchen-bot"})
        self.assertNotIn("Alex", json.dumps(self.sent))
        self.assertNotIn(sp["id"], json.dumps(self.sent))
        ov_path = os.path.join(os.environ["ADMIN_STATE_DIR"], "wake-overrides.json")
        ov = json.load(open(ov_path))
        self.assertEqual(ov["hey_kitchen"], {"persona": "kitchen-bot"})
        self.assertEqual(stat.S_IMODE(os.stat(ov_path).st_mode), 0o640)
        words = {w["name"]: w for w in c.req("GET", "/api/wakewords")[1]["words"]}
        self.assertEqual(words["hey_kitchen"]["persona"], "kitchen-bot")
        doc = json.load(open(os.path.join(os.environ["ADMIN_STATE_DIR"], "enroll-status.json")))
        self.assertEqual(doc["hey_kitchen"]["state"], "recording")
        self.assertEqual(doc["hey_kitchen"]["persona"], "kitchen-bot")
        self.assertFalse(doc["hey_kitchen"]["had_model"])
        models = json.load(open(os.path.join(os.environ["ADMIN_STATE_DIR"], "models.json")))
        self.assertEqual(models["hey_kitchen"]["speakers"], [sp["id"]])
        self.assertNotIn("Alex", json.dumps(models))
        # a second phrase for the same bot, and a different bot on an existing word, are different checks
        st, j = c.req("POST", "/api/enroll", {"phrase": "hey finn", "count": 25, "persona": "grok"})
        self.assertEqual((st, j["error"]), (400, "“Hey Finn” is already a wake word."))
        self.sent.clear()
        st, j = c.req("POST", "/api/enroll", {"phrase": "okay finn", "count": 25, "persona": "grok"})
        self.assertNotEqual(st, 202)
        ov = json.load(open(ov_path))
        self.assertNotIn("okay_finn", ov)  # listener refused: the new mapping is dropped
        self.assertEqual(ov["hey_kitchen"]["persona"], "kitchen-bot")

    def test_status_transitions(self):
        prev = {}
        busy = {"enrolling": True, "last_enroll": {"phrase": "hey_kitchen", "stage": "recording",
                                                    "message": "Say it on the station.", "persona": "grok", "session": "s1"}}
        got = ga.derive_enroll_status(prev, busy, set(), 100)
        self.assertEqual(got["hey_kitchen"]["state"], "recording")
        self.assertEqual(got["hey_kitchen"]["updated_at"], 100)
        again = ga.derive_enroll_status(got, busy, set(), 250)
        self.assertEqual(again["hey_kitchen"]["updated_at"], 100)  # unchanged stage does not bump the clock
        up = {"enrolling": False, "last_enroll": {"phrase": "hey_kitchen", "stage": "uploading",
                                                   "message": "Sending the clips.", "persona": "grok", "session": "s1"}}
        got = ga.derive_enroll_status(again, up, set(), 300)
        self.assertEqual(got["hey_kitchen"]["state"], "training")  # clips sent, model not published yet
        self.assertEqual(got["hey_kitchen"]["updated_at"], 300)
        self.assertFalse(got["hey_kitchen"]["had_model"])
        ready = ga.derive_enroll_status(got, up, {"hey_kitchen"}, 400)
        self.assertEqual(ready["hey_kitchen"]["state"], "ready")
        self.assertEqual(ready["hey_kitchen"]["message"], "Ready.")
        busy_up = {"enrolling": True, "last_enroll": dict(up["last_enroll"], stage="uploading")}
        still = {"hey_kitchen": {"state": "uploading", "message": "Sending the clips.", "updated_at": 1,
                                 "persona": "grok", "had_model": False, "session": "s1"}}
        self.assertEqual(ga.derive_enroll_status(still, busy_up, {"hey_kitchen"}, 450)["hey_kitchen"]["state"], "uploading")
        topup = {"hey_kitchen": {"state": "uploading", "message": "Sending the clips.", "updated_at": 1,
                                 "persona": "grok", "had_model": True, "session": "s1"}}
        self.assertEqual(ga.derive_enroll_status(topup, up, {"hey_kitchen"}, 460)["hey_kitchen"]["state"], "training")
        failed = {"enrolling": False, "last_enroll": {"phrase": "hey_kitchen", "stage": "failed",
                                                       "message": "Needs more clips.", "persona": "grok"}}
        stay = ga.derive_enroll_status(ready, failed, {"hey_kitchen"}, 600)
        self.assertEqual(stay["hey_kitchen"]["state"], "failed")
        self.assertEqual(stay["hey_kitchen"]["message"], "Needs more clips.")
        # listener down: keep what was saved, but a published model still finishes a training row
        training = {"okay_finn": {"state": "training", "message": "Training.", "updated_at": 7,
                                  "persona": "finn", "had_model": False}}
        self.assertEqual(ga.derive_enroll_status(training, None, set(), 800)["okay_finn"]["state"], "training")
        self.assertEqual(ga.derive_enroll_status(training, None, set(), 800)["okay_finn"]["updated_at"], 7)
        published = ga.derive_enroll_status(training, None, {"okay_finn"}, 800)
        self.assertEqual(published["okay_finn"]["state"], "ready")
        self.assertEqual(published["okay_finn"]["message"], "Ready.")
        # a model that was already there when recording started is not a new publish
        old_model = {"hey_kitchen": {"state": "training", "message": "Training.", "updated_at": 9,
                                     "persona": "grok", "had_model": True}}
        self.assertEqual(ga.derive_enroll_status(old_model, None, {"hey_kitchen"}, 900)["hey_kitchen"]["state"], "training")

    def test_status_persists_across_a_new_handler(self):
        c = signed_in()
        CLOCK[0] = 1_700_000_100
        ga.write_enroll_status({"hey_kitchen": {"state": "recording", "message": "Say it on the station.",
                                                "updated_at": 10, "persona": "kitchen-bot", "had_model": False,
                                                "session": "s1"}})
        self.enroll_up([
            (200, {"enrolling": True, "last_enroll": {"phrase": "hey_kitchen", "stage": "recording",
                                                       "message": "Say it on the station.", "persona": "kitchen-bot",
                                                       "session": "s1"}}),
            (503, {"error": "wake listener unreachable"}),
            (200, {"enrolling": False, "last_enroll": {"phrase": "hey_kitchen", "stage": "training",
                                                        "message": "Training.", "persona": "kitchen-bot", "session": "s1"}}),
            (503, {"error": "wake listener unreachable"}),
        ])
        st, j = c.req("GET", "/api/enroll/status")
        self.assertEqual(st, 200)
        self.assertEqual(j["words"]["hey_kitchen"]["state"], "recording")
        self.assertEqual(j["words"]["hey_kitchen"]["updated_at"], 10)
        self.assertEqual(j["buttons"]["kitchen-bot"]["label"], "Processing…")
        self.assertTrue(j["buttons"]["kitchen-bot"]["processing"])
        c2 = C()  # another request builds another Handler; the cookie is the same sign-in
        c2.jar = dict(c.jar)
        st, j = c2.req("GET", "/api/enroll/status")
        self.assertEqual(j["words"]["hey_kitchen"]["state"], "recording")
        self.assertEqual(j["buttons"]["kitchen-bot"]["label"], "Processing…")
        os.makedirs(os.path.dirname(ga.ENROLLED), exist_ok=True)
        json.dump({"words": [{"name": "hey_kitchen", "persona": "grok"}]}, open(ga.ENROLLED, "w"))
        st, j = c.req("GET", "/api/enroll/status")
        self.assertEqual(j["words"]["hey_kitchen"]["state"], "ready")
        self.assertEqual(j["buttons"]["kitchen-bot"]["label"], "Ready")
        self.assertFalse(j["buttons"]["kitchen-bot"]["processing"])
        self.assertEqual(j["buttons"]["kitchen-bot"]["message"], "")
        path = os.path.join(os.environ["ADMIN_STATE_DIR"], "enroll-status.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
        saved = json.load(open(path))
        c3 = C()
        c3.jar = dict(c.jar)
        st, j = c3.req("GET", "/api/enroll/status")
        self.assertEqual(st, 200)
        self.assertEqual(j["words"]["hey_kitchen"]["state"], "ready")
        self.assertEqual(j["buttons"]["kitchen-bot"]["label"], "Ready")
        self.assertEqual(json.load(open(path)), saved)  # listener down: the file is what the next page load reads

    def test_record_button(self):
        processing = ga.record_button([
            {"state": "failed", "message": "Needs more clips.", "updated_at": 9, "phrase": "okay_finn"},
            {"state": "training", "message": "Training.", "updated_at": 2, "phrase": "hey_kitchen"},
        ])
        self.assertEqual(processing["label"], "Processing…")
        self.assertTrue(processing["processing"])
        self.assertEqual(processing["phrase"], "hey_kitchen")  # training wins over a newer failure
        self.assertEqual(processing["state"], "training")
        failed = ga.record_button([
            {"state": "ready", "message": "Ready.", "updated_at": 1, "phrase": "hey_kitchen"},
            {"state": "failed", "message": "Needs more clips.", "updated_at": 5, "phrase": "okay_finn"},
        ])
        self.assertEqual(failed, {"label": "Record voice", "processing": False, "message": "Needs more clips.",
                                  "phrase": "okay_finn", "state": "failed"})
        ready = ga.record_button([{"state": "ready", "message": "Ready.", "updated_at": 4, "phrase": "hey_kitchen"}])
        self.assertEqual(ready["label"], "Ready")
        self.assertFalse(ready["processing"])
        self.assertEqual(ready["message"], "")
        self.assertEqual(ready["state"], "ready")
        idle = ga.record_button([])
        self.assertEqual(idle["label"], "Record voice")
        self.assertEqual(idle["state"], "idle")
        buttons = ga.buttons_for({
            "hey_kitchen": {"state": "training", "message": "Training.", "updated_at": 3, "persona": "kitchen-bot"},
            "play_lamp": {"state": "ready", "message": "Ready.", "updated_at": 1, "persona": "kitchen-bot"},
            "okay_finn": {"state": "failed", "message": "Needs more clips.", "updated_at": 4, "persona": "finn"},
            "hey_lamp": {"state": "failed", "message": "Nope.", "updated_at": 8, "persona": "Not A Slug"},
        })
        self.assertEqual(buttons["kitchen-bot"]["label"], "Processing…")
        self.assertEqual(buttons["kitchen-bot"]["phrase"], "hey_kitchen")
        self.assertEqual(buttons["finn"]["label"], "Record voice")
        self.assertEqual(buttons["finn"]["state"], "failed")
        self.assertEqual(buttons["finn"]["message"], "Needs more clips.")
        self.assertEqual(buttons["grok"]["phrase"], "hey_lamp")  # a persona that isn't a bot slug counts as Grok
        self.assertEqual(buttons["grok"]["message"], "Nope.")

class StepUp(unittest.TestCase):
    """Sudo-style window on the session: 10 minutes from the password, no slide."""
    URL, KEY = Bots.URL, Bots.KEY

    def setUp(self):
        self._clock = CLOCK[0]
        CLOCK[0] = 9 * 3600 + 50 * 60  # 09:50; a window opened now closes at 10:00
        self.c = signed_in()
        self._rc = ga.relay_call
        def fake(method, path, payload=None, timeout=10):
            if path == "/admin/bots" and method == "GET":
                return 200, {"bots": [{"slug": "kai", "name": "Kai", "voice": "eve", "source": "registry",
                                       "webhook": {"configured": True, "last4": "WXYZ", "saved_at": 1},
                                       "status": {"state": "connected", "last_answered_at": 1}}],
                             "voices": ["eve"]}
            if path == "/admin/bots" and method == "POST":
                return 201, {"ok": True, "slug": payload["slug"], "voice_token": "vt_" + "A" * 43}
            return 200, {"ok": True, "last4": "WXYZ"}
        ga.relay_call = fake

    def tearDown(self):
        ga.relay_call = self._rc
        CLOCK[0] = self._clock

    def _save(self, password=GOOD):
        body = {"url": self.URL, "key": self.KEY}
        if password is not None:
            body["password"] = password
        return self.c.req("PUT", "/api/bots/kai/webhook", body)

    def _until(self):
        return self.c.req("GET", "/api/state")[1]["stepup_until"]

    def test_opens_does_not_slide_and_expires_at_10(self):
        self.assertIsNone(self._until())
        self.assertEqual(self._save()[0], 200)
        self.assertEqual(self._until(), 10 * 3600)
        self.assertEqual(self.c.req("GET", "/api/bots")[1]["stepup_until"], 10 * 3600)
        CLOCK[0] = 10 * 3600 - 5 * 60  # 09:55, still inside the window
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kay"})[0], 200)
        self.assertEqual(self.c.req("POST", "/api/bots", {"slug": "zed", "name": "Zed", "voice": "eve"})[0], 201)
        self.assertEqual(self._until(), 10 * 3600)  # those requests did not move it
        CLOCK[0] = 10 * 3600 - 1
        self.assertEqual(self.c.req("PUT", "/api/bots/kai/webhook", {"url": self.URL, "key": self.KEY})[0], 200)
        self.assertEqual(self._until(), 10 * 3600)
        CLOCK[0] = 10 * 3600  # 10:00 exactly
        self.assertIsNone(self._until())
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kai"})[0], 403)
        self.assertEqual(self._save()[0], 200)  # a new password opens a new window
        self.assertEqual(self._until(), 10 * 3600 + ga.STEPUP_TTL)

    def test_reopen_while_open_resets_from_that_entry(self):
        self._save()
        CLOCK[0] = 10 * 3600 - 2 * 60
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(self._save()[0], 200)
        self.assertEqual(self._until(), CLOCK[0] + ga.STEPUP_TTL)
        self.assertEqual(buf.getvalue().count("stepup open"), 1)
        self.assertNotIn(GOOD, buf.getvalue())

    def test_logout_and_password_change_clear(self):
        self._save()
        self.c.req("POST", "/api/logout", {})
        self.assertIsNone(self.c.req("GET", "/api/state")[1]["stepup_until"])
        self.assertEqual(self.c.req("POST", "/api/login", {"username": "admin", "password": GOOD})[0], 200)
        self.assertIsNone(self._until())
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kay"})[0], 403)
        self._save()
        self.assertEqual(self.c.req("POST", "/api/password", {"old": GOOD, "new": "a brand new pass 7"})[0], 200)
        self.assertIsNone(self._until())
        self.assertEqual(self.c.req("POST", "/api/bots", {"slug": "zed", "name": "Zed", "voice": "eve"})[0], 403)

    def test_idle_and_session_expiry_clear(self):
        self._save()
        rec = next(iter(ga.AUTH.sessions.values()))
        rec["stepup_until"] = CLOCK[0] + ga.IDLE_TTL + 500  # would still be open when idle hits
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kay"})[0], 200)
        CLOCK[0] = rec["seen"] + ga.IDLE_TTL + 1
        st, j = self.c.req("GET", "/api/state")
        self.assertFalse(j["signed_in"])
        self.assertIsNone(j["stepup_until"])
        self.assertEqual(ga.AUTH.sessions, {})
        self.assertEqual(self.c.req("POST", "/api/login", {"username": "admin", "password": GOOD})[0], 200)
        self.assertIsNone(self._until())
        self._save()
        rec = next(iter(ga.AUTH.sessions.values()))
        rec["seen"] = CLOCK[0]
        rec["exp"] = CLOCK[0] + 10
        rec["stepup_until"] = CLOCK[0] + 500
        CLOCK[0] += 11  # absolute session cap, idle not yet due
        st, j = self.c.req("GET", "/api/state")
        self.assertFalse(j["signed_in"])
        self.assertIsNone(j["stepup_until"])

    def test_remove_still_prompts_inside_the_window(self):
        self._save()
        self.assertEqual(self.c.req("DELETE", "/api/bots/kai", {})[0], 403)
        self.assertEqual(self.c.req("DELETE", "/api/bots/kai/webhook", {})[0], 403)
        self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kay"})[0], 200)  # rename still skipped
        self.assertEqual(self.c.req("DELETE", "/api/bots/kai/webhook", {"password": GOOD})[0], 200)

    def test_audit_line_once_per_open(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(self._save()[0], 200)
            self.assertEqual(self.c.req("PATCH", "/api/bots/kai", {"name": "Kay"})[0], 200)
            self.assertEqual(self.c.req("POST", "/api/bots", {"slug": "zed", "name": "Zed", "voice": "eve"})[0], 201)
            self.assertEqual(self.c.req("PUT", "/api/bots/kai/webhook", {"url": self.URL, "key": self.KEY})[0], 200)
        text = buf.getvalue()
        self.assertEqual(text.count("stepup open"), 1)
        self.assertNotIn(GOOD, text)
        self.assertNotIn(self.KEY, text)

    def test_wrong_password_does_not_open(self):
        body = {"url": self.URL, "key": self.KEY, "password": "wrong-wrong-1"}
        for _ in range(5):
            buf = io.StringIO()
            with redirect_stdout(buf):
                st, _ = self.c.req("PUT", "/api/bots/kai/webhook", body)
            self.assertNotEqual(st, 200)
            self.assertNotIn("stepup open", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            st, _ = self.c.req("PUT", "/api/bots/kai/webhook", dict(body, password=GOOD))
        self.assertEqual(st, 429)  # same 15-minute lockout as sign-in
        self.assertNotIn("stepup open", buf.getvalue())
        self.assertIsNone(self._until())


class LocationLabel(unittest.TestCase):
    """The browser sees the place name. Coordinates stay in the helper's env write."""
    LAT, LON = "12.3456", "-7.5"

    def setUp(self):
        self._clock = CLOCK[0]
        self.c = signed_in()
        self.saved = {}
        self.calls = []
        self._helper = ga.helper
        self._urlopen = ga.urllib.request.urlopen
        def fake(*args, stdin=None, timeout=30):
            self.calls.append(args)
            if args[:2] == ("settings-set", "time"):
                self.saved.clear()
                self.saved.update(stdin or {})
                return True, {"ok": True}
            if args[:2] == ("settings-get", "time"):
                return True, {"values": {
                    "GROK_STATION_LAT": self.saved.get("GROK_STATION_LAT"),
                    "GROK_STATION_LON": self.saved.get("GROK_STATION_LON"),
                    "GROK_STATION_UNITS": self.saved.get("GROK_STATION_UNITS"),
                    "timezone": self.saved.get("timezone") or "UTC"}}
            return True, {"ok": True}
        ga.helper = fake

    def tearDown(self):
        ga.helper = self._helper
        ga.urllib.request.urlopen = self._urlopen
        CLOCK[0] = self._clock

    def _no_coords(self, blob):
        low = blob.lower()
        for n in (self.LAT, self.LON, "12.3456", "-7.5000"):
            self.assertNotIn(n, blob)
        self.assertNotIn("latitude", low)
        self.assertNotIn("longitude", low)
        self.assertNotRegex(low, r'"(lat|lon|latitude|longitude|grok_station_lat|grok_station_lon)"')

    def test_label_only_in_get_responses(self):
        st, j = self.c.req("PUT", "/api/settings/time", {"GROK_STATION_LAT": self.LAT, "GROK_STATION_LON": self.LON,
                                                         "timezone": "UTC"})
        self.assertEqual(st, 200)
        self.assertNotIn("GROK_STATION_LAT", self.saved)
        self.assertNotIn("GROK_STATION_LON", self.saved)
        payload = {"results": [{"name": "Exampleton", "admin1": "Sample", "country_code": "US",
                                "latitude": 12.3456, "longitude": -7.5, "timezone": "America/Chicago"}]}
        class Resp:
            def read(self, n): return json.dumps(payload).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        old = ga.urllib.request.urlopen
        ga.urllib.request.urlopen = lambda *a, **k: Resp()
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                st, j = self.c.req("GET", "/api/geocode?q=Exampleton")
                self.assertEqual(st, 200)
                self._no_coords(json.dumps(j))
                self.assertEqual(set(j["results"][0]), {"id", "label"})
                self.assertEqual(j["results"][0]["label"], "Exampleton, Sample")
                pid = j["results"][0]["id"]
                st, j = self.c.req("PUT", "/api/settings/time", {"place": pid, "timezone": "UTC",
                                                                 "GROK_STATION_UNITS": "celsius",
                                                                 "GROK_STATION_LAT": self.LAT, "GROK_STATION_LON": self.LON})
                self.assertEqual(st, 200)
                self._no_coords(json.dumps(j))
                self.assertEqual(j["location"], "Exampleton, Sample")
                self.assertEqual(j["timezone"], "America/Chicago")
        finally:
            ga.urllib.request.urlopen = old
        self.assertNotIn(self.LAT, buf.getvalue())
        self.assertNotIn(self.LON, buf.getvalue())
        self.assertEqual(self.saved["GROK_STATION_LAT"], self.LAT)
        self.assertEqual(self.saved["GROK_STATION_LON"], self.LON)
        self.assertEqual(self.saved["timezone"], "America/Chicago")
        self.assertEqual(self.saved["GROK_STATION_UNITS"], "celsius")
        raw = open(os.path.join(os.environ["ADMIN_STATE_DIR"], "location.json")).read()
        self.assertIn("Exampleton, Sample", raw)
        self.assertNotIn(self.LAT, raw)
        self.assertNotIn(self.LON, raw)
        for path in ("/api/state", "/api/settings/time", "/api/settings/display", "/api/home", "/api/bots",
                     "/api/geocode?q=Exampleton"):
            if path.startswith("/api/geocode"):
                ga.urllib.request.urlopen = lambda *a, **k: Resp()
            st, body = self.c.req("GET", path, raw=True)
            if path.startswith("/api/geocode"):
                ga.urllib.request.urlopen = old
            self.assertEqual(st, 200, path)
            self._no_coords(body.decode())
        got = json.loads(self.c.req("GET", "/api/settings/time", raw=True)[1])
        self.assertEqual(got["values"]["location"], "Exampleton, Sample")
        CLOCK[0] += ga.GEOCODE_TTL + 1
        before = len(self.calls)
        self.assertEqual(self.c.req("PUT", "/api/settings/time", {"place": pid})[0], 400)
        self.assertEqual(len(self.calls), before)


class WakePersonaSync(unittest.TestCase):
    def test_control_endpoint_runs_sync_in_a_thread(self):
        sys.path.insert(0, os.path.join(ROOT, "wake"))
        import grok_wake as gw
        port = free_port()
        old_port, old_sync = gw.CONTROL_PORT, gw.sync_personas
        box = {"n": 0, "release": threading.Event()}
        def fake():
            box["n"] += 1
            box["release"].wait(3)
        gw.sync_personas = fake
        gw.CONTROL_PORT = port
        th = threading.Thread(target=gw.control_server, daemon=True)
        th.start()
        try:
            st = j = None
            for _ in range(50):
                try:
                    st, j = self._post(port)
                    break
                except OSError:
                    time.sleep(0.02)
            self.assertEqual((st, j), (202, {"started": True}))
            st, j = self._post(port)
            self.assertEqual((st, j), (202, {"running": True}))
            self.assertEqual(box["n"], 1)  # the second call did not start another sync
            box["release"].set()
            deadline = time.time() + 2
            while time.time() < deadline:
                st, j = self._post(port)
                if j.get("started") is True:
                    break
                time.sleep(0.02)
            self.assertEqual(j.get("started"), True)
            self.assertEqual(box["n"], 2)
        finally:
            box["release"].set()
            gw.sync_personas = old_sync
            if gw.CONTROL_HTTPD is not None:
                gw.CONTROL_HTTPD.shutdown()
                th.join(2)
                gw.CONTROL_HTTPD.server_close()
                gw.CONTROL_HTTPD = None
            gw.CONTROL_PORT = old_port

    def _post(self, port):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("POST", "/personas/sync", b"{}", {"Content-Type": "application/json"})
        r = c.getresponse()
        body = json.loads(r.read() or b"{}")
        c.close()
        return r.status, body


class PrivacyGuestBotIdAndWake(unittest.TestCase):
    """Privacy toggle, guest-mode removal, Bot ID, portraits, wake-entry migration, training states."""
    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    def test_privacy_needs_stepup_audits_and_proxies(self):
        c = signed_in()
        calls = []

        def unconfigured(method, path, payload=None, timeout=10, raw=None, content_type=None):
            calls.append((method, path, payload))
            return None

        old = ga.relay_call
        ga.relay_call = unconfigured
        try:
            st, j = c.req("GET", "/api/privacy")
            self.assertEqual(st, 200)
            self.assertIs(j["pii_mode"], True)
            self.assertEqual(j["message"], ga.RELAY_UNCONFIGURED)
            self.assertIn("household names", j["text"])
            calls.clear()
            buf = io.StringIO()
            with redirect_stdout(buf):
                st, j = c.req("PUT", "/api/privacy", {"pii_mode": False, "password": "wrong-secret-zz"})
            self.assertEqual(st, 403)
            self.assertEqual(calls, [])  # a wrong password never reaches the relay
            self.assertNotIn("wrong-secret-zz", buf.getvalue())
            st, j = c.req("PUT", "/api/privacy", {"pii_mode": False, "password": GOOD})
            self.assertEqual(st, 503)
            self.assertEqual(calls[-1], ("PUT", "/admin/policy", {"pii_mode": False, "actor": "admin"}))

            def configured(method, path, payload=None, timeout=10, raw=None, content_type=None):
                calls.append((method, path, payload, content_type))
                if method == "GET":
                    return 200, {"pii_mode": True}
                return 200, {"ok": True, "pii_mode": payload["pii_mode"]}

            ga.relay_call = configured
            calls.clear()
            buf = io.StringIO()
            with redirect_stdout(buf):
                st, j = c.req("PUT", "/api/privacy", {"pii_mode": False, "password": GOOD})
            self.assertEqual((st, j.get("pii_mode")), (200, False))
            self.assertEqual(calls[-1][:3], ("PUT", "/admin/policy", {"pii_mode": False, "actor": "admin"}))
            text = buf.getvalue()
            self.assertIn("audit persona=- action=pii_mode off", text)
            self.assertNotIn(GOOD, text)
            calls.clear()
            st, j = c.req("PUT", "/api/privacy", {"pii_mode": True})  # step-up window still open
            self.assertEqual((st, j.get("pii_mode")), (200, True))
            self.assertEqual(calls[-1][2], {"pii_mode": True, "actor": "admin"})
            ga.relay_call = lambda *a, **k: (200, {"ok": True})  # field missing: the switch stays on
            st, j = c.req("GET", "/api/privacy")
            self.assertEqual((st, j["pii_mode"]), (200, True))
            self.assertNotIn("message", j)
        finally:
            ga.relay_call = old

    def test_guest_mode_is_gone_and_a_stale_key_is_dropped(self):
        c = signed_in()
        st, sp = c.req("POST", "/api/speakers", {"name": "Alex Example", "guest": True})
        self.assertEqual(st, 200)
        ga.write_json(ga.path("t1.json"), {"guest": True, "kept": 1}, 0o640)
        ga.drop_stale_guest()
        doc = json.load(open(ga.path("t1.json")))
        self.assertNotIn("guest", doc)
        self.assertEqual(doc["kept"], 1)
        ga.drop_stale_guest()
        self.assertEqual(json.load(open(ga.path("t1.json"))), {"kept": 1})
        speakers = json.load(open(ga.path("speakers.json")))
        self.assertTrue(speakers[sp["id"]]["guest"])  # a speaker's own flag is not the station switch
        self.assertEqual(c.req("GET", "/api/guest")[0], 404)
        self.assertEqual(c.req("PUT", "/api/t1", {"guest": True})[0], 404)
        js = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        self.assertNotIn("Guest mode", js)
        self.assertNotIn("/api/guest", js)
        self.assertIn('id="spg"> Guest', js)

    def test_bot_id_helper_and_labels(self):
        self.assertEqual(ga.bot_id_from_name("Nix"), "nix")
        self.assertEqual(ga.bot_id_from_name("Hey Nix"), "hey-nix")
        self.assertEqual(ga.bot_id_from_name("Nix 2!"), "nix-2")
        self.assertEqual(ga.bot_id_problem("grok"), ga.BOT_ID_RESERVED)
        for bad in ("a", "1nix"):
            self.assertIn("voice service", ga.bot_id_problem(bad))
        self.assertIsNone(ga.bot_id_problem("nix"))
        self.assertIsNone(ga.bot_id_problem("kitchen-bot"))
        js = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        self.assertIn(">Bot ID <", js)
        self.assertNotIn(">Slug<", js)
        self.assertIn("Short lowercase ID for this bot, like nix. Used for wake-word routing, portraits and replies. Can't be changed later.", js)
        self.assertIn("Hey Grok is the built-in wake word. It is always on.", js)
        self.assertNotIn("Jarvis", js)
        self.assertIn("Higher wakes more easily but may wake by mistake. Lower means fewer false wakes but it may miss you.", js)
        self.assertIn('min="20" max="30" step="1" value="25"', js)
        self.assertIn("Block personal info in replies", js)
        self.assertIn('["privacy", "Privacy"]', js)

    def test_avatar_upload_needs_stepup_and_refresh_schedules_sync(self):
        c = signed_in()
        calls = []
        delays = []

        def fake(method, path, payload=None, timeout=10, raw=None, content_type=None):
            calls.append((method, path, payload, content_type, raw))
            if method == "POST" and path.endswith("/avatar-request"):
                return 202, {"ok": True}
            if method == "POST" and path.endswith("/avatar"):
                return 200, {"ok": True}
            return 200, {"ok": True, "bots": [], "voices": ["eve"]}

        old_rc, old_sync = ga.relay_call, ga.schedule_persona_sync
        ga.relay_call = fake
        ga.schedule_persona_sync = lambda delay=30: delays.append(delay)
        try:
            st, j = c.req("POST", "/api/bots/kai/avatar", data=self.PNG, ctype="image/png")
            self.assertEqual(st, 403)
            self.assertEqual(calls, [])
            st, j = c.req("POST", "/api/bots/kai/avatar", data=self.PNG, ctype="image/png",
                          headers={"X-GS-Password": GOOD})
            self.assertEqual(st, 200, j)
            method, path, payload, ctype, raw = calls[-1]
            self.assertEqual((method, path, ctype), ("POST", "/admin/bots/kai/avatar", "image/png"))
            self.assertIsNone(payload)
            self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
            n = len(calls)
            st, j = c.req("POST", "/api/bots/kai/avatar", data=b"\xff\xd8\xffnot-a-png", ctype="image/png",
                          headers={"X-GS-Password": GOOD})
            self.assertEqual(st, 400)
            self.assertEqual(j["error"], ga.AVATAR_ERROR)
            self.assertEqual(len(calls), n)  # magic mismatch never reaches the relay
            calls.clear()
            st, j = c.req("POST", "/api/bots/kai/avatar-request", {})
            self.assertEqual(st, 202)
            self.assertEqual(delays, [30])
            self.assertEqual(calls[-1][:3], ("POST", "/admin/bots/kai/avatar-request", {"actor": "admin"}))
        finally:
            ga.relay_call, ga.schedule_persona_sync = old_rc, old_sync

    def test_wake_entries_keep_existing_enabled_state(self):
        signed_in()
        old_cfg = ga.WAKE_CONFIG
        d = tempfile.mkdtemp()
        models = os.path.join(d, "models")
        os.makedirs(models)
        open(os.path.join(models, "hey_grok.onnx"), "w").close()
        cfg = os.path.join(d, "wakewords.json")
        ov_path = ga.path("wake-overrides.json")
        try:
            json.dump({"models_dir": models, "words": [{"name": "hey_finn", "persona": "finn", "threshold": 0.5}],
                       }, open(cfg, "w"))
            ga.WAKE_CONFIG = cfg
            ga.write_json(ov_path, {"hey_finn": {"enabled": False, "threshold": 0.3}}, 0o640)
            ga.wakewords()
            ov = json.load(open(ov_path))
            self.assertEqual(ov["hey_finn"], {"enabled": False, "threshold": 0.3})
            os.remove(ov_path)
            ga.wakewords()
            ov = json.load(open(ov_path))
            self.assertNotIn("hey_finn", ov)  # a config word is not copied into the overrides
            self.assertTrue(ov["hey_grok"]["enabled"])
            json.dump({"models_dir": models, "words": []}, open(cfg, "w"))
            os.remove(ov_path)
            words = {w["name"]: w for w in ga.wakewords()}
            enabled = sorted(n for n, w in words.items() if w.get("enabled"))
            self.assertEqual(enabled, ["hey_grok"])
            self.assertNotIn("hey_jarvis", words)
            ga.write_json(ov_path, {"hey_nix": {"enabled": True, "persona": "nix"}}, 0o640)
            ga.remember_bot_wakeword("nix")
            self.assertTrue(json.load(open(ov_path))["hey_nix"]["enabled"])  # already there: left alone
            ga.remember_bot_wakeword("zed")
            ov = json.load(open(ov_path))
            self.assertEqual(ov["hey_zed"], {"enabled": False, "persona": "zed"})
            self.assertTrue(ov["hey_nix"]["enabled"])
        finally:
            ga.WAKE_CONFIG = old_cfg

    def test_new_bot_wake_entry_starts_disabled(self):
        c = signed_in()

        def fake(method, path, payload=None, timeout=10, raw=None, content_type=None):
            if method == "POST" and path == "/admin/bots":
                return 201, {"ok": True, "slug": payload["slug"], "voice_token": "vt_" + "A" * 43}
            if method == "GET" and path == "/admin/bots":
                return 200, {"bots": [{"slug": "nix", "name": "Nix", "voice": "eve", "source": "added"}],
                             "voices": ["eve"]}
            return 200, {"ok": True}

        old = ga.relay_call
        ga.relay_call = fake
        try:
            st, j = c.req("POST", "/api/bots", {"slug": "nix", "name": "Nix", "voice": "eve", "password": GOOD})
            self.assertIn(st, (200, 201), j)
            ov = json.load(open(ga.path("wake-overrides.json")))
            self.assertEqual(ov["hey_nix"], {"enabled": False, "persona": "nix"})
            words = {w["name"]: w for w in c.req("GET", "/api/wakewords")[1]["words"]}
            self.assertFalse(words["hey_nix"]["enabled"])
            self.assertEqual(words["hey_nix"]["label"], "Hey Nix")
            ov = json.load(open(ga.path("wake-overrides.json")))
            self.assertEqual(ov["hey_nix"], {"enabled": False, "persona": "nix"})
        finally:
            ga.relay_call = old

    def test_below_the_gate_is_failed_quality_not_done(self):
        log = "JOB DONE below the gate, not published"
        self.assertEqual(ga.map_train_state("done", log), "failed_quality")
        self.assertNotEqual(ga.map_train_state("done", log), "done")
        self.assertEqual(ga.map_train_state("published", "JOB DONE published"), "published")
        self.assertEqual(ga.map_train_state("processing", ""), "processing")
        self.assertEqual(ga.map_train_state("done", "JOB DONE check (fail), not published"), "failed_quality")
        msg = ga.quality_message({"recall_real": 0.65, "fa_h": 1.5})
        self.assertIn("0.65", msg)
        self.assertIn("0.80", msg)
        self.assertIn("20-30", msg)
        c = signed_in()
        ga.write_enroll_status({
            "hey_finn": {"state": "done", "message": log, "updated_at": 1, "persona": "finn",
                         "scores": {"recall_real": 0.65, "fa_h": 1.5}},
            "hey_hank": {"state": "published", "message": "Ready.", "updated_at": 2, "persona": "hank"},
            "hey_kai": {"state": "processing", "message": "Processing…", "updated_at": 3, "persona": "kai"},
        })
        st, j = c.req("GET", "/api/enroll/status")
        self.assertEqual(st, 200)
        self.assertEqual(j["words"]["hey_finn"]["state"], "failed_quality")
        self.assertNotEqual(j["words"]["hey_finn"]["state"], "done")
        self.assertIn("0.65", j["words"]["hey_finn"]["message"])
        self.assertIn("20-30", j["words"]["hey_finn"]["message"])
        self.assertEqual(j["words"]["hey_hank"]["state"], "published")
        self.assertEqual(j["words"]["hey_kai"]["state"], "processing")
        self.assertEqual(j["buttons"]["finn"]["state"], "failed_quality")
        self.assertIn("0.65", j["buttons"]["finn"]["label"])
        self.assertEqual(j["buttons"]["hank"]["label"], "Ready")
        self.assertEqual(j["buttons"]["kai"]["label"], "Processing…")


if __name__ == "__main__":
    unittest.main()


class FirstRunKey(unittest.TestCase):
    """First run: code, password, then the write-only xAI key. The screen asks for the key until it is saved."""
    KEY = "xai-" + "A" * 40

    def setUp(self):
        os.unlink(ga.XAI_KEY_FILE)
        open(os.environ["FAKE_LOG"], "w").close()

    def tearDown(self):
        open(ga.XAI_KEY_FILE, "w").write("set\n")

    def test_key_step_after_password_then_screen_normal(self):
        c = signed_in()
        d = setup_doc()
        self.assertEqual(d["mode"], "key")
        self.assertNotIn("code", d)
        self.assertIn("fingerprint", d)
        st, j = c.req("GET", "/api/state")
        self.assertFalse(j["xai_key_set"])
        self.assertEqual(j["wizard"]["step"], 3)  # the key step comes right after the password
        self.assertEqual(c.req("POST", "/api/xai-key", {"key": "short"})[0], 400)
        st, j = c.req("POST", "/api/xai-key", {"key": self.KEY})
        self.assertEqual(st, 200)
        self.assertNotIn(self.KEY, json.dumps(j))
        calls = [json.loads(l) for l in open(os.environ["FAKE_LOG"]) if l.strip()]
        self.assertEqual([x for x in calls if x["argv"] == ["xai-key-set"]][0]["stdin"], json.dumps({"key": self.KEY}))
        open(ga.XAI_KEY_FILE, "w").write("set\n")  # the fake helper doesn't write; the real one does
        ga.AUTH.publish()
        self.assertFalse(os.path.exists(os.path.join(os.environ["ADMIN_RUN_DIR"], "setup.json")))
        st, j = c.req("GET", "/api/xai-key")
        self.assertEqual(j, {"set": True})

    def test_key_api_needs_sign_in(self):
        reset_state()
        self.assertEqual(C().req("POST", "/api/xai-key", {"key": self.KEY})[0], 401)
        self.assertEqual(C().req("GET", "/api/xai-key")[0], 401)
