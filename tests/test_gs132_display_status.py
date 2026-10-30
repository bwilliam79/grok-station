"""0.13.2 (Nix Info): the admin reads the display's localhost /status with proxies off and one overall deadline."""
import json, os, sys, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_admin import ga  # noqa: E402  (sets up the admin env before import)

BODY = json.dumps({"grok": {"state": "online", "cause": None}}).encode()


class _H(BaseHTTPRequestHandler):
    trickle = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "8000" if self.trickle else str(len(BODY)))
        self.end_headers()
        if not self.trickle:
            self.wfile.write(BODY)
            return
        end = time.monotonic() + 8
        try:
            while time.monotonic() < end:
                self.wfile.write(b" "); self.wfile.flush(); time.sleep(0.3)
        except OSError:
            pass


class DisplayStatusRead(unittest.TestCase):
    def _serve(self, trickle):
        h = type("H", (_H,), {"trickle": trickle})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        old = {k: os.environ.get(k) for k in ("GROK_STATION_PORT", "http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in old.items()])
        os.environ["GROK_STATION_PORT"] = str(srv.server_address[1])
        os.environ.pop("no_proxy", None); os.environ.pop("NO_PROXY", None)

    def test_proxy_env_is_ignored(self):
        self._serve(False)
        os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"  # nothing listens; a proxied read fails
        import urllib.request
        urllib.request.install_opener(None)  # urlopen caches its opener; drop it so the env is read as at a fresh start
        self.assertEqual(ga._fetch_display_status(1.5).get("grok"), {"state": "online", "cause": None})
        self.assertEqual(ga.read_display_status(), {"state": "online", "cause": None})

    def test_overall_deadline_holds_against_a_trickling_body(self):
        self._serve(True)
        t0 = time.monotonic()
        with self.assertRaises(Exception):
            ga._fetch_display_status(1.5)
        self.assertLess(time.monotonic() - t0, 5.0)
        t0 = time.monotonic()
        self.assertEqual(ga.read_display_status(timeout=1.5), {"state": "checking", "cause": None})
        self.assertLess(time.monotonic() - t0, 5.0)


if __name__ == "__main__":
    unittest.main()
