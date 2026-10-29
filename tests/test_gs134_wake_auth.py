#!/usr/bin/env python3
"""Wake control port requires X-Grok-Wake-Token. Clients send it. The token is never logged."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "wake"))
import grok_wake as gw

TOKEN = "wake-test-value"
WARN = "wake control: token unreadable"
HEADER = "X-grok-wake-token"  # Request.get_header lowercases the name


def _write_token(text):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "wake_control_token")
    with open(path, "w", encoding="ascii") as f:
        f.write(text)
    os.chmod(path, 0o440)
    return path


def _trust_test_owner():
    """The server only trusts root:grok-wake 0440. Tests own their files, so point the policy at this user."""
    old = dict(gw.WAKE_TOKEN_POLICY)
    gw.WAKE_TOKEN_POLICY.update(uid=os.getuid(), gid=os.getgid())
    return old


class _Resp:
    def __init__(self, status=200, body=b'{"ok": true}'):
        self.status = status
        self._body = body

    def read(self, n=65536):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _header(req):
    return req.get_header(HEADER)


class TokenFile(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get("WAKE_CONTROL_TOKEN_FILE")
        self.cached = bytes(gw._WAKE_TOKEN["raw"])
        self.policy = _trust_test_owner()

    def tearDown(self):
        if self.old is None:
            os.environ.pop("WAKE_CONTROL_TOKEN_FILE", None)
        else:
            os.environ["WAKE_CONTROL_TOKEN_FILE"] = self.old
        gw._WAKE_TOKEN["raw"] = self.cached
        gw.WAKE_TOKEN_POLICY.clear(); gw.WAKE_TOKEN_POLICY.update(self.policy)

    def test_strip_cache_and_reread_when_missing(self):
        missing = os.path.join(tempfile.mkdtemp(), "absent")
        os.environ["WAKE_CONTROL_TOKEN_FILE"] = missing
        self.assertEqual(gw._load_wake_token(force=True), b"")
        path = _write_token(TOKEN + "\n")
        os.environ["WAKE_CONTROL_TOKEN_FILE"] = path
        self.assertEqual(gw._load_wake_token(), TOKEN.encode("ascii"))
        os.chmod(path, 0o640)
        with open(path, "w", encoding="ascii") as f:
            f.write("other-value\n")
        os.chmod(path, 0o440)
        self.assertEqual(gw._load_wake_token(), TOKEN.encode("ascii"))  # cached once it was read
        self.assertEqual(gw._load_wake_token(force=True), b"other-value")

    def test_install_creates_a_group_readable_token(self):
        sh = open(os.path.join(ROOT, "install.sh")).read()
        self.assertIn("gen $S/wake_control_token root:grok-wake 0440", sh)
        for rel in ("admin/grok_admin.py", "relay/relay.py", "app/grok_station.py", "wake/grok_wake.py"):
            src = open(os.path.join(ROOT, rel)).read()
            self.assertIn("WAKE_CONTROL_TOKEN_FILE", src)
            self.assertNotIn('E("WAKE_CONTROL_TOKEN"', src)
            self.assertNotIn("os.environ.get(\"WAKE_CONTROL_TOKEN\"", src)


class ControlServer(unittest.TestCase):
    def prepare(self):
        """Hook for subclasses to break the token file before the server starts."""

    def setUp(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.path = _write_token(TOKEN + "\n")
        self.policy = _trust_test_owner()
        self.prepare()
        self.old_env = os.environ.get("WAKE_CONTROL_TOKEN_FILE")
        os.environ["WAKE_CONTROL_TOKEN_FILE"] = self.path
        self.old_port = gw.CONTROL_PORT
        self.state = dict(gw.ENROLL_STATE)
        self.reload = gw.RELOAD.is_set()
        self.recovery = gw.RECOVERY
        self.live = (gw.LIVE["play"], gw.LIVE["session"])
        self.late = gw.LATE_PLAYING["on"]
        self.sync = gw.sync_personas
        self.sync_on = gw._PERSONA_SYNC["on"]
        self.post = gw.post_state
        self.token = bytes(gw._WAKE_TOKEN["raw"])
        gw.CONTROL_PORT = self.port
        gw.ENROLL_STATE.update(busy=False, pause=False, discard=False, prompt=None)
        gw.RELOAD.clear()
        gw.RECOVERY = None
        gw.LIVE["play"] = None
        gw.LIVE["session"] = False
        gw.LATE_PLAYING["on"] = False
        gw._PERSONA_SYNC["on"] = False
        gw.post_state = lambda state=None, **kw: None
        self.syncs = {"n": 0}

        def fake():
            self.syncs["n"] += 1

        gw.sync_personas = fake
        self.th = threading.Thread(target=gw.control_server, daemon=True)
        self.th.start()

    def tearDown(self):
        if gw.CONTROL_HTTPD is not None:
            gw.CONTROL_HTTPD.shutdown()
            self.th.join(2)
            gw.CONTROL_HTTPD.server_close()
            gw.CONTROL_HTTPD = None
        gw.CONTROL_PORT = self.old_port
        gw.ENROLL_STATE.clear()
        gw.ENROLL_STATE.update(self.state)
        if self.reload:
            gw.RELOAD.set()
        else:
            gw.RELOAD.clear()
        gw.RECOVERY = self.recovery
        gw.LIVE["play"], gw.LIVE["session"] = self.live
        gw.LATE_PLAYING["on"] = self.late
        gw.sync_personas = self.sync
        gw._PERSONA_SYNC["on"] = self.sync_on
        gw.post_state = self.post
        gw._WAKE_TOKEN["raw"] = self.token
        gw.WAKE_TOKEN_POLICY.clear(); gw.WAKE_TOKEN_POLICY.update(self.policy)
        if self.old_env is None:
            os.environ.pop("WAKE_CONTROL_TOKEN_FILE", None)
        else:
            os.environ["WAKE_CONTROL_TOKEN_FILE"] = self.old_env

    def _once(self, method, path, body, token):
        import http.client
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Grok-Wake-Token"] = token
        data = None if body is None else json.dumps(body).encode()
        last = None
        for _ in range(50):
            try:
                c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                c.request(method, path, data, headers)
                r = c.getresponse()
                raw = r.read() or b"{}"
                c.close()
                return r.status, raw
            except OSError as exc:
                last = exc
                time.sleep(0.02)
        raise last

    def test_every_route_rejects_a_missing_or_wrong_token(self):
        routes = [
            ("GET", "/status", None, 200),
            ("GET", "/no-such", None, 404),
            ("POST", "/personas/sync", {}, 202),
            ("POST", "/reload", {}, 202),
            ("POST", "/forget", {}, 400),
            ("POST", "/test-sound", {}, 409),
            ("POST", "/enroll/control", {"op": "pause"}, 409),
            ("POST", "/enroll/open-tile", {"persona": "juniper", "phrase": "hey_juniper", "name": "Juniper"}, 200),
            ("POST", "/late-reply", {}, 404),
            ("POST", "/mic-reset", {}, 503),
            ("POST", "/enroll", {"phrase": "nope"}, 400),
            ("POST", "/no-such", {}, 404),
        ]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            for method, path, body, _expect in routes:
                leaked = dict(body or {}, leak=TOKEN)
                status, raw = self._once(method, path, None if body is None else leaked, None)
                self.assertEqual(status, 403, path)
                self.assertEqual(json.loads(raw), {"error": "forbidden"})
                self.assertNotIn(TOKEN.encode(), raw)
                status, raw = self._once(method, path, None if body is None else leaked, "wrong-token")
                self.assertEqual(status, 403, path)
                self.assertEqual(json.loads(raw), {"error": "forbidden"})
                self.assertNotIn(TOKEN.encode(), raw)
            self.assertFalse(gw.RELOAD.is_set())
            self.assertIsNone(gw.ENROLL_STATE.get("prompt"))
            self.assertEqual(self.syncs["n"], 0)
            queued = gw.ENROLL_Q.qsize()
            for method, path, body, expect in routes:
                status, raw = self._once(method, path, body, TOKEN)
                self.assertEqual(status, expect, (path, raw))
                self.assertNotIn(TOKEN.encode(), raw)
            self.assertTrue(gw.RELOAD.is_set())
            self.assertIsNotNone(gw.ENROLL_STATE.get("prompt"))
            self.assertEqual(gw.ENROLL_Q.qsize(), queued)
            deadline = time.time() + 2
            while self.syncs["n"] < 1 and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(self.syncs["n"], 1)
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())


class FailClosedTokenFile(ControlServer):
    """A bad token file at startup: every route is 403, even with the right header."""

    ROUTES = [("GET", "/status", None), ("POST", "/enroll", {"phrase": "hey_finn", "count": 5}),
              ("POST", "/enroll/control", {"op": "discard"}), ("POST", "/enroll/open-tile", {"phrase": "hey_finn"}),
              ("POST", "/personas/sync", {}), ("POST", "/mic-reset", {}), ("POST", "/late-reply", {"id": "x"})]

    def prepare(self):
        os.chmod(self.path, 0o640)  # group-writable owner bit set: not 0440

    def test_every_route_rejects_a_missing_or_wrong_token(self):
        self._all_403()

    def _all_403(self):
        for method, path, body in self.ROUTES:
            code = self._once(method, path, body, TOKEN)[0]
            self.assertEqual(code, 403, f"{method} {path}")

    def test_bad_file(self):
        self._all_403()


class FailClosedMissing(FailClosedTokenFile):
    def prepare(self):
        os.remove(self.path)


class FailClosedEmpty(FailClosedTokenFile):
    def prepare(self):
        os.chmod(self.path, 0o640)
        open(self.path, "w").close()
        os.chmod(self.path, 0o440)


class FailClosedWorldReadable(FailClosedTokenFile):
    def prepare(self):
        os.chmod(self.path, 0o444)


class FailClosedGroupWritable(FailClosedTokenFile):
    def prepare(self):
        os.chmod(self.path, 0o460)


class FailClosedWrongOwner(FailClosedTokenFile):
    def prepare(self):
        gw.WAKE_TOKEN_POLICY["uid"] = os.getuid() + 1


class FailClosedWrongGroup(FailClosedTokenFile):
    def prepare(self):
        gw.WAKE_TOKEN_POLICY["gid"] = os.getgid() + 1


class Clients(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get("WAKE_CONTROL_TOKEN_FILE")
        self.path = _write_token(TOKEN + "\n")
        os.environ["WAKE_CONTROL_TOKEN_FILE"] = self.path
        self.flags = []

    def tearDown(self):
        if self.old is None:
            os.environ.pop("WAKE_CONTROL_TOKEN_FILE", None)
        else:
            os.environ["WAKE_CONTROL_TOKEN_FILE"] = self.old
        for mod, name, prev in self.flags:
            setattr(mod, name, prev)

    def _flag(self, mod, name):
        self.flags.append((mod, name, getattr(mod, name)))
        setattr(mod, name, False)

    def test_admin_relay_display_and_enroll_send_the_header(self):
        sys.path.insert(0, os.path.join(ROOT, "admin"))
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        sys.path.insert(0, os.path.join(ROOT, "app"))
        import grok_admin as ga
        import relay
        import grok_station as gs

        self._flag(ga, "_WAKE_TOKEN_WARNED")
        self._flag(relay, "_WAKE_TOKEN_WARNED")
        self._flag(gs, "_WAKE_TOKEN_WARNED")
        self._flag(gw, "_WAKE_CLIENT_WARNED")
        seen = []

        def build_opener(*handlers):
            seen.append(("admin-proxy", handlers))

            class Op:
                def open(self, req, timeout=None):
                    seen.append(req)
                    return _Resp()

            return Op()

        def urlopen(req, timeout=None):
            seen.append(req)
            return _Resp(body=b'{"enrolling": false}')

        old_build, old_open = urllib.request.build_opener, urllib.request.urlopen
        old_late = os.environ.get("GROK_LATE_DIR")
        os.environ["GROK_LATE_DIR"] = tempfile.mkdtemp()
        urllib.request.build_opener = build_opener
        urllib.request.urlopen = urlopen
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code, body = ga.wake("POST", "/enroll/control", {"op": "discard"})
                self.assertEqual(code, 200)
                relay.post_wake_open_tile({"persona": "juniper", "phrase": "hey_juniper", "name": "Juniper"})
                self.assertEqual(relay._post_wake_late("vs_" + "a" * 22, "later", "Juniper", b""), 200)
                gs.fetch_wake_status()
                fake = type("H", (), {})()
                raw = b'{"phrase": "hey_finn", "count": 5}'
                fake.headers = {"Content-Length": str(len(raw))}
                fake.rfile = io.BytesIO(raw)
                fake.client_address = ("127.0.0.1", 9)
                fake.replies = []
                fake._reply = lambda code, obj: fake.replies.append((code, obj))
                gs.Handler._enroll(fake)
                gw.enroll_cli("hey_juniper", 5, False)
        finally:
            urllib.request.build_opener, urllib.request.urlopen = old_build, old_open
            if old_late is None:
                os.environ.pop("GROK_LATE_DIR", None)
            else:
                os.environ["GROK_LATE_DIR"] = old_late
        headers = [_header(req) for req in seen if hasattr(req, "get_header")]
        self.assertGreaterEqual(len(headers), 5)
        self.assertEqual(set(headers), {TOKEN})
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())
        self.assertNotIn(WARN, out.getvalue())
        proxy = [item[1] for item in seen if isinstance(item, tuple) and item[0] == "admin-proxy"][0]
        self.assertEqual(len(proxy), 1)
        self.assertIsInstance(proxy[0], urllib.request.ProxyHandler)
        self.assertEqual(proxy[0].proxies, {})

    def test_unreadable_token_still_sends_and_warns_once(self):
        sys.path.insert(0, os.path.join(ROOT, "admin"))
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        sys.path.insert(0, os.path.join(ROOT, "app"))
        import grok_admin as ga
        import relay
        import grok_station as gs

        os.environ["WAKE_CONTROL_TOKEN_FILE"] = os.path.join(tempfile.mkdtemp(), "missing")
        self._flag(ga, "_WAKE_TOKEN_WARNED")
        self._flag(relay, "_WAKE_TOKEN_WARNED")
        self._flag(gs, "_WAKE_TOKEN_WARNED")
        self._flag(gw, "_WAKE_CLIENT_WARNED")
        seen = []

        def build_opener(*handlers):
            class Op:
                def open(self, req, timeout=None):
                    seen.append(req)
                    raise urllib.error.URLError("down")

            return Op()

        def urlopen(req, timeout=None):
            seen.append(req)
            raise urllib.error.URLError("down")

        old_build, old_open = urllib.request.build_opener, urllib.request.urlopen
        urllib.request.build_opener = build_opener
        urllib.request.urlopen = urlopen
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(ga.wake("GET", "/status")[0], 503)
                self.assertEqual(ga.wake("GET", "/status")[0], 503)
                relay.post_wake_open_tile({"persona": "juniper", "name": "Juniper"})
                relay.post_wake_open_tile({"persona": "juniper", "name": "Juniper"})
                gs.fetch_wake_status()
                gs.fetch_wake_status()
                for _ in range(2):
                    with self.assertRaises(urllib.error.URLError):
                        gw.enroll_cli("hey_juniper", 5, False)
        finally:
            urllib.request.build_opener, urllib.request.urlopen = old_build, old_open
        headers = [_header(req) for req in seen]
        self.assertTrue(headers)
        self.assertEqual(set(headers), {None})
        text = out.getvalue()
        self.assertEqual(text.count(WARN), 4)  # admin, relay, display, enroll cli — once each
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(TOKEN, err.getvalue())
        self.assertNotIn("missing", text)


if __name__ == "__main__":
    unittest.main()
