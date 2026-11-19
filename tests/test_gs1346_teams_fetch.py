"""A dripping teams list stops at the deadline. A fake response cannot show that.

The server is a real socket on 127.0.0.1. The pinned HTTPS connection is replaced
here, in the test, with a plain connection to that socket. Production has no flag for it.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import cards  # noqa: E402

DRIP_N = 40
DRIP_S = 0.1
OK_BODY = (
    b'{"sports":[{"leagues":[{"teams":[{"team":{"id":"1","abbreviation":"A",'
    b'"displayName":"Alpha"}}]}]}]}'
)


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        return


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _drip(self, body, finish):
        try:
            for i in range(len(body)):
                self.wfile.write(body[i:i + 1])
                self.wfile.flush()
                time.sleep(DRIP_S)
            finish()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def do_GET(self):
        mode = self.server.mode
        self.server.hits += 1
        if mode == "close":
            self.protocol_version = "HTTP/1.0"
        else:
            self.protocol_version = "HTTP/1.1"
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "https://site.api.espn.com/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "oversize":
            n = cards.TEAM_LIST_MAX + 8
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(n))
            self.end_headers()
            try:
                self.wfile.write(b"x" * min(n, 64))
            except (BrokenPipeError, ConnectionResetError):
                return
            return
        if mode == "ok":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(OK_BODY)))
            self.end_headers()
            self.wfile.write(OK_BODY)
            return
        body = b"{" * DRIP_N
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if mode == "length":
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._drip(body, lambda: None)
            return
        if mode == "chunked":
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def end():
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

            def one(chunk):
                self.wfile.write(b"%x\r\n" % len(chunk))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            try:
                for i in range(len(body)):
                    one(body[i:i + 1])
                    time.sleep(DRIP_S)
                end()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
            return
        self.end_headers()
        self._drip(body, lambda: None)


class _LocalHTTP(cards.http.client.HTTPConnection):
    """Test stand-in for the pinned HTTPS connection. Talks to the local drip server."""

    port_override = 9

    def __init__(self, host, pin, port=None, timeout=5, context=None):
        super().__init__("127.0.0.1", self.port_override, timeout=timeout)


class DripFetch(unittest.TestCase):
    def setUp(self):
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.mode = "length"
        self.server.hits = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        _LocalHTTP.port_override = self.server.server_address[1]
        self.old_pin = cards.pin_ip
        self.old_conn = cards.PinnedHTTPSConnection
        self.old_fetch = cards.TEAM_FETCH_S
        self.old_max = cards.TEAM_LIST_MAX
        cards.pin_ip = lambda host, port=443, resolve=None: "127.0.0.1"
        cards.PinnedHTTPSConnection = _LocalHTTP
        cards.TEAM_FETCH_S = 1
        self.addCleanup(self._restore)
        self.tmp = tempfile.TemporaryDirectory(prefix="gs1346-drip-")
        self.addCleanup(self.tmp.cleanup)
        self.cache = cards.LogoCache(self.tmp.name, ["a.espncdn.com"])
        self.cache.team_refresh = True
        self.url = cards.teams_list_url("football", "nfl")
        self.path = cards._teams_path(self.tmp.name, "football", "nfl")
        with cards._TEAM_LOCK:
            cards._TEAM_REFRESH.clear()
            cards._TEAM_FAIL_AT.clear()
            cards._TEAM_PARSED.clear()

    def _restore(self):
        cards.pin_ip = self.old_pin
        cards.PinnedHTTPSConnection = self.old_conn
        cards.TEAM_FETCH_S = self.old_fetch
        cards.TEAM_LIST_MAX = self.old_max

    def _clear_backoff(self):
        with cards._TEAM_LOCK:
            cards._TEAM_FAIL_AT.clear()
            cards._TEAM_REFRESH.clear()
        self.server.hits = 0

    def _one_drip(self, mode):
        self._clear_backoff()
        self.server.mode = mode
        started = time.monotonic()
        cards.schedule_teams_refresh(self.cache, self.path, self.url)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with cards._TEAM_LOCK:
                busy = self.path in cards._TEAM_REFRESH
            if not busy:
                break
            time.sleep(0.02)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, cards.TEAM_FETCH_S + 1.5, mode)
        self.assertIn(self.path, cards._TEAM_FAIL_AT, mode)
        self.assertEqual(self.server.hits, 1, mode)
        cards.schedule_teams_refresh(self.cache, self.path, self.url)
        time.sleep(0.3)
        self.assertEqual(self.server.hits, 1, mode)

    def test_each_framing_stops_at_the_deadline_and_backs_off(self):
        for mode in ("length", "chunked", "close"):
            self._one_drip(mode)

    def test_a_normal_body_an_oversize_body_and_a_redirect(self):
        self.server.mode = "ok"
        got = cards.fetch_teams_bytes(self.url)
        self.assertEqual(got, OK_BODY)
        cards.TEAM_LIST_MAX = 32
        self.server.mode = "oversize"
        self.assertIsNone(cards.fetch_teams_bytes(self.url))
        cards.TEAM_LIST_MAX = self.old_max
        self.server.mode = "redirect"
        self.assertIsNone(cards.fetch_teams_bytes(self.url))
        self.assertEqual(self.server.hits, 3)


if __name__ == "__main__":
    unittest.main()
