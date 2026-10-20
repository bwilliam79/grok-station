"""Admin page HTTPS: per-station cert (tools/grok-station tls-cert), TLS-only app, port 80 redirect-only, HSTS,
Secure cookies, fingerprint published for the station screen."""
import hashlib, http.client, json, os, re, socket, ssl, stat, subprocess, sys, tempfile, threading, time, unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
CLI = os.path.join(ROOT, "tools", "grok-station")
if "ADMIN_STATE_DIR" not in os.environ:  # run alone: same sandbox shape as test_admin
    T = tempfile.mkdtemp()
    os.environ.update(ADMIN_STATE_DIR=os.path.join(T, "state"), ADMIN_RUN_DIR=os.path.join(T, "run"))
    os.makedirs(os.environ["ADMIN_STATE_DIR"]); os.makedirs(os.environ["ADMIN_RUN_DIR"])
sys.path.insert(0, os.path.join(ROOT, "admin"))
sys.path.insert(0, os.path.join(ROOT, "app"))
import grok_admin as ga  # noqa: E402


def make_cert(d, host):
    hf = os.path.join(d, "hostname")
    open(hf, "w").write(host + "\n")
    r = subprocess.run(["sh", CLI, "tls-cert"], env=dict(os.environ, GROK_TLS_DIR=os.path.join(d, "tls"), GROK_HOSTNAME_FILE=hf),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class Cert(unittest.TestCase):
    def test_made_once_per_hostname_key_private(self):
        d = tempfile.mkdtemp()
        fp1 = make_cert(d, "grok-station")
        tls = os.path.join(d, "tls")
        self.assertRegex(fp1, r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(tls, "key.pem")).st_mode), 0o600)
        der = ssl.PEM_cert_to_DER_cert(open(os.path.join(tls, "cert.pem")).read())
        self.assertEqual(fp1, ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest()))
        txt = subprocess.run(["openssl", "x509", "-in", os.path.join(tls, "cert.pem"), "-noout", "-text"], capture_output=True, text=True).stdout
        for want in ("DNS:grok-station.local", "DNS:grok-station", "IP Address:10.42.0.1", "CA:FALSE", "TLS Web Server Authentication"):
            self.assertIn(want, txt)
        self.assertEqual(make_cert(d, "grok-station"), fp1)            # still valid, same host: kept
        fp2 = make_cert(d, "kitchen")                                   # renamed: new cert
        self.assertNotEqual(fp2, fp1)
        self.assertIn("DNS:kitchen.local", subprocess.run(["openssl", "x509", "-in", os.path.join(tls, "cert.pem"), "-noout", "-ext", "subjectAltName"],
                                                          capture_output=True, text=True).stdout)
        self.assertEqual(sorted(os.listdir(tls)), ["cert.pem", "fingerprint", "host", "key.pem"])  # no temp leftovers

    def test_no_key_in_repo(self):
        out = subprocess.run(["git", "-C", ROOT, "grep", "-l", "-I", "-E", "^-----BEGIN [A-Z ]*PRIVATE KEY-----$"], capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "")


class Servers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = tempfile.mkdtemp()
        cls.fp = make_cert(d, ga.hostname())
        ga.TLS_DIR = os.path.join(d, "tls")
        cls.https, cls.http = free_port(), free_port()
        ga.PORT = cls.https
        srv = ga.TLSServer(("127.0.0.1", cls.https), ga.Handler)
        srv.socket = ga.tls_context().wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        red = ga.ThreadingHTTPServer(("127.0.0.1", cls.http), ga.Redirect)
        threading.Thread(target=red.serve_forever, daemon=True).start()

    def tls_conn(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # self-signed: we pin by fingerprint below
        return http.client.HTTPSConnection("127.0.0.1", self.https, timeout=10, context=ctx)

    def test_https_serves_page_with_hsts_and_pinned_cert(self):
        c = self.tls_conn()
        c.request("GET", "/")
        r = c.getresponse(); body = r.read()
        self.assertEqual(r.status, 200)
        self.assertIn(b"<", body)
        self.assertEqual(r.getheader("Strict-Transport-Security"), "max-age=31536000")
        der = c.sock.getpeercert(binary_form=True)
        self.assertEqual(":".join(f"{b:02X}" for b in hashlib.sha256(der).digest()), self.fp)
        self.assertGreaterEqual(c.sock.version(), "TLSv1.2")

    def test_plain_http_on_tls_port_gets_nothing(self):
        c = http.client.HTTPConnection("127.0.0.1", self.https, timeout=5)
        with self.assertRaises(Exception):
            c.request("GET", "/"); r = c.getresponse(); r.read()
            if r.status: raise AssertionError("plain HTTP answered")  # noqa: E701

    def test_port80_redirects_only(self):
        for method in ("GET", "POST", "HEAD"):
            c = http.client.HTTPConnection("127.0.0.1", self.http, timeout=5)
            c.request(method, "/api/system?x=1", body=b"{}" if method == "POST" else None, headers={"Host": ga.hostname() + ".local"})
            r = c.getresponse(); body = r.read()
            self.assertEqual(r.status, 301)
            self.assertEqual(r.getheader("Location"), f"https://{ga.hostname()}.local:{self.https}/api/system?x=1")
            self.assertEqual(body, b"")
            self.assertIsNone(r.getheader("Set-Cookie"))
            self.assertIsNone(r.getheader("Content-Type"))

    def test_redirect_host_and_path_sanitized(self):
        for host, path in (("evil.com\r\nX: y", "/"), ("a b", "//evil.com/x"), ("", "/<script>")):
            c = http.client.HTTPConnection("127.0.0.1", self.http, timeout=5)
            c.putrequest("GET", path, skip_host=True); c.putheader("Host", host.replace("\r\n", "")) if host else None; c.endheaders()
            r = c.getresponse(); r.read()
            loc = r.getheader("Location")
            self.assertTrue(loc.startswith("https://"))
            self.assertNotIn("<", loc); self.assertNotIn(" ", loc)
            self.assertTrue(loc.startswith(f"https://{ga.hostname()}.local:{self.https}/"), loc)  # bad Host: our own name
            self.assertNotRegex(loc, r"^https://[^/]+//")  # never a protocol-relative path

    def test_system_page_fingerprint(self):
        self.assertEqual(ga.tls_info()["fingerprint"], self.fp)


class NoCertScreen(unittest.TestCase):
    def test_no_cert_screen_or_tls_json(self):
        import grok_station as gs
        for name in ("draw_cert", "read_cert", "draw_fingerprint", "TLS_FILE"):
            self.assertFalse(hasattr(gs, name), name)
        self.assertFalse(hasattr(ga, "publish_tls"))
        src = open(gs.__file__).read()
        self.assertNotIn("New security certificate", src)
        self.assertNotIn('"Admin page"', src)


if __name__ == "__main__":
    unittest.main()
