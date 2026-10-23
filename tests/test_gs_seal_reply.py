#!/usr/bin/env python3
"""Home-machine gs-seal-reply: one pinned POST, secret lint, permissions, no retry.

The TLS peer is a socket pair. Binding a listening socket is not available here, and the
script never needs a second host: create_connection is the only place a TCP socket would open.
"""
import argparse, base64, contextlib, errno, gc, hashlib, importlib.machinery, importlib.util, io
import json, os, re, socket, ssl, stat, subprocess, sys, tempfile, threading, time, unittest

import nacl
from nacl.public import PrivateKey
from nacl.signing import SigningKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import botreply  # noqa: E402
import handoff  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "gs-seal-reply")
SETUP = os.path.join(ROOT, "scripts", "gs-seal-setup")
COMMON = os.path.join(ROOT, "scripts", "gs_seal_common.py")
HOST_UUID = "00112233-4455-6677-8899-AABBCCDDEEFF"


def _load(name, path):
    """Load a seal script. Restore sys.path: the script replaces it while it imports."""
    saved_path = list(sys.path)
    saved_dont = sys.dont_write_bytecode
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        loader.exec_module(mod)
    finally:
        sys.path[:] = saved_path
        sys.dont_write_bytecode = saved_dont
    return mod


gs = _load("gs_seal_reply", SCRIPT)
gsc = sys.modules["gs_seal_common"]
su = _load("gs_seal_setup", SETUP)

RID = "vs_" + "a" * 22
RID2 = "vs_" + "b" * 22
ANSWER = "Tacos on Tuesday."


def _fullwidth(text):
    out = []
    for ch in text:
        o = ord(ch)
        if ch == " ":
            out.append("\u3000")
        elif 0x21 <= o <= 0x7e:
            out.append(chr(o + 0xfee0))
        else:
            out.append(ch)
    return "".join(out)


class QuietSock(socket.socket):
    """AF_UNIX pairs reject TCP_NODELAY with ENOTSUP. http.client only ignores ENOPROTOOPT."""

    def setsockopt(self, level, optname, value, *args):
        if optname == socket.TCP_NODELAY:
            return None
        return super().setsockopt(level, optname, value, *args)


def _read_http(ss, limit=1024 * 1024):
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < limit:
        chunk = ss.recv(4096)
        if not chunk:
            break
        buf += chunk
    if b"\r\n\r\n" not in buf:
        return buf, b""
    head, rest = buf.split(b"\r\n\r\n", 1)
    match = re.search(br"(?i)content-length:\s*(\d+)", head)
    need = int(match.group(1)) if match else 0
    while len(rest) < need and len(rest) < limit:
        chunk = ss.recv(4096)
        if not chunk:
            break
        rest += chunk
    return head, rest[:need]


class LocalTLS:
    """One HTTPS peer on a socket pair. Records each create_connection and each POST body."""

    def __init__(self, ctx, status=200):
        self.ctx = ctx
        self.status = status
        self.posts = []
        self.connects = []
        self.errors = []
        self._real = None
        self._threads = []

    def __enter__(self):
        self._real = socket.create_connection

        def fake(address, timeout=None, source_address=None):
            self.connects.append(address)
            raw_c, raw_s = socket.socketpair()
            raw_s.settimeout(5)
            client = QuietSock(fileno=raw_c.detach())
            if timeout is not None:
                client.settimeout(min(float(timeout) if timeout else 5, 5))
            entered = threading.Event()

            def run():
                entered.set()
                self._serve(raw_s)

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            self._threads.append(thread)
            if not entered.wait(2):
                raise RuntimeError("tls stand-in did not start")
            return client

        socket.create_connection = fake
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._real is not None:
            socket.create_connection = self._real
            self._real = None
        for thread in self._threads:
            thread.join(5)

    def _serve(self, raw):
        ss = None
        try:
            ss = self.ctx.wrap_socket(raw, server_side=True)
            ss.settimeout(2)
            head, body = _read_http(ss)
            if head.startswith(b"POST "):
                self.posts.append((head, body))
                payload = b'{"ok":true}'
                ss.sendall(
                    f"HTTP/1.1 {self.status} OK\r\nContent-Length: {len(payload)}\r\n"
                    "Connection: close\r\n\r\n".encode() + payload)
        except Exception as exc:
            self.errors.append(exc.__class__.__name__)
        finally:
            for sock in (ss, raw):
                try:
                    if sock is not None:
                        sock.close()
                except Exception:
                    pass


@contextlib.contextmanager
def _patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


class SealReply(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._cert_dir = tempfile.mkdtemp(prefix="gs-seal-cert-")
        cert = os.path.join(cls._cert_dir, "cert.pem")
        key = os.path.join(cls._cert_dir, "key.pem")
        subprocess.check_call(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key, "-out", cert,
             "-days", "1", "-nodes", "-subj", "/CN=localhost"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(cert) as fh:
            der = ssl.PEM_cert_to_DER_cert(fh.read())
        cls.pin = hashlib.sha256(der).hexdigest()
        cls.server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_ctx.load_cert_chain(cert, key)

    @classmethod
    def tearDownClass(cls):
        for name in os.listdir(cls._cert_dir):
            os.unlink(os.path.join(cls._cert_dir, name))
        os.rmdir(cls._cert_dir)

    def setUp(self):
        self._old_home = os.environ.get("HOME")
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        os.environ["HOME"] = self.home
        self.station = PrivateKey.generate()
        self.station_pub = base64.b64encode(bytes(self.station.public_key)).decode()
        self.hw_uuid = HOST_UUID
        self.tm_excluded = True
        self.tm_fail = False
        self.commands = []
        self.bad_argv = []
        self._real_run = subprocess.run
        self._real_platform = sys.platform
        gsc.subprocess.run = self._fake_run
        su.subprocess.run = self._fake_run
        self._load_nacl = (gs.load_nacl, su.load_nacl)

        def _skip_vendor(*_args, **_kwargs):
            import nacl
            return nacl

        self._skip_vendor = _skip_vendor
        gs.load_nacl = _skip_vendor
        su.load_nacl = _skip_vendor
        sys.platform = "darwin"

    def tearDown(self):
        gsc.subprocess.run = self._real_run
        su.subprocess.run = self._real_run
        gs.load_nacl, su.load_nacl = self._load_nacl
        sys.platform = self._real_platform
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    def _fake_run(self, argv, check=False, capture_output=False, **kwargs):
        if kwargs.get("shell"):
            raise AssertionError("shell")
        cmd = list(argv)
        if not cmd or cmd[0] not in ("/usr/bin/tmutil", "/usr/sbin/ioreg"):
            return self._real_run(argv, check=check, capture_output=capture_output, **kwargs)
        self.commands.append(cmd)
        out = b""
        rc = 0
        if cmd[:2] == ["/usr/bin/tmutil", "addexclusion"] and len(cmd) == 3:
            if not check:
                self.bad_argv.append(cmd)
            if self.tm_fail:
                raise subprocess.CalledProcessError(1, cmd)
        elif cmd[:2] == ["/usr/bin/tmutil", "isexcluded"] and len(cmd) == 3:
            if not capture_output:
                self.bad_argv.append(cmd)
            flag = b"[Excluded]" if self.tm_excluded else b"[Included]"
            out = flag + b"  " + cmd[2].encode() + b"\n"
        elif cmd == ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]:
            if not check or not capture_output:
                self.bad_argv.append(cmd)
            out = ('"IOPlatformUUID" = "%s"\n' % self.hw_uuid).encode()
        else:
            self.bad_argv.append(cmd)
            rc = 1
        if check and rc != 0:
            raise subprocess.CalledProcessError(rc, cmd, out, b"")
        return subprocess.CompletedProcess(cmd, rc, out, b"")

    def env(self, **extra):
        env = os.environ.copy()
        env["HOME"] = self.home
        env.pop("GROK_STATION_BOT_KEY", None)
        env.pop("GS_STATION", None)
        env.update(extra)
        return env

    def child_env(self, **extra):
        """HOME is the temp dir, so a new interpreter does not see this user's site-packages."""
        env = self.env(**extra)
        site = os.path.dirname(os.path.dirname(os.path.abspath(nacl.__file__)))
        prev = env.get("PYTHONPATH")
        env["PYTHONPATH"] = site if not prev else site + os.pathsep + prev
        return env

    def cli(self, argv, stdin=b"", **extra):
        """In-process command. keygen and init go to gs-seal-setup; reply stays on gs-seal-reply."""
        home = extra.pop("HOME", None)
        if extra:
            raise AssertionError("unexpected env %r" % (sorted(extra),))
        mod = su if argv and argv[0] in ("keygen", "init", "vendor-install") else gs
        return self._invoke(mod, argv, home=home, stdin=stdin)

    def _invoke(self, mod, argv, home=None, stdin=b""):
        old_home = os.environ.get("HOME")
        if home is not None:
            os.environ["HOME"] = home
        # unittest shows ResourceWarning. A finalizer from an earlier test
        # (an unclosed socket) must run before stderr is swapped, or the warning
        # is captured in front of "refused\n".
        gc.collect()
        old = (sys.stdin, sys.stdout, sys.stderr)
        wrapper = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
        sys.stdin, sys.stdout, sys.stderr = wrapper, io.StringIO(), io.StringIO()
        try:
            try:
                rc = mod.main(list(argv))
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
            return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
        finally:
            sys.stdin, sys.stdout, sys.stderr = old
            wrapper.close()
            if home is not None:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home

    def root(self):
        return os.path.join(self.home, ".config", "grok-station")

    def key_path(self, slug="kai"):
        return os.path.join(self.root(), "keys", slug + ".key")

    def config_path(self, slug="kai"):
        return os.path.join(self.root(), slug + ".json")

    def log_path(self):
        return os.path.join(self.root(), "gs-seal-reply.log")

    def sent_path(self):
        return os.path.join(self.root(), "sent-ids")

    def host_path(self):
        return os.path.join(self.root(), "host-id")

    def call(self, argv, stdin=b""):
        return self._invoke(gs, argv, stdin=stdin)

    def sent_ids(self):
        lines = self.slurp(self.sent_path()).splitlines()
        ids = []
        for line in lines:
            rid, epoch = line.split(" ")
            self.assertRegex(epoch, r"^[0-9]+$")
            self.assertLessEqual(abs(int(epoch) - time.time()), 120)
            ids.append(rid)
        return ids

    def reply(self, stdin, rid=RID, kind=None, status=200, persona="kai"):
        """In-process reply. A decoy station env must not change the pinned POST."""
        argv = ["reply", "--persona", persona, "--request-id", rid]
        if kind:
            argv += ["--kind", kind]
        data = stdin if isinstance(stdin, (bytes, bytearray)) else stdin.encode()
        old_station, old_key = os.environ.get("GS_STATION"), os.environ.get("GROK_STATION_BOT_KEY")
        os.environ["GS_STATION"] = "https://192.0.2.9"
        os.environ["GROK_STATION_BOT_KEY"] = base64.b64encode(b"\x11" * 32).decode()
        tls = LocalTLS(self.server_ctx, status)
        tls.__enter__()
        try:
            result = self.call(argv, data)
        finally:
            tls.close()
            if old_station is None:
                os.environ.pop("GS_STATION", None)
            else:
                os.environ["GS_STATION"] = old_station
            if old_key is None:
                os.environ.pop("GROK_STATION_BOT_KEY", None)
            else:
                os.environ["GROK_STATION_BOT_KEY"] = old_key
        return result, tls

    def provision(self, slug="kai", endpoint="https://127.0.0.1:8443", pin=None, station_pub=None):
        pin = self.pin if pin is None else pin
        station_pub = self.station_pub if station_pub is None else station_pub
        rc, out, err = self.cli(["keygen", "--persona", slug])
        self.assertEqual(self.bad_argv, [])
        self.assertEqual((rc, err), (0, ""), err)
        self.assertTrue(out.endswith("\n"))
        pub = out.strip()
        self.assertEqual(len(base64.b64decode(pub, validate=True)), 32)
        self.assertNotIn("public:", out)
        self.assertNotIn(self.hw_uuid, out)
        self.assertNotIn(self.hw_uuid, err)
        seed = self.slurp(self.key_path(slug), binary=True)
        self.assertEqual(len(seed), 32)
        self.assertEqual(pub, base64.b64encode(bytes(SigningKey(seed).verify_key)).decode())
        self.assertNotIn(base64.b64encode(seed).decode(), out)
        self.assertEqual(self.mode(self.key_path(slug)), 0o600)
        self.assertEqual(self.mode(os.path.join(self.root(), "keys")), 0o700)
        self.assertEqual(self.mode(self.root()), 0o700)
        self.assertEqual(self.slurp(self.host_path()), self.hw_uuid + "\n")
        self.assertEqual(self.mode(self.host_path()), 0o600)
        self.assertIn(["/usr/bin/tmutil", "addexclusion", self.root()], self.commands)
        rc, out, err = self.cli(["init", "--persona", slug, "--station", endpoint,
                                 "--pin-sha256", pin, "--station-pub", station_pub])
        self.assertEqual((rc, out, err), (0, "", ""), err)
        self.assertEqual(self.mode(self.config_path(slug)), 0o600)
        return seed

    def mode(self, path):
        return stat.S_IMODE(os.lstat(path).st_mode)

    def slurp(self, path, binary=False):
        with open(path, "rb" if binary else "r") as fh:
            return fh.read()

    def ingest(self, body, kind="answer", rid=RID, slug="kai"):
        seed = self.slurp(self.key_path(slug), binary=True)
        verify = base64.b64encode(bytes(SigningKey(seed).verify_key)).decode()
        pending = {"request_id": rid, "persona": slug, "nonce": "n0",
                   "expires_ms": int(time.time() * 1000) + 60000, "pii_mode": True}
        if kind == "bot_list":
            pending["kind"] = "bot_list"
        return botreply.ingest(
            body, self.station, {"killed": False, "keys": {slug: verify}}, {slug},
            lambda _rid: pending, set(), botreply.Limiter(),
            now_ms=int(time.time() * 1000),
            source=(lambda: slug) if kind == "bot_list" else None)

    def rewrite_endpoint(self, endpoint, slug="kai"):
        path = self.config_path(slug)
        doc = json.loads(self.slurp(path))
        doc["endpoint"] = endpoint
        raw = (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
        self.assertEqual(self.mode(path), 0o600)

    def test_source_posts_once_and_imports_neither_relay_nor_handoff(self):
        src = self.slurp(SCRIPT)
        setup = self.slurp(SETUP)
        common_src = self.slurp(COMMON)
        self.assertTrue(src.startswith("#!/usr/bin/python3 -I\n"))
        self.assertTrue(setup.startswith("#!/usr/bin/python3 -I\n"))
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        self.assertTrue(os.access(SETUP, os.X_OK))
        self.assertEqual(src.count(".request("), 1)
        self.assertIn('"/voice/reply"', src)
        self.assertNotIn(".request(", setup)
        self.assertNotIn('add_parser("keygen"', src)
        self.assertNotIn('add_parser("init"', src)
        self.assertNotIn('add_parser("reply"', setup)
        for blob in (src, setup, common_src):
            self.assertNotIn("import relay", blob)
            self.assertNotIn("import handoff", blob)
            self.assertNotIn("os.system", blob)
            self.assertNotIn("eval(", blob)
            self.assertNotIn("exec(", blob)
            self.assertNotIn("shell=True", blob)
            self.assertNotIn("GROK_STATION_BOT_KEY", blob)
            self.assertNotIn("GS_STATION", blob)
        self.assertNotIn("subprocess", src)
        self.assertIn("subprocess.run", setup)
        self.assertIn('["/usr/bin/tmutil", "addexclusion", root]', setup)
        self.assertNotIn("addexclusion", common_src)
        self.assertIn('["/usr/bin/tmutil", "isexcluded", root]', common_src)
        self.assertIn('["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]', common_src)
        keygen_body = setup.split("def cmd_keygen", 1)[1].split("\ndef ", 1)[0]
        self.assertLess(keygen_body.index("_ensure_private_dir(root)"), keygen_body.index("exclude_from_time_machine(root)"))
        self.assertLess(keygen_body.index("load_nacl("), keygen_body.index("from nacl"))
        seal_body = src.split("def seal_outer", 1)[1].split("\ndef ", 1)[0]
        self.assertLess(seal_body.index("load_nacl("), seal_body.index("from nacl"))
        self.assertIn('"-m", "pip", "install"', setup)
        self.assertIn('"--require-hashes"', setup)
        self.assertIn('"--no-deps"', setup)
        self.assertIn('"--only-binary", ":all:"', setup)
        init_body = setup.split("def cmd_init", 1)[1].split("\ndef ", 1)[0]
        self.assertLess(init_body.index("os.path.lexists(path)"), init_body.index("_write_private"))
        reply_body = src.split("def cmd_reply", 1)[1].split("\ndef ", 1)[0]
        self.assertLess(reply_body.index("require_time_machine_exclusion(root)"), reply_body.index("read_key("))
        self.assertLess(reply_body.index("require_same_host()"), reply_body.index("read_key("))
        self.assertIn('sys.platform != "darwin"', src)
        self.assertIn('sys.platform != "darwin"', setup)
        self.assertIn("except Exception:", src)
        self.assertIn("except Exception:", setup)
        self.assertEqual(gs.SENT_KEEP_SECONDS, 7 * 24 * 60 * 60)
        self.assertEqual(gs.REQUEST_ID.pattern, handoff.REQUEST_ID.pattern)
        self.assertEqual(gs.SLUG.pattern, handoff.BOT_SLUG.pattern)
        self.assertTrue(gs.slug_ok("kai") and handoff.bot_slug_ok("kai"))
        self.assertFalse(gs.slug_ok("grok") or handoff.bot_slug_ok("grok"))
        self.assertIsNone(gs.secret_lint(ANSWER))
        self.assertEqual(gs.secret_lint(None), "error")
        self.assertEqual(gs.ANSWER_MAX, 400)

    def test_keygen_is_once_and_a_wide_dir_is_not_tightened(self):
        seed = self.provision()
        again = self.cli(["keygen", "--persona", "kai"])
        self.assertEqual(again, (1, "", "refused\n"))
        self.assertEqual(self.slurp(self.key_path(), binary=True), seed)
        self.assertEqual(self.mode(self.key_path()), 0o600)
        other = self.cli(["keygen", "--persona", "grok"])
        self.assertEqual(other, (1, "", "refused\n"))
        wide = os.path.join(self.home, "wide")
        os.makedirs(os.path.join(wide, ".config", "grok-station"))
        os.chmod(os.path.join(wide, ".config", "grok-station"), 0o755)
        refused = self.cli(["keygen", "--persona", "jules"], HOME=wide)
        target = os.path.join(wide, ".config", "grok-station")
        self.assertEqual(refused, (1, "", "refused\n"))
        self.assertEqual(self.mode(target), 0o755)
        self.assertFalse(os.path.exists(os.path.join(target, "keys")))

    def test_init_normalizes_pin_and_refuses_a_wide_key(self):
        self.cli(["keygen", "--persona", "kai"])
        colon = ":".join(self.pin[i:i + 2] for i in range(0, 64, 2)).upper()
        messy = "\n".join(self.station_pub[i:i + 8] for i in range(0, len(self.station_pub), 8))
        wrote = self.cli(["init", "--persona", "kai", "--station", "https://127.0.0.1:8443",
                          "--pin-sha256", colon, "--station-pub", messy])
        self.assertEqual(wrote[:2], (0, ""))
        expect = (json.dumps({"endpoint": "https://127.0.0.1:8443", "pin": self.pin,
                              "station_pub": self.station_pub},
                             sort_keys=True, separators=(",", ":")) + "\n").encode()
        self.assertEqual(self.slurp(self.config_path(), binary=True), expect)
        self.assertEqual(self.mode(self.config_path()), 0o600)
        os.chmod(self.key_path(), 0o644)
        os.remove(self.config_path())
        refused = self.cli(["init", "--persona", "kai", "--station", "https://127.0.0.1:8443",
                            "--pin-sha256", self.pin, "--station-pub", self.station_pub])
        self.assertEqual(refused[0], 1)
        self.assertEqual(refused[2], "refused\n")
        self.assertEqual(self.mode(self.key_path()), 0o644)
        self.assertFalse(os.path.exists(self.config_path()))

    def test_answer_round_trip_uses_the_config_station_not_the_environment(self):
        seed = self.provision()
        self.rewrite_endpoint("https://127.0.0.1:9443")
        (rc, out, err), tls = self.reply(ANSWER + "\n")
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(tls.errors, [])
        self.assertEqual(tls.connects, [("127.0.0.1", 9443)])
        self.assertEqual(len(tls.posts), 1)
        head, body = tls.posts[0]
        self.assertTrue(head.startswith(b"POST /voice/reply HTTP/1.1\r\n"))
        self.assertIn(b"Host: 127.0.0.1:9443\r\n", head)
        self.assertNotIn(b"192.0.2.9", head)
        self.assertNotIn(ANSWER.encode(), head)
        self.assertEqual(set(json.loads(body)), {"v", "bot", "sealed"})
        self.assertNotIn(base64.b64encode(seed), body)
        got = self.ingest(body)
        self.assertEqual(got["text"], ANSWER)
        self.assertEqual(got["request_id"], RID)
        self.assertEqual((got["tier"], got["action_class"], got["bot"]), (0, "answer", "kai"))
        (rc2, out2, err2), tls2 = self.reply("Soup on Wednesday.", rid=RID2)
        self.assertEqual((rc2, out2, err2), (0, "", ""))
        self.assertEqual(tls2.connects, [("127.0.0.1", 9443)])
        self.assertEqual(self.ingest(tls2.posts[0][1], rid=RID2)["text"], "Soup on Wednesday.")
        log = self.slurp(self.log_path())
        self.assertRegex(log, rf"^request_id={RID} persona=kai status=200 ms=\d+\n"
                          rf"request_id={RID2} persona=kai status=200 ms=\d+\n$")
        for banned in (ANSWER, "Soup on Wednesday.", base64.b64encode(seed).decode(), self.station_pub):
            self.assertNotIn(banned, log)
        self.assertEqual(self.mode(self.log_path()), 0o600)
        self.assertEqual(self.sent_ids(), [RID, RID2])
        self.assertNotIn(self.hw_uuid, log)
        self.assertEqual(self.mode(self.sent_path()), 0o600)

    def test_bot_list_round_trip(self):
        self.provision()
        text = "Kai\nJules\n"
        (rc, out, err), tls = self.reply(text, kind="bot_list")
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(len(tls.posts), 1)
        got = self.ingest(tls.posts[0][1], kind="bot_list")
        self.assertEqual(got["names"], ["Kai", "Jules"])
        self.assertEqual(got["kind"], "bot_list")
        log = self.slurp(self.log_path())
        self.assertNotIn("Jules", log)
        self.assertNotIn("Kai", log)
        self.assertRegex(log, rf"^request_id={RID} persona=kai status=200 ms=\d+\n$")
        (rc2, _, err2), tls2 = self.reply("Kai\nkai\n", kind="bot_list", rid=RID2)
        self.assertEqual((rc2, err2), (1, "refused\n"))
        self.assertEqual(tls2.connects, [])
        self.assertNotIn(RID2, self.slurp(self.sent_path()))

    def test_bad_request_id_and_overlong_answer_do_not_post(self):
        self.provision()
        bad = ("vs_short", "vs_" + "a" * 21, "vs_" + "a" * 23, "nope_" + "b" * 19, "vs_" + "a" * 21 + "!")
        for rid in bad:
            (rc, out, err), tls = self.reply(ANSWER, rid=rid)
            self.assertEqual((rc, out, err), (1, "", "refused\n"), rid)
            self.assertEqual(tls.connects, [], rid)
        self.assertFalse(os.path.exists(self.sent_path()))
        self.assertFalse(os.path.exists(self.log_path()))
        over = ("ok " * 133) + "!!"
        exact = ("ok " * 133) + "!"
        self.assertEqual((len(exact), len(over)), (400, 401))
        (rc, out, err), tls = self.reply(over)
        self.assertEqual((rc, out, err), (1, "", "refused\n"))
        self.assertEqual(tls.connects, [])
        self.assertFalse(os.path.exists(self.sent_path()))
        (rc, out, err), tls = self.reply("")
        self.assertEqual((rc, err), (1, "refused\n"))
        self.assertEqual(tls.connects, [])
        (rc, out, err), tls = self.reply(exact)
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(len(tls.posts), 1)
        self.assertEqual(len(self.ingest(tls.posts[0][1])["text"]), 400)

    def test_lint_hit_does_not_read_the_key_or_post(self):
        self.provision()
        samples = [
            "-----BEGIN PRIVATE KEY-----\nabc",
            "sk-proj-abcdef12extra",
            "ghp_abcdefghijklmnop",
            "AKIAIOSFODNN7EXAMPLE",
            "Bearer abcdefgh",
            "the password is swordfish",
            "see https://user:swordfish@192.0.2.1/x",
            "ab" * 16,
            "Abcdefghij" * 4,
        ]
        samples += [_fullwidth(text) for text in list(samples)]
        reads = []

        def watch(_slug):
            reads.append("read")
            raise AssertionError("key was read")

        with _patched(gs, "read_key", watch):
            for text in samples:
                (rc, out, err), tls = self.reply(text)
                self.assertEqual((rc, out, err), (1, "", "refused\n"), text[:24])
                self.assertEqual(tls.connects, [], text[:24])
                self.assertEqual(tls.posts, [], text[:24])
                self.assertNotIn(text, err)
            def boom(text):
                raise RuntimeError(text)
            with _patched(gs, "secret_lint", boom):
                leak = "lint-exception-must-not-be-logged"
                (rc, out, err), tls = self.reply(leak)
                self.assertEqual((rc, out, err), (1, "", "refused\n"))
                self.assertEqual(tls.connects, [])
                self.assertNotIn(leak, err)
            samples.append(leak)
        self.assertEqual(reads, [])
        self.assertFalse(os.path.exists(self.sent_path()))
        log = self.slurp(self.log_path())
        lines = log.splitlines()
        self.assertEqual(lines[-1], "lint_rejects=%d" % len(lines))
        for line in lines:
            self.assertRegex(line, r"^lint_rejects=\d+$")
        for text in samples:
            self.assertNotIn(text, log)
        self.assertEqual(self.mode(self.log_path()), 0o600)
        (rc, _, err), tls = self.reply("password is swordfish", kind="bot_list", rid=RID2)
        self.assertEqual((rc, err), (1, "refused\n"))
        self.assertEqual(tls.connects, [])
        self.assertNotIn("password is swordfish", self.slurp(self.log_path()))

    def test_stdin_over_one_mibibyte_is_refused_before_the_key(self):
        self.provision()
        blob = b"z" * (1024 * 1024 + 1)
        reads = []
        with _patched(gs, "read_key", lambda _slug: reads.append("read")):
            (rc, out, err), tls = self.reply(blob)
        self.assertEqual((rc, out, err), (1, "", "refused\n"))
        self.assertEqual(reads, [])
        self.assertEqual(tls.connects, [])
        self.assertFalse(os.path.exists(self.sent_path()))

    def test_http_failure_posts_once_and_a_second_send_does_not_connect(self):
        seed = self.provision()
        (rc, out, err), tls = self.reply(ANSWER, status=500)
        self.assertEqual((rc, out, err), (1, "", "refused\n"))
        self.assertEqual(len(tls.posts), 1)
        self.assertEqual(len(tls.connects), 1)
        self.assertTrue(tls.posts[0][0].startswith(b"POST /voice/reply "))
        (rc2, out2, err2), tls2 = self.reply(ANSWER)
        self.assertEqual((rc2, out2, err2), (1, "", "refused\n"))
        self.assertEqual(tls2.connects, [])
        self.assertEqual(tls2.posts, [])
        log = self.slurp(self.log_path())
        self.assertRegex(log, rf"^request_id={RID} persona=kai status=500 ms=\d+\n$")
        self.assertNotIn(ANSWER, log)
        self.assertNotIn(base64.b64encode(seed).decode(), log)
        self.assertEqual(self.sent_ids(), [RID])
        self.assertEqual(self.mode(self.log_path()), 0o600)

    def test_wrong_pin_sends_no_request_and_does_not_retry(self):
        seed = self.provision(pin="ab" * 32)
        (rc, out, err), tls = self.reply(ANSWER)
        self.assertEqual((rc, out, err), (1, "", "refused\n"))
        self.assertEqual(len(tls.connects), 1)
        self.assertEqual(tls.posts, [])
        (rc2, _, err2), tls2 = self.reply(ANSWER)
        self.assertEqual((rc2, err2), (1, "refused\n"))
        self.assertEqual(tls2.connects, [])
        log = self.slurp(self.log_path())
        self.assertRegex(log, rf"^request_id={RID} persona=kai status=0 ms=\d+\n$")
        self.assertNotIn(ANSWER, log)
        self.assertNotIn(base64.b64encode(seed).decode(), log)
        self.assertEqual(self.sent_ids(), [RID])

    def test_connect_error_does_not_retry(self):
        self.provision()
        calls = []
        real = socket.create_connection

        def boom(address, timeout=None, source_address=None):
            calls.append(address)
            raise OSError(errno.EPERM, "nope")

        socket.create_connection = boom
        try:
            rc, out, err = self.call(["reply", "--persona", "kai", "--request-id", RID], ANSWER.encode())
            rc2, _, err2 = self.call(["reply", "--persona", "kai", "--request-id", RID], ANSWER.encode())
        finally:
            socket.create_connection = real
        self.assertEqual(calls, [("127.0.0.1", 8443)])
        self.assertEqual((rc, out, err), (1, "", "refused\n"))
        self.assertEqual((rc2, err2), (1, "refused\n"))
        log = self.slurp(self.log_path())
        self.assertRegex(log, rf"^request_id={RID} persona=kai status=0 ms=\d+\n$")
        self.assertNotIn(ANSWER, log)

    def test_wide_perms_and_symlinks_are_left_alone(self):
        self.provision()
        key = self.key_path()
        keys = os.path.join(self.root(), "keys")
        checks = (
            (key, 0o644),
            (self.config_path(), 0o644),
            (self.host_path(), 0o644),
            (keys, 0o755),
            (self.root(), 0o755),
        )
        for path, mode in checks:
            os.chmod(path, mode)
            (rc, out, err), tls = self.reply(ANSWER)
            self.assertEqual((rc, out, err), (1, "", "refused\n"), path)
            self.assertEqual(self.mode(path), mode, path)
            self.assertEqual(tls.connects, [], path)
            self.assertFalse(os.path.exists(self.sent_path()), path)
            os.chmod(path, 0o600 if stat.S_ISREG(os.lstat(path).st_mode) else 0o700)
        sent = self.sent_path()
        fd = os.open(sent, os.O_CREAT | os.O_WRONLY, 0o644)
        os.close(fd)
        os.chmod(sent, 0o644)
        (rc, _, err), tls = self.reply(ANSWER)
        self.assertEqual((rc, err), (1, "refused\n"))
        self.assertEqual(self.mode(sent), 0o644)
        self.assertEqual(self.slurp(sent, binary=True), b"")
        self.assertEqual(tls.connects, [])
        os.remove(sent)
        target = os.path.join(self.home, "sent-target")
        with open(target, "w") as fh:
            fh.write("keep\n")
        os.symlink(target, sent)
        (rc, _, err), tls = self.reply(ANSWER)
        self.assertEqual((rc, err), (1, "refused\n"))
        self.assertTrue(os.path.islink(sent))
        self.assertEqual(self.slurp(target), "keep\n")
        self.assertEqual(tls.connects, [])
        os.remove(sent)
        raw = self.slurp(key, binary=True)
        os.remove(key)
        elsewhere = os.path.join(self.home, "elsewhere.key")
        fd = os.open(elsewhere, os.O_CREAT | os.O_WRONLY, 0o600)
        os.write(fd, raw)
        os.close(fd)
        os.symlink(elsewhere, key)
        (rc, _, err), tls = self.reply(ANSWER)
        self.assertEqual((rc, err), (1, "refused\n"))
        self.assertTrue(os.path.islink(key))
        self.assertEqual(self.slurp(elsewhere, binary=True), raw)
        self.assertEqual(self.mode(elsewhere), 0o600)
        self.assertEqual(tls.connects, [])

    def test_other_owner_is_refused_and_tighter_modes_still_post(self):
        seed = self.provision()
        real_euid = os.geteuid
        with _patched(os, "geteuid", lambda: real_euid() + 1):
            (rc, out, err), tls = self.reply(ANSWER)
        self.assertEqual((rc, out, err), (1, "", "refused\n"))
        self.assertEqual(tls.connects, [])
        self.assertEqual(self.mode(self.key_path()), 0o600)
        self.assertFalse(os.path.exists(self.sent_path()))
        bad = type("S", (), {})()
        bad.st_mode = stat.S_IFREG | 0o600
        bad.st_uid = real_euid() + 1
        self.assertFalse(gs.access_ok(bad, 0o600))
        link = type("S", (), {})()
        link.st_mode = stat.S_IFLNK | 0o777
        link.st_uid = real_euid()
        self.assertFalse(gs.access_ok(link, 0o600))
        os.chmod(self.key_path(), 0o400)
        os.chmod(self.config_path(), 0o400)
        os.chmod(os.path.join(self.root(), "keys"), 0o500)
        (rc, out, err), tls = self.reply(ANSWER)
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(len(tls.posts), 1)
        self.assertEqual(self.ingest(tls.posts[0][1])["text"], ANSWER)
        self.assertEqual(self.mode(self.key_path()), 0o400)
        self.assertEqual(self.mode(self.config_path()), 0o400)
        self.assertEqual(self.mode(os.path.join(self.root(), "keys")), 0o500)
        self.assertNotIn(base64.b64encode(seed).decode(), self.slurp(self.log_path()))
        self.assertNotIn(ANSWER, self.slurp(self.log_path()))

    def test_argv_and_unknown_flags_never_carry_the_reply_text(self):
        answer = "Plain speech about Tuesday tacos."
        self.provision()
        wrapper = (
            "import time, sys, runpy\n"
            "time.sleep(0.4)\n"
            "sys.platform = 'darwin'\n"
            "script = sys.argv[1]\n"
            "sys.argv = sys.argv[1:]\n"
            "runpy.run_path(script, run_name='__main__')\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", wrapper, SCRIPT, "reply", "--persona", "kai", "--request-id", RID],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.child_env())
        proc.stdin.write(answer.encode())
        proc.stdin.flush()
        cmd = ""
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            cmd = self.slurp(f"/proc/{proc.pid}/cmdline", binary=True).replace(b"\x00", b" ").decode()
            state = self.slurp(f"/proc/{proc.pid}/status")
            if "\nState:\tS" in state:
                break
            time.sleep(0.02)
        envb = self.slurp(f"/proc/{proc.pid}/environ", binary=True).decode("utf-8", "replace")
        self.assertNotIn(answer, cmd)
        self.assertNotIn(answer, envb)
        self.assertNotIn(answer, " ".join(proc.args))
        proc.stdin.close()
        out = proc.stdout.read()
        err = proc.stderr.read()
        proc.wait(timeout=20)
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn(answer.encode(), out + err)
        self.assertEqual(err, b"refused\n", err.decode()[-1500:])
        if os.path.exists(self.log_path()):
            self.assertNotIn(answer, self.slurp(self.log_path()))
        extra = self.call(["reply", "--persona", "kai", "--request-id", RID, answer])
        self.assertEqual(extra, (2, "", "refused\n"))
        self.assertNotIn(answer, extra[2])
        unknown = self.call(["reply", "--persona", "kai", "--request-id", RID, "--station", "https://192.0.2.8"])
        self.assertEqual(unknown, (2, "", "refused\n"))
        missing = self.call([])
        self.assertEqual(missing, (2, "", "refused\n"))

    def test_reply_parser_has_no_init_or_keygen(self):
        for cmd in ("init", "keygen"):
            self.assertEqual(self.call([cmd, "--persona", "kai"]), (2, "", "refused\n"), cmd)
        reply_cmds = set()
        setup_cmds = set()
        for action in gs._parser()._actions:
            if isinstance(action, argparse._SubParsersAction):
                reply_cmds = set(action.choices)
        for action in su._parser()._actions:
            if isinstance(action, argparse._SubParsersAction):
                setup_cmds = set(action.choices)
        self.assertEqual(reply_cmds, {"reply"})
        self.assertEqual(setup_cmds, {"keygen", "init", "vendor-install"})

    def test_init_refuses_to_overwrite_config(self):
        self.provision()
        original = self.slurp(self.config_path(), binary=True)
        again = self.cli(["init", "--persona", "kai", "--station", "https://127.0.0.1:9",
                          "--pin-sha256", "cd" * 32, "--station-pub", self.station_pub])
        self.assertEqual(again, (1, "", "refused\n"))
        self.assertEqual(self.slurp(self.config_path(), binary=True), original)
        self.assertEqual(self.mode(self.config_path()), 0o600)
        os.remove(self.config_path())
        target = os.path.join(self.home, "other.json")
        with open(target, "w") as fh:
            fh.write("keep\n")
        os.symlink(target, self.config_path())
        again = self.cli(["init", "--persona", "kai", "--station", "https://127.0.0.1:8443",
                          "--pin-sha256", self.pin, "--station-pub", self.station_pub])
        self.assertEqual(again, (1, "", "refused\n"))
        self.assertTrue(os.path.islink(self.config_path()))
        self.assertEqual(self.slurp(target), "keep\n")

    def test_main_refuses_unless_darwin(self):
        with _patched(sys, "platform", "linux"):
            self.assertEqual(self.cli(["keygen", "--persona", "kai"]), (1, "", "refused\n"))
            self.assertFalse(os.path.exists(self.root()))
            self.assertEqual(self.commands, [])
            rc, out, err = self.call(["reply", "--persona", "kai", "--request-id", RID], ANSWER.encode())
            self.assertEqual((rc, out, err), (1, "", "refused\n"))

    def test_unexpected_errors_are_refused_without_detail(self):
        def boom(_root):
            raise RuntimeError("do-not-print-this-secret")
        with _patched(su, "exclude_from_time_machine", boom):
            rc, out, err = self.cli(["keygen", "--persona", "kai"])
        self.assertEqual((rc, out, err), (1, "", "refused\n"))
        self.assertNotIn("secret", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.key_path()))
        self.provision()
        def leak(_slug):
            raise RuntimeError(ANSWER)
        with _patched(gs, "read_key", leak):
            (rc, out, err), tls = self.reply(ANSWER)
        self.assertEqual((rc, out, err), (1, "", "refused\n"))
        self.assertNotIn(ANSWER, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(tls.connects, [])

    def test_seed_buffer_is_wiped_after_seal(self):
        seed = bytearray(self.provision())
        body = gs.seal_outer("kai", RID, "answer", ANSWER, seed, bytes(self.station.public_key))
        self.assertEqual(bytes(seed), b"\x00" * 32)
        self.assertNotIn(b"\x00" * 32, body)
        self.assertEqual(self.ingest(body)["text"], ANSWER)

    def test_bad_personas_are_refused_before_post(self):
        self.provision()
        bad = ("../x", "a/b", "grok", "Kai", "a" * 25)
        keys_before = set(os.listdir(os.path.join(self.root(), "keys")))
        for slug in bad:
            self.assertEqual(self.cli(["keygen", "--persona", slug]), (1, "", "refused\n"), slug)
            refused = self.cli(["init", "--persona", slug, "--station", "https://127.0.0.1:8443",
                                "--pin-sha256", self.pin, "--station-pub", self.station_pub])
            self.assertEqual(refused, (1, "", "refused\n"), slug)
            (rc, out, err), tls = self.reply(ANSWER, persona=slug)
            self.assertEqual((rc, out, err), (1, "", "refused\n"), slug)
            self.assertEqual(tls.connects, [], slug)
        self.assertEqual(set(os.listdir(os.path.join(self.root(), "keys"))), keys_before)
        self.assertFalse(os.path.exists(os.path.join(self.root(), "x.key")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "x.key")))
        self.assertFalse(os.path.isdir(os.path.join(self.root(), "keys", "a")))

    def test_keygen_refuses_when_time_machine_exclusion_fails(self):
        self.tm_fail = True
        rc, out, err = self.cli(["keygen", "--persona", "kai"])
        self.assertEqual((rc, out, err), (1, "", "refused\n"))
        self.assertFalse(os.path.exists(self.key_path()))
        self.assertFalse(os.path.exists(self.host_path()))
        self.assertNotIn("CalledProcessError", err)
        self.assertNotIn("Traceback", err)

    def test_reply_refuses_unless_time_machine_excludes_the_root(self):
        self.provision()
        self.tm_excluded = False
        self.commands.clear()
        reads = []
        with _patched(gs, "read_key", lambda slug: reads.append(slug)):
            (rc, out, err), tls = self.reply(ANSWER)
        self.assertEqual((rc, out, err), (1, "", "refused\n"))
        self.assertEqual(reads, [])
        self.assertEqual(tls.connects, [])
        self.assertIn(["/usr/bin/tmutil", "isexcluded", self.root()], self.commands)
        self.assertFalse(os.path.exists(self.sent_path()))
        self.assertNotIn(ANSWER, err)

    def test_host_mismatch_or_missing_file_refuses(self):
        self.provision()
        path = self.host_path()
        os.remove(path)
        reads = []
        with _patched(gs, "read_key", lambda slug: reads.append(slug)):
            (rc, out, err), tls = self.reply(ANSWER)
        self.assertEqual((rc, out, err), (1, "", "refused\n"))
        self.assertEqual(reads, [])
        self.assertEqual(tls.connects, [])
        other = "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        os.write(fd, (other + "\n").encode())
        os.close(fd)
        (rc, out, err), tls = self.reply(ANSWER)
        self.assertEqual((rc, out, err), (1, "", "refused\n"))
        self.assertEqual(tls.connects, [])
        self.assertEqual(self.slurp(path), other + "\n")
        self.assertNotIn(other, err)
        self.assertNotIn(self.hw_uuid, err)
        self.assertEqual(self.mode(path), 0o600)
        os.remove(path)
        target = os.path.join(self.home, "host-elsewhere")
        with open(target, "w") as fh:
            fh.write(self.hw_uuid + "\n")
        os.symlink(target, path)
        (rc, out, err), tls = self.reply(ANSWER)
        self.assertEqual((rc, err), (1, "refused\n"))
        self.assertTrue(os.path.islink(path))
        self.assertEqual(self.slurp(target), self.hw_uuid + "\n")
        self.assertEqual(tls.connects, [])

    def test_second_persona_keeps_the_same_host_id(self):
        self.provision()
        rc, out, err = self.cli(["keygen", "--persona", "jules"])
        self.assertEqual((rc, err), (0, ""), err)
        self.assertNotIn(self.hw_uuid, out)
        self.assertEqual(self.slurp(self.host_path()), self.hw_uuid + "\n")
        self.assertEqual(self.mode(self.key_path("jules")), 0o600)
        self.assertEqual(self.mode(self.host_path()), 0o600)

    def test_refuses_git_parents_and_sync_folders(self):
        def fresh():
            return tempfile.mkdtemp(dir=self.home)

        home = fresh()
        os.makedirs(os.path.join(home, ".git"))
        self.assertEqual(self.cli(["keygen", "--persona", "kai"], HOME=home), (1, "", "refused\n"))
        self.assertFalse(os.path.exists(os.path.join(home, ".config", "grok-station")))
        init = self.cli(["init", "--persona", "kai", "--station", "https://127.0.0.1:8443",
                         "--pin-sha256", self.pin, "--station-pub", self.station_pub], HOME=home)
        self.assertEqual(init, (1, "", "refused\n"))
        self.assertFalse(os.path.exists(os.path.join(home, ".config", "grok-station", "kai.json")))

        home = fresh()
        os.makedirs(os.path.join(home, ".config"))
        with open(os.path.join(home, ".config", ".git"), "w") as fh:
            fh.write("gitdir: /tmp/not-used\n")
        self.assertEqual(self.cli(["keygen", "--persona", "kai"], HOME=home), (1, "", "refused\n"))
        self.assertFalse(os.path.exists(os.path.join(home, ".config", "grok-station")))

        for parts in (
            ("Dropbox", "box"),
            ("Library", "Mobile Documents", "docs"),
            ("Library", "CloudStorage", "docs"),
            ("OneDrive - Personal", "docs"),
        ):
            home = fresh()
            cloud = os.path.join(home, *parts)
            os.makedirs(cloud)
            os.symlink(cloud, os.path.join(home, ".config"))
            refused = self.cli(["keygen", "--persona", "kai"], HOME=home)
            self.assertEqual(refused, (1, "", "refused\n"), parts)
            self.assertFalse(os.path.exists(os.path.join(cloud, "grok-station")), parts)

        home = fresh()
        os.makedirs(os.path.join(home, "OneNote"))
        os.makedirs(os.path.join(home, "DropboxOther"))
        rc, out, err = self.cli(["keygen", "--persona", "kai"], HOME=home)
        self.assertEqual((rc, err), (0, ""), err)
        self.assertTrue(os.path.isfile(os.path.join(home, ".config", "grok-station", "keys", "kai.key")))

        self.provision()
        os.makedirs(os.path.join(self.home, ".git"))
        reads = []
        with _patched(gs, "read_key", lambda slug: reads.append(slug)):
            (rc, out, err), tls = self.reply(ANSWER)
        self.assertEqual((rc, out, err), (1, "", "refused\n"))
        self.assertEqual(reads, [])
        self.assertEqual(tls.connects, [])
        self.assertNotIn(ANSWER, err)

    def test_sent_ids_drop_lines_older_than_seven_days(self):
        self.provision()
        old_rid = "vs_" + "c" * 22
        keep_rid = "vs_" + "d" * 22
        now = int(time.time())
        payload = ("%s %d\n%s %d\nnot-a-valid-line\n" % (
            old_rid, now - 8 * 24 * 60 * 60, keep_rid, now - 60)).encode()
        fd = os.open(self.sent_path(), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        os.write(fd, payload)
        os.close(fd)
        (rc, out, err), tls = self.reply(ANSWER)
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(len(tls.posts), 1)
        text = self.slurp(self.sent_path())
        self.assertNotIn(old_rid, text)
        self.assertNotIn("not-a-valid-line", text)
        self.assertNotIn(ANSWER, text)
        self.assertIn(keep_rid, text)
        self.assertIn(RID, text)
        self.assertEqual(self.mode(self.sent_path()), 0o600)

    def test_recent_duplicate_trims_old_ids_and_does_not_post(self):
        self.provision()
        old_rid = "vs_" + "e" * 22
        now = int(time.time())
        payload = ("%s %d\n%s %d\n" % (old_rid, now - 8 * 24 * 60 * 60, RID, now - 30)).encode()
        fd = os.open(self.sent_path(), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        os.write(fd, payload)
        os.close(fd)
        (rc, out, err), tls = self.reply(ANSWER)
        self.assertEqual((rc, err), (1, "refused\n"))
        self.assertEqual(tls.connects, [])
        text = self.slurp(self.sent_path())
        self.assertNotIn(old_rid, text)
        self.assertIn(RID, text)
        self.assertNotIn(ANSWER, text)
        self.assertFalse(os.path.exists(self.log_path()))

    def test_setup_only_helpers_stay_off_reply_and_common(self):
        names = ("exclude_from_time_machine", "record_host_id", "_ensure_private_dir", "_write_private")
        for name in names:
            self.assertNotIn(name, vars(gs), name)
            self.assertNotIn(name, vars(gsc), name)
            self.assertIn(name, vars(su), name)

    def test_script_tree_refuses_group_write_and_symlinked_common(self):
        for mod in (gs, su):
            dirs = mod._stdlib_dirs()
            self.assertTrue(dirs)
            stdlib = os.path.normpath(os.path.realpath(os.path.dirname(os.__file__)))
            self.assertIn(stdlib, dirs)
            self.assertIn(os.path.join(stdlib, "lib-dynload"), dirs)
            for path in dirs:
                parts = path.split(os.sep)
                self.assertNotIn("site-packages", parts, path)
                self.assertNotIn("dist-packages", parts, path)
            head = self.slurp(SCRIPT if mod is gs else SETUP).split("from gs_seal_common import", 1)[0]
            self.assertIn("sys.dont_write_bytecode = True", head)
            self.assertIn("sys.path =", head)
            self.assertNotIn("sys.path.insert", head)
            self.assertLess(head.index("_enforce_script_tree("), head.index("sys.path ="))
            base = tempfile.mkdtemp(dir=self.home)
            script_dir = os.path.join(base, "bin")
            os.mkdir(script_dir, 0o755)
            script = os.path.join(script_dir, "gs-seal-reply")
            common = os.path.join(script_dir, "gs_seal_common.py")
            with open(script, "w") as fh:
                fh.write("#!/usr/bin/python3 -I\n")
            with open(common, "w") as fh:
                fh.write("# common\n")
            os.chmod(base, 0o755)
            os.chmod(script_dir, 0o755)
            os.chmod(script, 0o755)
            os.chmod(common, 0o644)
            uid = os.geteuid()
            self.assertTrue(mod.script_tree_ok(script, uid, base))
            self.assertFalse(mod.script_tree_ok(script, uid + 1, base))
            os.chmod(script_dir, 0o775)
            self.assertFalse(mod.script_tree_ok(script, uid, base))
            os.chmod(script_dir, 0o707)
            self.assertFalse(mod.script_tree_ok(script, uid, base))
            os.chmod(script_dir, 0o755)
            self.assertTrue(mod.script_tree_ok(script, uid, base))
            os.remove(common)
            real = os.path.join(base, "real_common.py")
            with open(real, "w") as fh:
                fh.write("# real\n")
            os.chmod(real, 0o644)
            os.symlink(real, common)
            self.assertFalse(mod.script_tree_ok(script, uid, base))
            err = io.StringIO()
            gc.collect()
            with _patched(sys, "stderr", err):
                with self.assertRaises(SystemExit) as caught:
                    mod._enforce_script_tree(script, uid, base)
            self.assertEqual(caught.exception.code, 1)
            self.assertEqual(err.getvalue(), "refused\n")
            self.assertEqual(mod._stdlib_dirs(), gsc._stdlib_dirs())

    def test_symlinked_call_path_refuses_before_resolving(self):
        for mod in (gs, su):
            head = self.slurp(SCRIPT if mod is gs else SETUP)
            body = head.split("def script_tree_ok(", 1)[1].split("\ndef ", 1)[0]
            self.assertLess(body.index("invoked_path_ok(script_file)"), body.index("os.path.realpath"))
            base = os.path.realpath(tempfile.mkdtemp(dir=self.home))
            os.chmod(base, 0o755)
            script_dir = os.path.join(base, "bin")
            os.mkdir(script_dir, 0o755)
            script = os.path.join(script_dir, "gs-seal-reply")
            with open(script, "w") as fh:
                fh.write("#!/usr/bin/python3 -I\n")
            os.chmod(script, 0o755)
            common = os.path.join(script_dir, "gs_seal_common.py")
            with open(common, "w") as fh:
                fh.write("# common\n")
            os.chmod(common, 0o644)
            uid = os.geteuid()
            self.assertTrue(mod.invoked_path_ok(script))
            self.assertTrue(mod.script_tree_ok(script, uid, base))
            link_dir = os.path.join(base, "links")
            os.mkdir(link_dir, 0o755)
            link = os.path.join(link_dir, "gs-seal-reply")
            os.symlink(script, link)
            self.assertFalse(mod.invoked_path_ok(link))
            self.assertFalse(mod.script_tree_ok(link, uid, base))
            dir_link = os.path.join(base, "binlink")
            os.symlink(script_dir, dir_link)
            via_dir = os.path.join(dir_link, "gs-seal-reply")
            self.assertFalse(mod.invoked_path_ok(via_dir))
            self.assertFalse(mod.script_tree_ok(via_dir, uid, base))
            err = io.StringIO()
            with _patched(sys, "stderr", err):
                with self.assertRaises(SystemExit) as caught:
                    mod._enforce_script_tree(link, uid, base)
            self.assertEqual(caught.exception.code, 1)
            self.assertEqual(err.getvalue(), "refused\n")
            self.assertTrue(mod.script_tree_ok(script, uid, base))
            self.assertFalse(mod.invoked_path_ok(""))
            self.assertFalse(mod.invoked_path_ok("a\x00b"))

    def _trusted_vendor(self, home, files):
        vendor = os.path.join(home, ".config", "grok-station", "vendor")
        for rel, data in files.items():
            dest = os.path.join(vendor, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)
        for dirpath, dirnames, filenames in os.walk(home):
            os.chmod(dirpath, 0o700)
            for name in filenames:
                os.chmod(os.path.join(dirpath, name), 0o644)
        os.chmod(home, 0o700)
        manifest = os.path.join(home, ".config", "grok-station", "vendor-manifest.json")
        mapping = {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}
        raw = (json.dumps(mapping, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
        os.chmod(manifest, 0o600)
        return vendor, manifest

    def test_vendor_walk_refuses_writable_symlink_and_foreign_owner(self):
        home = tempfile.mkdtemp(dir=self.home)
        files = {"nacl/__init__.py": b"MARKER = 'gs-seal-fake-nacl'\n"}
        vendor, _manifest = self._trusted_vendor(home, files)
        gsc.assert_vendor_trusted(home, os.geteuid())
        before = list(sys.path)
        with self.assertRaises(gsc.Refused):
            gsc.load_nacl(home, os.geteuid() + 1)
        self.assertEqual(sys.path, before)
        os.chmod(os.path.join(home, ".config"), 0o775)
        with self.assertRaises(gsc.Refused):
            gsc.assert_vendor_trusted(home, os.geteuid())
        os.chmod(os.path.join(home, ".config"), 0o700)
        gsc.assert_vendor_trusted(home, os.geteuid())
        os.chmod(os.path.join(vendor, "nacl", "__init__.py"), 0o666)
        with self.assertRaises(gsc.Refused):
            gsc.assert_vendor_trusted(home, os.geteuid())
        os.chmod(os.path.join(vendor, "nacl", "__init__.py"), 0o644)
        config = os.path.join(home, ".config")
        os.rename(config, config + "-real")
        os.symlink(config + "-real", config)
        with self.assertRaises(gsc.Refused):
            gsc.assert_vendor_trusted(home, os.geteuid())

    def test_vendor_manifest_mismatch_refuses(self):
        files = {"nacl/__init__.py": b"MARKER = 'gs-seal-fake-nacl'\n"}
        uid = os.geteuid()

        home = tempfile.mkdtemp(dir=self.home)
        self._trusted_vendor(home, files)
        path = os.path.join(home, ".config", "grok-station", "vendor", "nacl", "__init__.py")
        with open(path, "wb") as fh:
            fh.write(b"MARKER = 'changed'\n")
        os.chmod(path, 0o644)
        with self.assertRaises(gsc.Refused):
            gsc.assert_vendor_trusted(home, uid)

        home = tempfile.mkdtemp(dir=self.home)
        vendor, _manifest = self._trusted_vendor(home, files)
        extra = os.path.join(vendor, "nacl", "extra.py")
        with open(extra, "wb") as fh:
            fh.write(b"x\n")
        os.chmod(extra, 0o644)
        with self.assertRaises(gsc.Refused):
            gsc.assert_vendor_trusted(home, uid)

        home = tempfile.mkdtemp(dir=self.home)
        vendor, _manifest = self._trusted_vendor(home, files)
        os.remove(os.path.join(vendor, "nacl", "__init__.py"))
        with self.assertRaises(gsc.Refused):
            gsc.assert_vendor_trusted(home, uid)

        home = tempfile.mkdtemp(dir=self.home)
        _vendor, manifest = self._trusted_vendor(home, files)
        os.chmod(manifest, 0o644)
        with self.assertRaises(gsc.Refused):
            gsc.assert_vendor_trusted(home, uid)

    def test_vendor_happy_path_imports_fake_nacl(self):
        home = tempfile.mkdtemp(dir=self.home)
        files = {"nacl/__init__.py": b"MARKER = 'gs-seal-fake-nacl'\n"}
        vendor, _manifest = self._trusted_vendor(home, files)
        saved_path = list(sys.path)
        saved = {}
        for key in list(sys.modules):
            if key == "nacl" or key.startswith("nacl."):
                saved[key] = sys.modules.pop(key)
        try:
            mod = gsc.load_nacl(home, os.geteuid())
            self.assertEqual(mod.MARKER, "gs-seal-fake-nacl")
            self.assertTrue(os.path.realpath(mod.__file__).startswith(os.path.realpath(vendor) + os.sep))
            self.assertEqual(sys.path[0], os.path.abspath(vendor))
            self.assertNotIn(os.path.dirname(SCRIPT), sys.path)
            self.assertIn(os.path.normpath(os.path.realpath(os.path.dirname(os.__file__))), sys.path)
            self.assertIs(sys.modules["gs_seal_common"], gsc)
        finally:
            for key in list(sys.modules):
                if key == "nacl" or key.startswith("nacl."):
                    del sys.modules[key]
            sys.modules.update(saved)
            sys.path[:] = saved_path

    def test_vendor_install_builds_pip_argv_and_manifest(self):
        lock = os.path.join(self.home, "vendor-lock.txt")
        with open(lock, "w") as fh:
            fh.write("# lock\n")
        seen = []

        def fake_run(argv, **kwargs):
            self.assertIsNot(kwargs.get("shell"), True)
            seen.append(list(argv))
            vendor = argv[argv.index("--target") + 1]
            dest = os.path.join(vendor, "nacl", "__init__.py")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(b"MARKER = 'installed'\n")
            os.chmod(os.path.dirname(dest), 0o700)
            os.chmod(dest, 0o644)
            return subprocess.CompletedProcess(list(argv), 0)

        with _patched(su.subprocess, "run", fake_run):
            rc, out, err = self.cli(["vendor-install", "--lock", lock])
        self.assertEqual((rc, out, err), (0, "", ""), err)
        vendor = os.path.join(self.root(), "vendor")
        self.assertEqual(seen, [[
            sys.executable, "-I", "-m", "pip", "install",
            "--require-hashes", "--no-deps", "--only-binary", ":all:",
            "--target", vendor, "-r", lock,
        ]])
        self.assertEqual(self.mode(vendor), 0o700)
        self.assertEqual(self.mode(self.root()), 0o700)
        manifest = os.path.join(self.root(), "vendor-manifest.json")
        self.assertEqual(self.mode(manifest), 0o600)
        doc = json.loads(self.slurp(manifest))
        self.assertEqual(doc, {
            "nacl/__init__.py": hashlib.sha256(b"MARKER = 'installed'\n").hexdigest(),
        })
        gsc.assert_vendor_trusted(self.home, os.geteuid())

    def test_vendor_install_refuses_symlink_and_pip_failure(self):
        home = tempfile.mkdtemp(dir=self.home)
        lock = os.path.join(home, "lock.txt")
        with open(lock, "w") as fh:
            fh.write("x\n")

        def fake_link(argv, **kwargs):
            vendor = argv[argv.index("--target") + 1]
            os.symlink("elsewhere", os.path.join(vendor, "link"))
            return subprocess.CompletedProcess(list(argv), 0)

        with _patched(su.subprocess, "run", fake_link):
            refused = self.cli(["vendor-install", "--lock", lock], HOME=home)
        self.assertEqual(refused, (1, "", "refused\n"))
        self.assertFalse(os.path.exists(os.path.join(home, ".config", "grok-station", "vendor-manifest.json")))

        home = tempfile.mkdtemp(dir=self.home)
        lock = os.path.join(home, "lock.txt")
        with open(lock, "w") as fh:
            fh.write("x\n")

        def fake_fail(argv, **kwargs):
            return subprocess.CompletedProcess(list(argv), 1)

        with _patched(su.subprocess, "run", fake_fail):
            refused = self.cli(["vendor-install", "--lock", lock], HOME=home)
        self.assertEqual(refused, (1, "", "refused\n"))
        self.assertFalse(os.path.exists(os.path.join(home, ".config", "grok-station", "vendor-manifest.json")))

    def test_keygen_refuses_without_a_trusted_vendor(self):
        gs.load_nacl, su.load_nacl = self._load_nacl
        try:
            rc, out, err = self.cli(["keygen", "--persona", "kai"])
        finally:
            gs.load_nacl = self._skip_vendor
            su.load_nacl = self._skip_vendor
        self.assertEqual((rc, out, err), (1, "", "refused\n"))
        self.assertFalse(os.path.exists(self.key_path()))
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()


class StdlibChain(unittest.TestCase):
    """M1: the stdlib dirs put on sys.path are walked like the script tree."""

    def test_group_writable_stdlib_dir_refuses(self):
        for mod in (gs, su, gsc):
            with tempfile.TemporaryDirectory() as tmp:
                lib = os.path.join(tmp, "lib")
                os.mkdir(lib, 0o755)
                os.chmod(tmp, 0o755)
                self.assertTrue(mod._stdlib_chain_ok(lib, os.getuid(), tmp))
                os.chmod(lib, 0o775)
                self.assertFalse(mod._stdlib_chain_ok(lib, os.getuid(), tmp))
                os.chmod(lib, 0o755)
                link = os.path.join(tmp, "link")
                os.symlink(lib, link)
                self.assertFalse(mod._stdlib_chain_ok(link, os.getuid(), tmp))

    def test_root_owned_stdlib_passes_and_foreign_owner_refuses(self):
        base = os.path.normpath(os.path.realpath(os.path.dirname(os.__file__)))
        if os.lstat(base).st_uid != 0:
            self.skipTest("stdlib not root-owned here")
        for mod in (gs, su, gsc):
            self.assertTrue(mod._stdlib_chain_ok(base, 4242, "/"))
            self.assertTrue(mod._stdlib_dirs(uid=4242))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(gs._stdlib_chain_ok(tmp, os.getuid() + 4242 if os.getuid() else 4242, tmp))

    def test_stdlib_dirs_exit_refused_on_bad_chain(self):
        import io, contextlib
        for mod in (gs, su, gsc):
            err = io.StringIO()
            orig = mod._stdlib_chain_ok
            mod._stdlib_chain_ok = lambda *a, **k: False
            try:
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                    mod._stdlib_dirs()
            finally:
                mod._stdlib_chain_ok = orig
            self.assertEqual(cm.exception.code, 1)
            self.assertEqual(err.getvalue(), "refused\n")
