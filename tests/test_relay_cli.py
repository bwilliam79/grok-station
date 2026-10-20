"""Relay CLI: uninstall, key rotation, and status. System effects stay on a fake."""
import contextlib
import datetime
import io
import os
import ssl
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
import grok_relay_cli as cli  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_CANARY = b"PRIVATE-KEY-CANARY\n"
XAI_CANARY = b"xai-live-secret-9999\n"
KEYCHAIN_CANARY = b"password: KEYCHAIN-SECRET-VALUE\n"
SOCKET_CANARY = "SOCKET-CANARY"
ROTATE_KEY = "xai-test-secret-wxyz"
CLOCK = datetime.datetime(2026, 10, 20, 12, 0, 0, tzinfo=datetime.timezone.utc)
LABEL = "label=" + cli.COMPOSE_LABEL


def _cert_pem():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "relay.example.invalid")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(0xABCDEF1234567890)
        .not_valid_before(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
        .not_valid_after(datetime.datetime(2028, 6, 1, tzinfo=datetime.timezone.utc))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


CERT_PEM = _cert_pem()
SERIAL8 = "34567890"
START_LINE = "2026-10-20T12:00:00Z relay local relay-uninstall-start " + SERIAL8 + "\n"
DONE_LINE = "2026-10-20T12:00:00Z relay station relay-uninstall-done -\n"


class FakeSystem:
    """Records every effect and pretends the commands changed a small machine."""

    def __init__(self, trace, platform="linux"):
        self.trace = trace
        self.platform_name = platform
        self.root = True
        self.clock = CLOCK
        self.reads = []
        self.audits = []
        self.unhandled = []
        self.fail_prefix = None
        self.boom_paths = set()
        self.modes = {}
        self.owners = {}
        self.keychain_secret = None
        self._install()

    def _install(self):
        paths = cli.paths_for(self.platform_name)
        self.paths = paths
        self.dirs = {paths.config, paths.state, paths.log, paths.cache}
        self.dirs.update(paths.systemd_dirs)
        if paths.launchd_dir:
            self.dirs.add(paths.launchd_dir)
        self.files = {
            paths.cert: CERT_PEM,
            paths.key: KEY_CANARY,
            paths.ca: b"CA-PIN-CANARY\n",
            paths.xai_key: XAI_CANARY,
            paths.paired: b"yes\n",
            paths.shim: b"#!/bin/sh\n",
        }
        if self.platform_name == "darwin":
            self.files[os.path.join(paths.launchd_dir, "com.example.grok-relay.plist")] = b"<plist/>\n"
            self.files[paths.pf_anchor] = b"pass in proto tcp to any port 443\n"
            self.files[paths.pf_conf] = (
                b'anchor "grok-relay"\nload anchor "grok-relay" from "/etc/pf.anchors/grok-relay"\n'
            )
            self.keychain = True
            self.pf_loaded = True
            self.nft = False
            self.ufw = []
        else:
            self.files["/etc/systemd/system/grok-relay.service"] = b"[Service]\n"
            self.files["/etc/systemd/system/grok-relay-update.timer"] = b"[Timer]\n"
            self.keychain = False
            self.pf_loaded = False
            self.nft = True
            self.ufw = [
                (1, "443/tcp ALLOW IN Anywhere # grok-relay"),
                (2, "8443/tcp ALLOW IN Anywhere # grok-relay"),
                (3, "22/tcp ALLOW IN Anywhere"),
            ]
        self.users = {cli.USER}
        self.groups = {cli.GROUP, cli.DOCKER_GROUP}
        self.docker_members = {cli.USER}
        self.containers = ["c1"]
        self.images = ["i1"]
        self.volumes = ["v1"]
        self.listeners = {443: "grok-relay", 8443: "grok-relay"}
        self.stuck_ports = set()

    def platform(self):
        return self.platform_name

    def is_root(self):
        return self.root

    def now(self):
        return self.clock

    def exists(self, path):
        return path in self.files or path in self.dirs

    def listdir(self, path):
        prefix = path.rstrip("/") + "/"
        names = set()
        for collection in (self.files, self.dirs):
            for entry in collection:
                if entry.startswith(prefix):
                    names.add(entry[len(prefix):].split("/")[0])
        return sorted(names)

    def read_bytes(self, path):
        self.reads.append(path)
        return self.files.get(path)

    def remove_file(self, path):
        self.trace.append(("remove_file", path))
        self.files.pop(path, None)

    def remove_tree(self, path):
        self.trace.append(("remove_tree", path))
        if path in self.boom_paths:
            raise OSError("busy")
        prefix = path.rstrip("/") + "/"
        for entry in list(self.files):
            if entry == path or entry.startswith(prefix):
                self.files.pop(entry, None)
        for entry in list(self.dirs):
            if entry == path or entry.startswith(prefix):
                self.dirs.discard(entry)

    def write_file(self, path, data):
        self.trace.append(("write_file", path))
        self.files[path] = data

    def write_secret(self, path, data, mode, owner):
        self.trace.append(("write_secret", path, mode, owner))
        self.files[path] = data
        self.modes[path] = mode
        self.owners[path] = owner
        parent = os.path.dirname(path)
        if parent:
            self.dirs.add(parent)

    def append_audit(self, path, line):
        self.trace.append(("audit", line))
        self.audits.append(line)
        self.files[path] = self.files.get(path, b"") + line.encode("ascii")
        parent = os.path.dirname(path)
        if parent:
            self.dirs.add(parent)

    def user_exists(self, name):
        return name in self.users

    def group_exists(self, name):
        return name in self.groups

    def user_in_group(self, user, group):
        if user not in self.users:
            return False
        if group == cli.DOCKER_GROUP:
            return user in self.docker_members
        return False

    def user_name(self, uid):
        return cli.USER if str(uid) == "42" else ""

    def keychain_store(self, service, account, secret):
        self.trace.append(("keychain_store", service, account, secret[-4:]))
        self.keychain = True
        self.keychain_secret = secret

    def run(self, argv):
        argv = list(argv)
        self.trace.append(("run", argv))
        if self.fail_prefix and tuple(argv[:len(self.fail_prefix)]) == tuple(self.fail_prefix):
            raise OSError("SECRET-STEP-CANARY")
        result = self._dispatch(argv)
        if result is None:
            self.unhandled.append(argv)
            return cli.RunResult(127, b"", b"")
        return result

    def _ok(self, out=b""):
        if isinstance(out, str):
            out = out.encode()
        return cli.RunResult(0, out, b"")

    def _clear_listeners(self):
        self.listeners = {port: owner for port, owner in self.listeners.items() if port in self.stuck_ports}

    def _dispatch(self, argv):
        if argv[:4] == ["docker", "compose", "-p", cli.PROJECT] and "down" in argv:
            if "--volumes" not in argv or "--rmi" not in argv or "all" not in argv:
                return None
            self.containers.clear()
            return self._ok()
        if argv == ["docker", "ps", "-aq", "--filter", LABEL]:
            return self._ok("\n".join(self.containers))
        if argv[:3] == ["docker", "rm", "-f"]:
            self.containers = [item for item in self.containers if item not in argv[3:]]
            return self._ok()
        if argv == ["docker", "images", "-q", "--filter", LABEL]:
            return self._ok("\n".join(self.images))
        if argv[:3] == ["docker", "rmi", "-f"]:
            self.images = [item for item in self.images if item not in argv[3:]]
            return self._ok()
        if argv == ["docker", "volume", "ls", "-q", "--filter", LABEL]:
            return self._ok("\n".join(self.volumes))
        if argv[:4] == ["docker", "volume", "rm", "-f"]:
            self.volumes = [item for item in self.volumes if item not in argv[4:]]
            return self._ok()
        if argv == ["gpasswd", "-d", cli.USER, cli.DOCKER_GROUP]:
            self.docker_members.discard(cli.USER)
            return self._ok()
        if argv == ["userdel", cli.USER]:
            self.users.discard(cli.USER)
            self.docker_members.discard(cli.USER)
            return self._ok()
        if argv == ["groupdel", cli.GROUP]:
            self.groups.discard(cli.GROUP)
            return self._ok()
        if argv == ["dscl", ".", "-delete", "/Groups/" + cli.DOCKER_GROUP, "GroupMembership", cli.USER]:
            self.docker_members.discard(cli.USER)
            return self._ok()
        if argv == ["dscl", ".", "-delete", "/Users/" + cli.USER]:
            self.users.discard(cli.USER)
            self.docker_members.discard(cli.USER)
            return self._ok()
        if argv == ["dscl", ".", "-delete", "/Groups/" + cli.GROUP]:
            self.groups.discard(cli.GROUP)
            return self._ok()
        if argv == ["security", "delete-generic-password", "-s", "grok-relay"]:
            present = self.keychain
            self.keychain = False
            return cli.RunResult(0 if present else 44, b"", b"")
        if argv == ["security", "find-generic-password", "-s", "grok-relay"]:
            if self.keychain:
                return cli.RunResult(0, KEYCHAIN_CANARY, b"")
            return cli.RunResult(44, b"", b"")
        if argv[:3] == ["systemctl", "disable", "--now"]:
            self._clear_listeners()
            return self._ok()
        if argv == ["systemctl", "daemon-reload"]:
            return self._ok()
        if argv[:3] == ["launchctl", "bootout", "system"]:
            self._clear_listeners()
            return self._ok()
        if argv[:3] == ["launchctl", "unload", "-w"]:
            return self._ok()
        if argv == ["nft", "list", "table", "inet", "grok_relay"]:
            text = b"table inet grok_relay\n" if self.nft else b""
            return cli.RunResult(0 if self.nft else 1, text, b"")
        if argv == ["nft", "delete", "table", "inet", "grok_relay"]:
            self.nft = False
            return self._ok()
        if argv == ["ufw", "status", "numbered"]:
            lines = ["Status: active", ""]
            for number, body in self.ufw:
                lines.append("[{:>2}] {}".format(number, body))
            return self._ok("\n".join(lines) + "\n")
        if argv[:3] == ["ufw", "--force", "delete"] and len(argv) == 4:
            number = int(argv[3])
            self.ufw = [row for row in self.ufw if row[0] != number]
            return self._ok()
        if argv == ["pfctl", "-a", "grok-relay", "-F", "all"]:
            self.pf_loaded = False
            return self._ok()
        if argv == ["pfctl", "-a", "grok-relay", "-sr"]:
            if self.pf_loaded:
                return self._ok(b"pass in proto tcp from any to any port 443\n")
            return self._ok(b"")
        if argv and argv[0] == "lsof":
            port = None
            for part in argv:
                if part.startswith("-iTCP:"):
                    port = int(part.split(":", 1)[1])
            owner = self.listeners.get(port)
            if not owner:
                return cli.RunResult(1, b"", b"")
            text = "p42\nc" + owner + "\nu" + owner + "\nn192.0.2.10:" + str(port) + "\n"
            return self._ok(text)
        if argv[:4] == ["ss", "-H", "-ltnp", "src"] and len(argv) == 5:
            port = int(argv[4][1:])
            owner = self.listeners.get(port)
            if not owner:
                return self._ok(b"")
            line = (
                "LISTEN 0 128 192.0.2.10:" + str(port) + " 0.0.0.0:* users:((\""
                + owner + "\",pid=42,fd=3)) " + SOCKET_CANARY + "\n"
            )
            return self._ok(line)
        return None


class FakeStation:
    def __init__(self, trace, reachable=True):
        self.trace = trace
        self._reachable = reachable

    def notify(self, line):
        self.trace.append(("notify", line))
        if not self._reachable:
            raise cli.StationError("unreachable")

    def revoke(self):
        self.trace.append(("revoke",))
        if not self._reachable:
            raise cli.StationError("unreachable")

    def reachable(self):
        self.trace.append(("reachable",))
        return self._reachable


class RelayCli(unittest.TestCase):
    def setUp(self):
        self.trace = []
        self.fake = FakeSystem(self.trace)
        self.station = FakeStation(self.trace)

    def _run(self, argv, fake=None, station=None, **kwargs):
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = cli.main(argv, system=fake or self.fake, station=station or self.station, **kwargs)
        return code, buf.getvalue(), err.getvalue()

    def _uninstall(self, word="uninstall", fake=None, station=None):
        return self._run(["uninstall"], fake=fake, station=station, input_fn=lambda _prompt: word)

    def _runs(self, trace=None):
        return [event[1] for event in (trace if trace is not None else self.trace) if event[0] == "run"]

    def _index(self, pred, trace=None):
        for index, event in enumerate(trace if trace is not None else self.trace):
            if pred(event):
                return index
        self.fail("event not found")

    def test_audit_start_is_before_unpair(self):
        self.assertEqual(cli.cert_serial8(CERT_PEM), SERIAL8)
        code, out, _err = self._uninstall()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.unhandled, [], self.fake.unhandled)
        start = self._index(lambda event: event[0] == "audit" and "relay-uninstall-start" in event[1])
        notify = self._index(lambda event: event[0] == "notify" and "relay-uninstall-start" in event[1])
        revoke = self._index(lambda event: event[0] == "revoke")
        compose = self._index(lambda event: event[0] == "run" and event[1][:4] == ["docker", "compose", "-p", "grok-relay"])
        self.assertLess(start, notify)
        self.assertLess(notify, revoke)
        self.assertLess(revoke, compose)
        self.assertEqual(self.trace[start][1], START_LINE)
        self.assertEqual(self.trace[notify][1], START_LINE)
        self.assertNotIn("abcdef12", self.trace[start][1])
        done = [event for event in self.trace if event[0] == "notify" and "relay-uninstall-done" in event[1]]
        self.assertEqual(done, [("notify", DONE_LINE)])
        self.assertNotIn(DONE_LINE, self.fake.audits)
        self.assertIn(
            ["docker", "compose", "-p", "grok-relay", "down", "--volumes", "--rmi", "all"],
            self._runs(),
        )
        self.assertLess(
            self._index(lambda event: event[0] == "run" and event[1][:2] == ["gpasswd", "-d"]),
            self._index(lambda event: event[0] == "run" and event[1][:1] == ["userdel"]),
        )
        self.assertLess(
            self._index(lambda event: event[0] == "run" and event[1][:1] == ["userdel"]),
            self._index(lambda event: event[0] == "run" and event[1][:1] == ["groupdel"]),
        )
        removed = [event[1] for event in self.trace if event[0] == "remove_file"]
        self.assertEqual(removed[-1], "/usr/local/bin/grok-relay")
        self.assertIn("/etc/systemd/system/grok-relay.service", removed)
        self.assertLess(removed.index("/etc/systemd/system/grok-relay.service"), removed.index("/usr/local/bin/grok-relay"))
        for label in cli.CHECKLIST:
            self.assertIn(label + ": PASS", out)
        self.assertNotIn("FAIL", out)
        self.assertNotIn("BEGIN CERTIFICATE", out)

    def test_wrong_confirmation_removes_nothing(self):
        for word in ("no", "UNINSTALL", "yes", ""):
            with self.subTest(word=word):
                trace = []
                fake = FakeSystem(trace)
                station = FakeStation(trace)
                code, out, _err = self._uninstall(word, fake=fake, station=station)
                self.assertEqual(code, 1)
                self.assertEqual(trace, [])
                self.assertIn("aborted: confirmation did not match", out)
                self.assertIn("/etc/grok-relay", out)
                self.assertIn(KEY_CANARY, fake.files[fake.paths.key])

    def test_yes_flag_is_rejected(self):
        def boom(_prompt):
            raise AssertionError("prompted")

        for argv in (["uninstall", "--yes"], ["--yes", "uninstall"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as caught:
                    with contextlib.redirect_stderr(io.StringIO()):
                        cli.main(argv, system=self.fake, station=self.station, input_fn=boom)
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.trace, [])

    def test_station_unreachable_prints_the_manual_message(self):
        station = FakeStation(self.trace, reachable=False)
        code, out, _err = self._uninstall(station=station)
        self.assertEqual(code, 0, out)
        self.assertIn("Station not notified: remove relay from admin page", out)
        self.assertFalse(any(event[0] == "notify" and "relay-uninstall-done" in event[1] for event in self.trace))
        self.assertTrue(any(event[0] == "revoke" for event in self.trace))
        self.assertLess(
            self._index(lambda event: event[0] == "audit" and "relay-uninstall-start" in event[1]),
            self._index(lambda event: event[0] == "revoke"),
        )
        self.assertIn(["docker", "compose", "-p", "grok-relay", "down", "--volumes", "--rmi", "all"], self._runs())
        self.assertTrue(any("relay-uninstall-fail unpair" in line for line in self.fake.audits))
        for label in cli.CHECKLIST:
            self.assertIn(label + ": PASS", out)

    def test_checklist_fail_exits_1(self):
        self.fake.boom_paths.add("/var/cache/grok-relay")
        code, out, _err = self._uninstall()
        self.assertEqual(code, 1)
        self.assertIn("files: FAIL", out)
        for label in cli.CHECKLIST:
            if label != "files":
                self.assertIn(label + ": PASS", out)
        hand = out.split("Remove by hand:", 1)[1]
        self.assertIn("/var/cache/grok-relay", hand)
        self.assertNotIn(XAI_CANARY.decode(), out)
        self.assertNotIn("PRIVATE-KEY-CANARY", out)

    def test_checklist_pass_exits_0(self):
        code, out, _err = self._uninstall()
        self.assertEqual(code, 0, out)
        self.assertNotIn("FAIL", out)
        self.assertNotIn("Remove by hand:", out)
        for label in cli.CHECKLIST:
            self.assertIn(label + ": PASS", out)

    def test_uninstall_does_not_print_secret_values(self):
        code, out, _err = self._uninstall()
        self.assertEqual(code, 0, out)
        blob = out + "\n" + "\n".join(self.fake.audits)
        for secret in (
            "PRIVATE-KEY-CANARY",
            "xai-live-secret-9999",
            "KEYCHAIN-SECRET-VALUE",
            "BEGIN CERTIFICATE",
            "example.invalid",
            "192.0.2.10",
            SOCKET_CANARY,
        ):
            self.assertNotIn(secret, blob)
        self.assertNotIn(self.fake.paths.key, self.fake.reads)
        self.assertNotIn(self.fake.paths.xai_key, self.fake.reads)
        joined = " ".join(" ".join(argv) for argv in self._runs())
        self.assertNotIn("xai-live-secret-9999", joined)
        self.assertNotIn("PRIVATE-KEY-CANARY", joined)

    def test_port_report_hides_the_socket_line(self):
        self.fake.stuck_ports.add(443)
        code, out, _err = self._uninstall()
        self.assertEqual(code, 1)
        self.assertIn("listening ports 443/8443: FAIL", out)
        self.assertIn("port 443", out.split("Remove by hand:", 1)[1])
        self.assertNotIn("192.0.2.10", out)
        self.assertNotIn(SOCKET_CANARY, out)
        self.assertNotIn("PRIVATE-KEY-CANARY", out)

    def test_step_exception_is_audited_and_uninstall_continues(self):
        self.fake.fail_prefix = ("docker", "compose")
        code, out, _err = self._uninstall()
        self.assertEqual(code, 0, out)
        self.assertIn("step docker failed", out)
        self.assertNotIn("SECRET-STEP-CANARY", out)
        self.assertNotIn("SECRET-STEP-CANARY", "\n".join(self.fake.audits))
        self.assertTrue(any(line.endswith("relay-uninstall-fail docker\n") for line in self.fake.audits))
        self.assertTrue(any(argv[:1] == ["userdel"] for argv in self._runs()))
        self.assertIn("files: PASS", out)

    def test_darwin_uses_dscl_and_keychain(self):
        trace = []
        fake = FakeSystem(trace, platform="darwin")
        station = FakeStation(trace)
        code, out, _err = self._uninstall(fake=fake, station=station)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.unhandled, [], fake.unhandled)
        runs = [event[1] for event in trace if event[0] == "run"]
        self.assertIn(["dscl", ".", "-delete", "/Groups/docker", "GroupMembership", "grok-relay"], runs)
        self.assertIn(["dscl", ".", "-delete", "/Users/grok-relay"], runs)
        self.assertIn(["dscl", ".", "-delete", "/Groups/grok-relay"], runs)
        self.assertIn(["security", "delete-generic-password", "-s", "grok-relay"], runs)
        self.assertFalse(any(argv[:1] == ["userdel"] for argv in runs))
        self.assertFalse(any(argv[:1] == ["gpasswd"] for argv in runs))
        removed = [event[1] for event in trace if event[0] == "remove_file"]
        self.assertEqual(removed[-1], "/usr/local/bin/grok-relay")
        self.assertIn("/Library/Application Support/grok-relay", out)
        self.assertNotIn("KEYCHAIN-SECRET-VALUE", out)
        self.assertNotIn("PRIVATE-KEY-CANARY", out)
        for label in cli.CHECKLIST:
            self.assertIn(label + ": PASS", out)

    def test_keychain_listing_is_not_printed(self):
        trace = []
        fake = FakeSystem(trace, platform="darwin")
        fake.fail_prefix = ("security", "delete-generic-password")
        station = FakeStation(trace)
        code, out, _err = self._uninstall(fake=fake, station=station)
        self.assertEqual(code, 1)
        self.assertIn("credential store: FAIL", out)
        self.assertIn("keychain item grok-relay", out)
        self.assertNotIn("KEYCHAIN-SECRET-VALUE", out)
        self.assertNotIn("xai-live-secret-9999", out)
        self.assertNotIn("KEYCHAIN-SECRET-VALUE", "\n".join(fake.audits))

    def test_not_root_removes_nothing(self):
        self.fake.root = False
        code, out, _err = self._uninstall()
        self.assertEqual(code, 1)
        self.assertIn("Run as root.", out)
        self.assertEqual(self.trace, [])
        self.assertNotIn("/etc/grok-relay", out)

    def test_rotate_stores_mode_600_and_last4(self):
        before = dict(os.environ)
        code, out, _err = self._run(["rotate-xai-key"], getpass_fn=lambda _prompt: ROTATE_KEY)
        self.assertEqual(code, 0, out)
        path = self.fake.paths.xai_key
        self.assertEqual(self.fake.modes[path], 0o600)
        self.assertEqual(self.fake.owners[path], "grok-relay")
        self.assertEqual(self.fake.files[path], ROTATE_KEY.encode() + b"\n")
        self.assertIn("Last 4: wxyz", out)
        self.assertNotIn(ROTATE_KEY, out)
        self.assertNotIn(ROTATE_KEY[:-4], out)
        self.assertIn("2026-10-20T12:00:00Z relay local xai-key-rotated wxyz\n", self.fake.audits)
        self.assertNotIn(ROTATE_KEY, "\n".join(self.fake.audits))
        self.assertFalse(any(event[0] == "run" for event in self.trace))
        self.assertEqual(os.environ, before)

    def test_rotate_on_darwin_does_not_put_the_key_on_a_command_line(self):
        trace = []
        fake = FakeSystem(trace, platform="darwin")
        code, out, _err = self._run(
            ["rotate-xai-key"], fake=fake, getpass_fn=lambda _prompt: ROTATE_KEY,
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.modes[fake.paths.xai_key], 0o600)
        self.assertEqual(fake.keychain_secret, ROTATE_KEY)
        self.assertIn(("keychain_store", "grok-relay", "grok-relay", "wxyz"), trace)
        for event in trace:
            if event[0] == "run":
                self.assertNotIn(ROTATE_KEY, " ".join(event[1]))
            blob = " ".join(str(part) for part in event)
            self.assertNotIn(ROTATE_KEY[:-4], blob)
        self.assertNotIn(ROTATE_KEY, out)

    def test_rotate_rejects_a_short_key_without_echoing_it(self):
        code, out, _err = self._run(["rotate-xai-key"], getpass_fn=lambda _prompt: "short")
        self.assertEqual(code, 1)
        self.assertIn("Key was not stored.", out)
        self.assertNotIn("short", out)
        self.assertFalse(any(event[0] == "write_secret" for event in self.trace))

    def test_status_prints_version_pairing_and_expiry(self):
        code, out, _err = self._run(["status"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "version: 1",
            "paired: yes",
            "cert expiry: 2028-06-01T00:00:00Z",
        ])
        self.assertNotIn(self.fake.paths.key, self.fake.reads)
        self.assertNotIn(self.fake.paths.xai_key, self.fake.reads)
        self.assertNotIn("BEGIN CERTIFICATE", out)
        self.assertNotIn("PRIVATE-KEY-CANARY", out)

    def test_parsers_do_not_return_addresses(self):
        text = "[ 4] 443/tcp ALLOW IN 192.0.2.10 # grok-relay\n[ 5] 22/tcp ALLOW IN Anywhere\n"
        self.assertEqual(cli.ufw_grok_numbers(text), [4])
        owned = "p9\ncnginx\nuwww\nn192.0.2.10:443\np10\ncgrok-relay\nuroot\n"
        self.assertTrue(cli.parse_lsof_owned(owned))
        self.assertFalse(cli.parse_lsof_owned("p9\ncnginx\nuwww\nn192.0.2.10:443\n"))
        ss = 'LISTEN 0 128 192.0.2.10:443 0.0.0.0:* users:(("python3",pid=42,fd=3))\n'
        self.assertTrue(cli.parse_ss_owned(ss, lambda _pid: ("grok-relay", "python3")))
        self.assertIs(cli.parse_ss_owned(ss, lambda _pid: ("www", "nginx")), False)

    def test_real_run_is_an_argument_list(self):
        with mock.patch("grok_relay_cli.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
            result = cli.RealSystem().run(["true", "--flag"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(run.call_args.args[0], ["true", "--flag"])
        self.assertIs(run.call_args.kwargs.get("shell"), False)
        with self.assertRaises(TypeError):
            cli.RealSystem().run("true --flag")
        with mock.patch("grok_relay_cli.subprocess.run", side_effect=FileNotFoundError):
            missing = cli.RealSystem().run(["missing-program"])
        self.assertEqual(missing.returncode, 127)

    def test_real_write_secret_is_mode_600(self):
        system = cli.RealSystem()
        system.lookup_owner = lambda _owner: (os.getuid(), os.getgid())
        calls = []
        system.run = lambda argv: calls.append(argv) or cli.RunResult(0, b"", b"")
        data = ROTATE_KEY.encode() + b"\n"
        old = os.umask(0)
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "xai_api_key")
                fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o644)
                os.close(fd)
                system.write_secret(path, data, 0o600, "grok-relay")
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                with open(path, "rb") as handle:
                    self.assertEqual(handle.read(), data)
        finally:
            os.umask(old)
        self.assertEqual(calls, [])

    def test_station_fetcher_hides_the_host(self):
        fake = FakeSystem([])
        url = "https://station.example.invalid"
        fake.files[fake.paths.station_url] = (url + "\n").encode()
        seen = []

        def fetch(target, body, _ctx):
            seen.append((target, body))
            if target.endswith("/revoke"):
                raise TimeoutError(url + " timed out")
            if target.endswith("/health"):
                return 503
            return 200

        station = cli.RealStation(
            fake, fake.paths, fetch=fetch, ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        )
        station.prepare()
        fake.files.pop(fake.paths.station_url)
        with self.assertRaises(cli.StationError) as caught:
            station.revoke()
        self.assertEqual(str(caught.exception), "unreachable")
        self.assertNotIn("example.invalid", str(caught.exception))
        self.assertEqual(seen[0][0], url + "/admin/relay/revoke")
        line = START_LINE
        station.notify(line)
        self.assertEqual(seen[1], (url + "/admin/relay/audit", line.encode("ascii")))
        self.assertFalse(station.reachable())
        bad = cli.RealStation(fake, fake.paths, fetch=fetch, ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
        fake.files[fake.paths.station_url] = b"http://station.example.invalid\n"
        with self.assertRaises(cli.StationError) as caught:
            bad.revoke()
        self.assertNotIn("example.invalid", str(caught.exception))

    def test_wrapper_execs_python(self):
        with open(os.path.join(ROOT, "relay", "grok-relay"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn('exec python3 "$(dirname "$0")/grok_relay_cli.py" "$@"', text)
        self.assertNotIn("shell=True", text)

    def test_windows_script_checks_elevation_and_has_no_policy_bypass(self):
        with open(os.path.join(ROOT, "relay", "grok-relay.ps1"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("ExecutionPolicy", text)
        self.assertNotIn("Bypass", text)
        self.assertIn("Administrator", text)
        self.assertIn("relay-uninstall-start", text)
        self.assertIn("relay-uninstall-done", text)
        self.assertIn("Station not notified: remove relay from admin page", text)
        self.assertIn("docker compose -p $Project down --volumes --rmi all", text)
        self.assertIn("uninstall", text)
        self.assertNotIn("-Yes", text)


if __name__ == "__main__":
    unittest.main()
