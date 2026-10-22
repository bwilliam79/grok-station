#!/usr/bin/env python3
"""0.1.1: relay startup smoke test, requirements cover the health-check imports, and the station key path.

Root cause of the blank station key on 0.1.0: relay/requirements.txt lacked PyNaCl, so the relay could not import
botreply, never answered on loopback, and the admin page showed "Can't reach the voice service".
"""
import asyncio, base64, os, re, shutil, socket, stat, subprocess, sys, tempfile, time, unittest, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(ROOT, "relay")
IMPORT_NAME = {"pynacl": "nacl", "pillow": "PIL"}


def _reqs():
    with open(os.path.join(RELAY, "requirements.txt")) as fh:
        return {m.group(1).lower() for m in re.finditer(r"^([A-Za-z0-9_.-]+)==", fh.read(), re.M)}


class Requirements(unittest.TestCase):
    def test_health_check_imports_are_pinned(self):
        """Every module install.sh's health check imports from the relay venv is in requirements.txt."""
        src = open(os.path.join(ROOT, "install.sh")).read()
        m = re.search(r"relay/venv/bin/python -c 'import ([^']+)'", src)
        self.assertIsNotNone(m)
        mods = [x.strip() for x in m.group(1).split(",")]
        provided = {IMPORT_NAME.get(r, r) for r in _reqs()}
        for mod in mods:
            self.assertIn(mod, provided, f"{mod} is imported by the health check but not in relay/requirements.txt")

    def test_relay_third_party_imports_are_pinned(self):
        provided = {IMPORT_NAME.get(r, r) for r in _reqs()}
        local = {f[:-3] for f in os.listdir(RELAY) if f.endswith(".py")}
        for f in os.listdir(RELAY):
            if not f.endswith(".py"):
                continue
            for m in re.finditer(r"^\s*(?:from|import) ([A-Za-z_][A-Za-z0-9_]*)", open(os.path.join(RELAY, f)).read(), re.M):
                mod = m.group(1)
                if mod in local or mod in sys.stdlib_module_names:
                    continue
                self.assertIn(mod, provided, f"relay/{f} imports {mod}, which is not in requirements.txt")

    def test_in_and_txt_agree(self):
        names = {re.split(r"[=<>\s]", ln.strip())[0].lower() for ln in open(os.path.join(RELAY, "requirements.in"))
                 if ln.strip() and not ln.startswith("#")}
        self.assertTrue(names <= _reqs(), names - _reqs())

    def test_installer_installs_requirements_every_run(self):
        src = open(os.path.join(ROOT, "install.sh")).read()
        line = [ln for ln in src.splitlines() if "pip install" in ln and "relay/requirements.txt" in ln]
        self.assertTrue(line)
        self.assertNotIn("||", line[0])  # not gated on the venv being new

    def test_installer_makes_station_key_and_restarts_on_failure(self):
        src = open(os.path.join(ROOT, "install.sh")).read()
        self.assertIn("station_box_key", src)
        self.assertIn("chown grok-relay:grok-admin $S/station_box_key", src)
        self.assertIn("Restarting the station services after the failed update", src)
        self.assertIn("relay answers on loopback", src)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class StartupSmoke(unittest.TestCase):
    """Start relay.py as a process, like systemd does, and call /healthz and /admin/bots on loopback."""

    def _start(self, secrets_ro=False):
        d = tempfile.mkdtemp(prefix="gs-relay-smoke-")
        self.addCleanup(shutil.rmtree, d, True)
        sec, st = os.path.join(d, "secrets"), os.path.join(d, "state")
        os.makedirs(sec)
        os.makedirs(st)
        with open(os.path.join(sec, "local_token"), "w") as fh:
            fh.write("a" * 64 + "\n")
        with open(os.path.join(sec, "admin_token"), "w") as fh:
            fh.write("b" * 64 + "\n")
        if secrets_ro:
            os.chmod(sec, 0o555)  # what ProtectSystem=strict does to /etc for the relay
            self.addCleanup(os.chmod, sec, 0o755)
        port = _free_port()
        env = dict(os.environ, GROK_SECRETS_DIR=sec, GROK_STATE_DIR=st, GROK_SHARED_STATE="1",
                   RELAY_BIND="127.0.0.1", RELAY_PORT=str(port), XAI_API_KEY="")
        p = subprocess.Popen([sys.executable, os.path.join(RELAY, "relay.py")], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: (p.kill(), p.wait(), p.stdout.close()))
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1).read()
                break
            except OSError:
                if p.poll() is not None:
                    self.fail("relay exited at startup: " + p.stdout.read().decode(errors="replace")[-800:])
                time.sleep(0.1)
        else:
            self.fail("relay never answered /healthz")
        return port, p

    def _bots(self, port):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/admin/bots", headers={"Authorization": "Bearer " + "b" * 64})
        import json
        return json.loads(urllib.request.urlopen(req, timeout=5).read())

    def test_starts_and_returns_station_key(self):
        port, _ = self._start()
        body = self._bots(port)
        self.assertEqual(len(base64.b64decode(body["station_pub"])), 32)
        self.assertFalse(body["station_pub_error"])

    @unittest.skipIf(os.geteuid() == 0, "root ignores the read-only mode")
    def test_read_only_secrets_reports_error_not_blank(self):
        port, p = self._start(secrets_ro=True)
        body = self._bots(port)
        self.assertEqual(body["station_pub"], "")
        self.assertTrue(body["station_pub_error"])
        p.kill()
        out = p.stdout.read().decode(errors="replace")
        self.assertIn("station key unreadable", out)
        self.assertNotIn("b" * 64, out)

    def test_missing_package_is_named(self):
        src = open(os.path.join(RELAY, "relay.py")).read()
        self.assertIn("missing Python package", src)


class PairingUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = open(os.path.join(ROOT, "admin", "web", "app.js")).read()
        cls.css = open(os.path.join(ROOT, "admin", "web", "admin.css")).read()

    def test_station_key_is_static_text_with_copy(self):
        self.assertIn('staticValue("spub", stationPub, "station public key", STATION_KEY_MISSING)', self.js)
        self.assertIn('staticValue("astapub", el._stationPub, "station public key", STATION_KEY_MISSING)', self.js)
        self.assertIn('staticValue("tlsfp"', self.js)
        self.assertNotRegex(self.js, r'<input[^>]*readonly')
        self.assertIn('Station key unavailable, see logs', self.js)

    def test_copy_has_clipboard_and_fallback(self):
        self.assertIn("navigator.clipboard", self.js)
        self.assertIn('document.execCommand("copy")', self.js)
        self.assertIn('title="Copy ${esc(label)}"', self.js)
        self.assertIn('"Copied"', self.js)

    def test_admin_passes_key_and_error(self):
        sys.path.insert(0, os.path.join(ROOT, "admin"))
        src = open(os.path.join(ROOT, "admin", "grok_admin.py")).read()
        self.assertIn('"station_pub_error": not pub_ok', src)
        self.assertIn('"station_pub_error": True', src)


if __name__ == "__main__":
    unittest.main()
