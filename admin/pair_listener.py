"""TLS pairing listener. Bound only while the pairing window says the port should be open.

The socket factory is injectable. Tests bind port 0 on 127.0.0.1. A peer that
fails lan_source_ok is dropped before the TLS handshake. Nothing secret is
written to the log or to GET /pair/cert-check.
"""
import json, os, re, ssl, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from pairing import lan_source_ok

BODY_CAP = 16 * 1024
DEFAULT_PORT = 8443
_PID = re.compile(r"[0-9a-f]{16}\Z")


class Issuance:
    """Certs the relay may collect after the admin confirms. Public certs only."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_id = {}
        self._served = False

    def put(self, pairing_id, cert_pem, ca_pem):
        if not isinstance(pairing_id, str) or not _PID.fullmatch(pairing_id):
            return
        with self._lock:
            self._by_id[pairing_id] = {"cert_pem": cert_pem, "ca_pem": ca_pem}
            self._served = False

    def get(self, pairing_id):
        with self._lock:
            item = self._by_id.get(pairing_id)
            return dict(item) if item else None

    def mark_served(self):
        """The relay has collected the cert. The next poll may close the port."""
        with self._lock:
            self._served = True

    def hold_open(self):
        """True while a confirmed cert is waiting for GET /pair/result."""
        with self._lock:
            return bool(self._by_id) and not self._served

    def clear(self):
        with self._lock:
            self._by_id.clear()
            self._served = True


def _default_socket(host, port):
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(8)
    return sock


def _server_context(certfile, keyfile):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile, keyfile)
    return ctx


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, sock, handler, ctx, source_ok):
        super().__init__(("127.0.0.1", 0), handler, bind_and_activate=False)
        try:
            self.socket.close()
        except OSError:
            pass
        self.socket = sock
        self.server_address = sock.getsockname()
        self.ssl_ctx = ctx
        self.source_ok = source_ok

    def finish_request(self, request, client_address):
        ip = client_address[0] if client_address else ""
        if not self.source_ok(ip if isinstance(ip, str) else ""):
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            request.settimeout(10)
            tls = self.ssl_ctx.wrap_socket(request, server_side=True)
            tls.settimeout(15)
        except Exception:
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            super().finish_request(tls, client_address)
        except Exception:
            try:
                tls.close()
            except OSError:
                pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, code, obj):
        body = (json.dumps(obj) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/pair/cert-check":
            # The TLS certificate is the check. The body stays empty of secrets.
            return self._json(200, {"ok": True})
        if path == "/pair/result":
            return self._result()
        return self._json(404, {"ok": False, "reason": "not-found"})

    def _result(self):
        q = parse_qs(urlsplit(self.path).query)
        pairing_id = (q.get("id") or [""])[0]
        if not _PID.fullmatch(pairing_id):
            return self._json(400, {"ok": False, "reason": "pairing-id"})
        with self.server.lock:
            got = self.server.issuance.get(pairing_id)
        if not got:
            return self._json(404, {"ok": False, "reason": "pending"})
        self._json(200, {"ok": True, "cert_pem": got["cert_pem"], "ca_pem": got["ca_pem"]})
        # After the body is sent, so a poll cannot close the socket mid-response.
        self.server.issuance.mark_served()

    def do_POST(self):
        if urlsplit(self.path).path != "/pair/submit":
            return self._json(404, {"ok": False, "reason": "not-found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"ok": False, "reason": "bad-request"})
        if n < 0 or n > BODY_CAP:
            return self._json(413, {"ok": False, "reason": "too-large"})
        raw = self.rfile.read(n) if n else b""
        try:
            doc = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "reason": "bad-json"})
        if not isinstance(doc, dict):
            return self._json(400, {"ok": False, "reason": "bad-json"})
        with self.server.lock:
            result = self.server.window.submit(doc.get("csr_pem"), doc.get("proof"))
        body = {
            "ok": bool(result.get("ok")),
            "reason": result.get("reason"),
            "tries": result.get("tries"),
        }
        fp = result.get("relay_fingerprint")
        if isinstance(fp, str) and fp:
            body["relay_fingerprint"] = fp
        return self._json(200 if result.get("ok") else 400, body)

    def log_message(self, fmt, *args):
        return


class PairListener:
    """Bind while `window.port_should_be_open()`, and unbind on the next poll otherwise.

    `socket_factory(host, port)` returns a listening socket. `on_bind` / `on_unbind`
    run on the transition (firewall and the station screen). `on_bind` returning
    False leaves the port closed.
    """

    def __init__(self, window, issuance, certfile, keyfile, *, host="0.0.0.0", port=None,
                 socket_factory=None, source_ok=None, on_bind=None, on_unbind=None, lock=None):
        self.window = window
        self.issuance = issuance
        self.certfile = certfile
        self.keyfile = keyfile
        self.host = host
        if port is None:
            port = int(os.environ.get("GROK_STATION_PAIR_PORT", str(DEFAULT_PORT)))
        self.port = int(port)
        self.socket_factory = socket_factory or _default_socket
        self.source_ok = source_ok or lan_source_ok
        self.on_bind = on_bind
        self.on_unbind = on_unbind
        self.lock = lock or threading.RLock()
        self._gate = threading.Lock()
        self._httpd = None
        self._thread = None
        self.bound_address = None

    @property
    def bound(self):
        return self._httpd is not None

    def _collecting(self):
        """Keep the port up after confirm until the relay collects the cert or the window ends."""
        if self.issuance is None or not self.issuance.hold_open():
            return False
        expires = getattr(self.window, "expires_at", None)
        now_fn = getattr(self.window, "_now", None)
        if expires is not None and callable(now_fn):
            try:
                if float(now_fn()) >= float(expires):
                    self.issuance.clear()
                    return False
            except (TypeError, ValueError):
                pass
        return True

    def poll(self):
        """One tick. Call about once a second so timeout, cancel, and lockout unbind."""
        with self._gate:
            with self.lock:
                should = bool(self.window.port_should_be_open())
            if not should:
                should = self._collecting()
            if should:
                self._bind()
            else:
                self._unbind()

    def _bind(self):
        if self._httpd is not None:
            return
        if self.on_bind is not None and self.on_bind() is False:
            return
        try:
            sock = self.socket_factory(self.host, self.port)
        except Exception:
            if self.on_unbind is not None:
                try:
                    self.on_unbind()
                except Exception:
                    pass
            raise
        ctx = _server_context(self.certfile, self.keyfile)
        httpd = _Server(sock, _Handler, ctx, self.source_ok)
        httpd.window = self.window
        httpd.issuance = self.issuance
        httpd.lock = self.lock
        self.bound_address = sock.getsockname()
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()

    def _unbind(self):
        httpd = self._httpd
        if httpd is None:
            return
        self._httpd = None
        self.bound_address = None
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2)
        if self.on_unbind is not None:
            try:
                self.on_unbind()
            except Exception:
                pass

    def unbind(self):
        with self._gate:
            self._unbind()

    def serve(self, stop):
        """Poll once a second until `stop` is set."""
        while not stop.is_set():
            try:
                self.poll()
            except Exception:
                pass
            stop.wait(1.0)
