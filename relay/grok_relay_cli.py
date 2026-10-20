#!/usr/bin/env python3
"""Relay host commands: status, xAI key rotation, and uninstall.

Every effect on the machine goes through a System object. The real one runs
programs as argument lists (never a shell string). Tests pass a fake.
Network calls go through an injected station client, not a live socket.

Audit lines are five fields, `<ts> <actor> <side> <event> <detail>`.
A line never carries a secret, a code, a key (last 4 only), an address, or a host name.
There is no --yes switch: uninstall continues only after the operator types uninstall.
"""
import argparse
import datetime
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from cryptography import x509

VERSION = "1"
ACTOR = "relay"
USER = "grok-relay"
GROUP = "grok-relay"
DOCKER_GROUP = "docker"
PROJECT = "grok-relay"
COMPOSE_LABEL = "com.docker.compose.project=" + PROJECT
SHIM = "/usr/local/bin/grok-relay"
CONFIRM_WORD = "uninstall"
PORTS = (443, 8443)
KEYCHAIN_SERVICE = "grok-relay"
EVENTS = frozenset({
    "relay-uninstall-start",
    "relay-uninstall-fail",
    "relay-uninstall-done",
    "xai-key-rotated",
})
SIDES = frozenset({"local", "station"})
CHECKLIST = (
    "files",
    "account",
    "services",
    "listening ports 443/8443",
    "firewall rules",
    "credential store",
    "containers/images/volumes",
    "PATH shim",
)
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_DETAIL = re.compile(r"[!-~]{1,64}")
_UFW_LINE = re.compile(r"^\[\s*(\d+)\]\s+(.*)$")
_SS_PROC = re.compile(r'\(\("([^"]*)",pid=(\d+)')
_KEY_MIN, _KEY_MAX = 8, 512

# uninstall step names written as the detail of relay-uninstall-fail
STEP_AUDIT = "audit-start"
STEP_NOTIFY = "notify"
STEP_UNPAIR = "unpair"
STEP_DOCKER = "docker"
STEP_ACCOUNT = "account"
STEP_SECRETS = "secrets"
STEP_FILES = "files"
STEP_FIREWALL = "firewall"
STEP_SERVICES = "services"
STEP_DONE = "done"


class StepError(Exception):
    """A named uninstall step failed. The message is the step name, never a secret."""


class StationError(Exception):
    """The station could not be reached. The message is a fixed token, never a URL."""


class RunResult:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = int(returncode)
        self.stdout = stdout if isinstance(stdout, bytes) else str(stdout).encode()
        self.stderr = stderr if isinstance(stderr, bytes) else str(stderr).encode()


class Paths:
    def __init__(self, config, state, log, cache, cert, key, ca, xai_key, minisign,
                 paired, station_url, shim, pf_anchor, pf_conf, launchd_dir, systemd_dirs):
        self.config = config
        self.state = state
        self.log = log
        self.cache = cache
        self.cert = cert
        self.key = key
        self.ca = ca
        self.xai_key = xai_key
        self.minisign = minisign
        self.paired = paired
        self.station_url = station_url
        self.shim = shim
        self.pf_anchor = pf_anchor
        self.pf_conf = pf_conf
        self.launchd_dir = launchd_dir
        self.systemd_dirs = tuple(systemd_dirs)

    def dirs(self):
        """Config, state, log, and cache. Deepest first so a nested state dir goes before its parent."""
        return tuple(sorted({self.cache, self.log, self.state, self.config}, key=len, reverse=True))


def paths_for(platform):
    if platform == "darwin":
        base = "/Library/Application Support/grok-relay"
        return Paths(
            config=base,
            state=base + "/state",
            log="/Library/Logs/grok-relay",
            cache="/Library/Caches/grok-relay",
            cert=base + "/client.crt",
            key=base + "/client.key",
            ca=base + "/ca.crt",
            xai_key=base + "/xai_api_key",
            minisign=base + "/minisign",
            paired=base + "/state/paired",
            station_url=base + "/station.url",
            shim=SHIM,
            pf_anchor="/etc/pf.anchors/grok-relay",
            pf_conf="/etc/pf.conf",
            launchd_dir="/Library/LaunchDaemons",
            systemd_dirs=(),
        )
    return Paths(
        config="/etc/grok-relay",
        state="/var/lib/grok-relay",
        log="/var/log/grok-relay",
        cache="/var/cache/grok-relay",
        cert="/etc/grok-relay/client.crt",
        key="/etc/grok-relay/client.key",
        ca="/etc/grok-relay/ca.crt",
        xai_key="/etc/grok-relay/xai_api_key",
        minisign="/etc/grok-relay/minisign",
        paired="/var/lib/grok-relay/paired",
        station_url="/etc/grok-relay/station.url",
        shim=SHIM,
        pf_anchor="",
        pf_conf="",
        launchd_dir="",
        systemd_dirs=(
            "/etc/systemd/system",
            "/usr/lib/systemd/system",
            "/lib/systemd/system",
        ),
    )


def format_audit(ts, event, detail, side="local"):
    """One audit line. Rejects anything that is not the five-field shape."""
    if not _TS.fullmatch(ts) or event not in EVENTS or side not in SIDES:
        raise ValueError("audit field")
    if detail != "-" and not _DETAIL.fullmatch(detail):
        raise ValueError("audit detail")
    return f"{ts} {ACTOR} {side} {event} {detail}\n"


def cert_serial8(pem):
    """Last 8 hex digits of the client certificate serial, or 00000000 when there is none."""
    if not pem:
        return "00000000"
    try:
        cert = x509.load_pem_x509_certificate(pem)
    except Exception:
        return "00000000"
    return format(cert.serial_number, "x")[-8:].rjust(8, "0")


def cert_expiry(pem):
    """UTC expiry `YYYY-MM-DDTHH:MM:SSZ`, `none` when the file is missing, `unknown` when it is unreadable."""
    if not pem:
        return "none"
    try:
        cert = x509.load_pem_x509_certificate(pem)
        when = cert.not_valid_after_utc
    except Exception:
        return "unknown"
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def key_ok(key):
    """A key long enough that the last 4 are not the whole value, printable, and free of whitespace."""
    if not isinstance(key, str) or not _KEY_MIN <= len(key) <= _KEY_MAX:
        return False
    return all(33 <= ord(c) <= 126 for c in key)


def ufw_grok_numbers(text):
    """Rule numbers whose ufw comment names grok-relay. Highest should be deleted first."""
    found = []
    for line in text.splitlines():
        match = _UFW_LINE.match(line.strip())
        if not match or "#" not in match.group(2):
            continue
        comment = match.group(2).split("#", 1)[1]
        if re.search(r"(^|\s)grok-relay($|\s)", comment.strip()):
            found.append(int(match.group(1)))
    return found


def parse_lsof_owned(text):
    """True when an lsof -Fpcu record is the relay user or a grok-relay command. Ignores address fields."""
    user = cmd = ""
    owned = False
    for raw in text.splitlines():
        if not raw:
            continue
        kind, val = raw[0], raw[1:]
        if kind == "p":
            user = cmd = ""
        elif kind == "c":
            cmd = val
        elif kind == "u":
            user = val
        else:
            continue
        if user == USER or (cmd and "grok-relay" in cmd):
            owned = True
    return owned


def parse_ss_owned(text, identify):
    """True when an ss listener is the relay. `identify(pid)` returns `(user, name)`.

    The socket line itself is never returned. Callers must not print `text`: it contains addresses.
    """
    owned = False
    for name, pid in _SS_PROC.findall(text):
        if "grok-relay" in name:
            owned = True
            continue
        try:
            user, proc = identify(pid)
        except Exception:
            user = proc = ""
        if user == USER or (proc and "grok-relay" in proc):
            owned = True
    return owned


def _decode(data):
    return data.decode("utf-8", "replace")


class RealSystem:
    """OS effects. External programs are argument lists; shell=True is never used.

    The xAI key is not one of those arguments. `security -w` would put it in the process list,
    so the keychain write goes through the Security framework instead.
    """

    def platform(self):
        return sys.platform

    def is_root(self):
        geteuid = getattr(os, "geteuid", None)
        return bool(geteuid and geteuid() == 0)

    def now(self):
        return datetime.datetime.now(datetime.timezone.utc)

    def run(self, argv):
        if isinstance(argv, str) or not isinstance(argv, (list, tuple)):
            raise TypeError("command must be a list of strings")
        argv = list(argv)
        if not argv or not all(isinstance(part, str) for part in argv):
            raise TypeError("command must be a list of strings")
        try:
            proc = subprocess.run(argv, capture_output=True, shell=False, check=False)
        except FileNotFoundError:
            return RunResult(127, b"", b"")
        return RunResult(proc.returncode, proc.stdout or b"", proc.stderr or b"")

    def exists(self, path):
        return os.path.lexists(path)

    def listdir(self, path):
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    def read_bytes(self, path):
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def remove_file(self, path):
        try:
            os.remove(path)
        except FileNotFoundError:
            return

    def remove_tree(self, path):
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
            return
        if not os.path.exists(path):
            return
        shutil.rmtree(path)

    def write_file(self, path, data):
        """Replace a non-secret file (pf.conf). Not for keys."""
        mode = 0o644
        if os.path.exists(path):
            mode = stat.S_IMODE(os.stat(path).st_mode)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    def write_secret(self, path, data, mode, owner):
        """Write bytes at `mode` owned by `owner`. `data` is never put in an error or a command."""
        if not isinstance(data, bytes):
            raise TypeError("secret must be bytes")
        if mode & 0o077:
            raise ValueError("secret mode")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        tmp = path + ".tmp"
        fd = os.open(tmp, flags, mode)
        try:
            view = memoryview(data)
            while view:
                wrote = os.write(fd, view)
                if wrote <= 0:
                    raise StepError("secret")
                view = view[wrote:]
            os.fchmod(fd, mode)  # an existing mode and the umask must not widen this file
            uid, gid = self.lookup_owner(owner)
            os.fchown(fd, uid, gid)
            os.fsync(fd)
        except StepError:
            os.close(fd)
            _unlink_quiet(tmp)
            raise
        except Exception:
            os.close(fd)
            _unlink_quiet(tmp)
            raise StepError("secret") from None
        os.close(fd)
        try:
            os.replace(tmp, path)
            os.chmod(path, mode)
            os.chown(path, uid, gid)
        except Exception:
            _unlink_quiet(tmp)
            raise StepError("secret") from None

    def lookup_owner(self, owner):
        import grp
        import pwd
        pw = pwd.getpwnam(owner)
        try:
            gid = grp.getgrnam(owner).gr_gid
        except KeyError:
            gid = pw.pw_gid
        return pw.pw_uid, gid

    def user_name(self, uid):
        import pwd
        try:
            return pwd.getpwuid(int(uid)).pw_name
        except (KeyError, TypeError, ValueError):
            return ""

    def append_audit(self, path, line):
        """Append one audit line. The directory must already exist, except the caller may create it first.

        Mode 600 is set only when this call creates the file. The line is not logged anywhere else.
        """
        fields = line.rstrip("\n").split(" ")
        if len(fields) != 5 or "\n" in line[:-1]:
            raise ValueError("audit line")
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, mode=0o755, exist_ok=True)
        data = line if line.endswith("\n") else line + "\n"
        encoded = data.encode("ascii")
        try:
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(path, os.O_APPEND | os.O_WRONLY)
            created = False
        try:
            if created:
                os.fchmod(fd, 0o600)
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def user_exists(self, name):
        if self.platform() == "darwin":
            return self.run(["dscl", ".", "-read", "/Users/" + name]).returncode == 0
        import pwd
        try:
            pwd.getpwnam(name)
            return True
        except KeyError:
            return False

    def group_exists(self, name):
        if self.platform() == "darwin":
            return self.run(["dscl", ".", "-read", "/Groups/" + name]).returncode == 0
        import grp
        try:
            grp.getgrnam(name)
            return True
        except KeyError:
            return False

    def user_in_group(self, user, group):
        if not self.user_exists(user):
            return False
        if self.platform() == "darwin":
            rc = self.run(["dscl", ".", "-read", "/Groups/" + group, "GroupMembership"])
            if rc.returncode != 0:
                return False
            members = _decode(rc.stdout).replace("GroupMembership:", " ").split()
            return user in members
        import grp
        import pwd
        try:
            ent = grp.getgrnam(group)
        except KeyError:
            return False
        if user in ent.gr_mem:
            return True
        try:
            return pwd.getpwnam(user).pw_gid == ent.gr_gid
        except KeyError:
            return False

    def keychain_store(self, service, account, secret):
        if self.platform() != "darwin":
            raise StepError("keychain")
        if not isinstance(secret, str):
            raise StepError("keychain")
        _keychain_upsert(service.encode("utf-8"), account.encode("utf-8"), secret.encode("utf-8"))


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _keychain_upsert(service, account, secret):
    """Write a keychain generic password without placing the secret on a command line."""
    import ctypes
    try:
        lib = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    except OSError:
        raise StepError("keychain") from None
    lib.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    lib.SecKeychainAddGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.c_void_p,
    ]
    lib.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    lib.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.SecKeychainItemModifyContent.restype = ctypes.c_int32
    lib.SecKeychainItemModifyContent.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
    ]
    lib.CFRelease.restype = None
    lib.CFRelease.argtypes = [ctypes.c_void_p]
    not_found = -25300
    item = ctypes.c_void_p()
    status = lib.SecKeychainFindGenericPassword(
        None, len(service), service, len(account), account, None, None, ctypes.byref(item),
    )
    if status == 0 and item.value:
        changed = lib.SecKeychainItemModifyContent(item, None, len(secret), secret)
        lib.CFRelease(item)
        if changed != 0:
            raise StepError("keychain")
        return
    if status not in (0, not_found):
        raise StepError("keychain")
    added = lib.SecKeychainAddGenericPassword(
        None, len(service), service, len(account), account, len(secret), secret, None,
    )
    if added != 0:
        raise StepError("keychain")


def _urllib_fetch(url, body, context):
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, context=context, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


class RealStation:
    """mTLS client. Pass `fetch` in tests so nothing here opens a socket.

    `prepare` reads the URL and client certificate into memory. Uninstall deletes those
    files before the final audit, and the done line still has to be able to reach the station.
    """

    def __init__(self, system, paths, fetch=None, ssl_context=None):
        self.system = system
        self.paths = paths
        self.fetch = fetch or _urllib_fetch
        self._ssl_context = ssl_context
        self._cached_base = None
        self._cached_ssl = None

    def prepare(self):
        self._cached_base = self._read_base()
        self._cached_ssl = self._load_ssl()

    def revoke(self):
        self._post("/admin/relay/revoke", b"")

    def notify(self, line):
        if not isinstance(line, str):
            raise StationError("unreachable")
        self._post("/admin/relay/audit", line.encode("ascii"))

    def reachable(self):
        try:
            self._post("/admin/relay/health", b"")
        except StationError:
            return False
        return True

    def _read_base(self):
        raw = self.system.read_bytes(self.paths.station_url)
        if not raw:
            raise StationError("unreachable")
        try:
            url = raw.decode("ascii").strip()
        except UnicodeError:
            raise StationError("unreachable") from None
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise StationError("unreachable")
        return url.rstrip("/")

    def _load_ssl(self):
        if self._ssl_context is not None:
            return self._ssl_context
        import ssl
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(self.paths.ca)
        context.load_cert_chain(self.paths.cert, self.paths.key)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    def _post(self, path, body):
        try:
            base = self._cached_base if self._cached_base is not None else self._read_base()
            context = self._cached_ssl if self._cached_ssl is not None else self._load_ssl()
            status = self.fetch(base + path, body, context)
        except StationError:
            raise
        except Exception:
            raise StationError("unreachable") from None
        if status != 200:
            raise StationError("unreachable")


class RelayCLI:
    def __init__(self, system, station=None):
        self.system = system
        self.paths = paths_for(system.platform())
        self.station = station

    def status(self):
        try:
            pem = self.system.read_bytes(self.paths.cert)
            expiry = cert_expiry(pem or b"")
        except Exception:
            expiry = "unknown"
        print("version: " + VERSION)
        print("paired: " + self._paired())
        print("cert expiry: " + expiry)
        return 0

    def rotate(self, getpass_fn):
        if not self.system.is_root():
            print("Run as root.")
            return 1
        try:
            key = getpass_fn("xAI key: ")
        except (EOFError, KeyboardInterrupt):
            print("Key was not stored.")
            return 1
        if not key_ok(key):
            print("Key was not stored.")
            return 1
        last4 = key[-4:]
        try:
            # The key is a file body, never an argument, an environment value, or a log line.
            self.system.write_secret(self.paths.xai_key, (key + "\n").encode("utf-8"), 0o600, USER)
            if self.system.platform() == "darwin":
                self.system.keychain_store(KEYCHAIN_SERVICE, USER, key)
        except Exception:
            print("Key was not stored.")
            return 1
        try:
            self._write_local("xai-key-rotated", last4)
        except Exception:
            print("xAI key stored. Last 4: " + last4)
            print("step audit failed")
            return 1
        print("xAI key stored. Last 4: " + last4)
        return 0

    def uninstall(self, input_fn):
        if self.system.platform() not in ("linux", "darwin"):
            print("On Windows, run grok-relay.ps1 from an elevated PowerShell.")
            return 1
        if not self.system.is_root():
            print("Run as root.")
            return 1
        self._print_plan()
        if not self._confirmed(input_fn):
            print("aborted: confirmation did not match")
            return 1
        if self.station is None:
            self.station = RealStation(self.system, self.paths)
        # Load the client certificate before it is deleted. The done line is sent after removal.
        prepare = getattr(self.station, "prepare", None)
        if prepare is not None:
            try:
                prepare()
            except Exception:
                pass
        # Audit first, then unpair. A later failure still falls through to the checklist.
        self._audit_start()
        self._guard(STEP_UNPAIR, self._step_unpair)
        self._guard(STEP_DOCKER, self._step_docker)
        self._guard(STEP_ACCOUNT, self._step_account)
        self._guard(STEP_SECRETS, self._step_secrets)
        self._guard(STEP_FILES, self._step_files)
        self._guard(STEP_FIREWALL, self._step_firewall)
        self._guard(STEP_SERVICES, self._step_services)
        self._audit_done()
        return self._checklist()

    def _confirmed(self, input_fn):
        try:
            typed = input_fn("Type uninstall to continue: ")
        except (EOFError, KeyboardInterrupt):
            return False
        if typed is None:
            return False
        return str(typed).strip() == CONFIRM_WORD

    def _print_plan(self):
        paths = self.paths
        print("This will remove the relay and cannot be undone.")
        print("Accounts: user grok-relay, group grok-relay, docker group membership")
        print("Docker project grok-relay: containers, images, and volumes (--volumes --rmi all)")
        print("Credentials (values are not shown): client certificate, private key, pinned CA, xAI key")
        print("Directories: " + ", ".join((paths.config, paths.state, paths.log, paths.cache)))
        if self.system.platform() == "darwin":
            print("Firewall: pf anchor grok-relay")
            print("Services: launchd plists in /Library/LaunchDaemons matching grok-relay")
            print("Credential store: macOS Keychain service grok-relay")
        else:
            print("Firewall: nft table grok_relay, ufw rules commented grok-relay")
            print("Services: systemd units grok-relay*.service and grok-relay*.timer")
            print("Credential store: xAI key file")
        print("PATH shim: " + paths.shim)
        print("Station pairing will be revoked.")

    def _ts(self):
        now = self.system.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=datetime.timezone.utc)
        else:
            now = now.astimezone(datetime.timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _line(self, event, detail, side="local"):
        return format_audit(self._ts(), event, detail, side)

    def _audit_path(self, line):
        return os.path.join(self.paths.log, "audit-" + line[:7] + ".log")

    def _write_local(self, event, detail):
        line = self._line(event, detail, "local")
        self.system.append_audit(self._audit_path(line), line)
        return line

    def _audit_start(self):
        try:
            pem = self.system.read_bytes(self.paths.cert) or b""
        except Exception:
            pem = b""
        try:
            line = self._write_local("relay-uninstall-start", cert_serial8(pem))
        except Exception:
            self._audit_fail(STEP_AUDIT)
            return
        try:
            self.station.notify(line)
        except Exception:
            self._audit_fail(STEP_NOTIFY)

    def _audit_fail(self, step):
        if not re.fullmatch(r"[a-z0-9-]{1,32}", step):
            step = "unknown"
        try:
            # Skip when the log dir is already gone. Recreating it would leave a file the checklist flags.
            line = self._line("relay-uninstall-fail", step)
            parent = os.path.dirname(self._audit_path(line))
            if parent and not self.system.exists(parent):
                return
            self.system.append_audit(self._audit_path(line), line)
        except Exception:
            return

    def _audit_done(self):
        # The log directory is already removed, so this line exists only on the station.
        # When the station is down, write nothing.
        try:
            up = bool(self.station.reachable())
        except Exception:
            return
        if not up:
            return
        try:
            self.station.notify(self._line("relay-uninstall-done", "-", "station"))
        except Exception:
            self._audit_fail(STEP_DONE)

    def _guard(self, step, fn):
        try:
            fn()
        except Exception:
            self._audit_fail(step)
            if step == STEP_UNPAIR:
                print("Station not notified: remove relay from admin page")
            else:
                print("step " + step + " failed")

    def _step_unpair(self):
        self.station.revoke()

    def _compose_argv(self):
        argv = ["docker", "compose", "-p", PROJECT]
        compose = os.path.join(self.paths.config, "compose.yaml")
        if self.system.exists(compose):
            argv.extend(("-f", compose))
        argv.extend(("down", "--volumes", "--rmi", "all"))
        return argv

    def _step_docker(self):
        failed = False
        try:
            if self.system.run(self._compose_argv()).returncode != 0:
                failed = True
        except Exception:
            failed = True
        sweeps = (
            (["docker", "ps", "-aq", "--filter", "label=" + COMPOSE_LABEL], ["docker", "rm", "-f"]),
            (["docker", "images", "-q", "--filter", "label=" + COMPOSE_LABEL], ["docker", "rmi", "-f"]),
            (["docker", "volume", "ls", "-q", "--filter", "label=" + COMPOSE_LABEL], ["docker", "volume", "rm", "-f"]),
        )
        for list_argv, remove_argv in sweeps:
            try:
                result = self.system.run(list_argv)
                if result.returncode != 0:
                    failed = True
                    continue
                ids = [item for item in _decode(result.stdout).split() if item]
                if not ids:
                    continue
                if self.system.run(remove_argv + ids).returncode != 0:
                    failed = True
            except Exception:
                failed = True
        if failed:
            raise StepError(STEP_DOCKER)

    def _step_account(self):
        if self.system.platform() == "darwin":
            drop = ["dscl", ".", "-delete", "/Groups/" + DOCKER_GROUP, "GroupMembership", USER]
            user = ["dscl", ".", "-delete", "/Users/" + USER]
            group = ["dscl", ".", "-delete", "/Groups/" + GROUP]
        else:
            drop = ["gpasswd", "-d", USER, DOCKER_GROUP]
            user = ["userdel", USER]
            group = ["groupdel", GROUP]
        failed = False
        try:
            if self.system.user_in_group(USER, DOCKER_GROUP):
                if self.system.run(drop).returncode != 0:
                    failed = True
            if self.system.user_exists(USER):
                if self.system.run(user).returncode != 0:
                    failed = True
            if self.system.group_exists(GROUP):
                if self.system.run(group).returncode != 0:
                    failed = True
        except Exception:
            failed = True
        if failed:
            raise StepError(STEP_ACCOUNT)

    def _step_secrets(self):
        failed = False
        for path in (self.paths.cert, self.paths.key, self.paths.ca, self.paths.xai_key):
            try:
                self.system.remove_file(path)
            except Exception:
                failed = True
        if self.system.platform() == "darwin":
            try:
                # Not found (44) means the item is already gone.
                code = self.system.run(
                    ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE]
                ).returncode
                if code not in (0, 44, 127):
                    failed = True
            except Exception:
                failed = True
        if failed:
            raise StepError(STEP_SECRETS)

    def _step_files(self):
        failed = False
        for path in self.paths.dirs():
            try:
                self.system.remove_tree(path)
            except Exception:
                failed = True
        if failed:
            raise StepError(STEP_FILES)

    def _step_firewall(self):
        if self.system.platform() == "darwin":
            self._firewall_darwin()
        else:
            self._firewall_linux()

    def _firewall_linux(self):
        failed = False
        try:
            listed = self.system.run(["nft", "list", "table", "inet", "grok_relay"])
            if listed.returncode == 0:
                if self.system.run(["nft", "delete", "table", "inet", "grok_relay"]).returncode != 0:
                    failed = True
        except Exception:
            failed = True
        try:
            status = self.system.run(["ufw", "status", "numbered"])
            if status.returncode == 0:
                for number in sorted(ufw_grok_numbers(_decode(status.stdout)), reverse=True):
                    if self.system.run(["ufw", "--force", "delete", str(number)]).returncode != 0:
                        failed = True
        except Exception:
            failed = True
        if failed:
            raise StepError(STEP_FIREWALL)

    def _firewall_darwin(self):
        failed = False
        try:
            code = self.system.run(["pfctl", "-a", "grok-relay", "-F", "all"]).returncode
            if code not in (0, 1, 127):
                failed = True
        except Exception:
            failed = True
        try:
            if self.paths.pf_anchor:
                self.system.remove_file(self.paths.pf_anchor)
        except Exception:
            failed = True
        try:
            self._strip_pf_conf()
        except Exception:
            failed = True
        if failed:
            raise StepError(STEP_FIREWALL)

    def _strip_pf_conf(self):
        path = self.paths.pf_conf
        if not path:
            return
        data = self.system.read_bytes(path)
        if not data or b"grok-relay" not in data:
            return
        kept = [line for line in data.splitlines(keepends=True) if b"grok-relay" not in line]
        self.system.write_file(path, b"".join(kept))

    def _unit_paths(self):
        found = []
        for directory in self.paths.systemd_dirs:
            for name in self.system.listdir(directory):
                if name.startswith("grok-relay") and (name.endswith(".service") or name.endswith(".timer")):
                    found.append(os.path.join(directory, name))
        return found

    def _plist_paths(self):
        if not self.paths.launchd_dir:
            return []
        found = []
        for name in self.system.listdir(self.paths.launchd_dir):
            if "grok-relay" in name and name.endswith(".plist"):
                found.append(os.path.join(self.paths.launchd_dir, name))
        return found

    def _step_services(self):
        failed = False
        try:
            if self.system.platform() == "darwin":
                self._remove_launchd()
            else:
                self._remove_systemd()
        except Exception:
            failed = True
        # The shim is this command's own path. Remove it after the service units.
        try:
            self.system.remove_file(self.paths.shim)
        except Exception:
            failed = True
        if failed:
            raise StepError(STEP_SERVICES)

    def _remove_systemd(self):
        units = self._unit_paths()
        names = sorted({os.path.basename(path) for path in units})
        failed = False
        if names:
            try:
                code = self.system.run(["systemctl", "disable", "--now", *names]).returncode
                if code not in (0, 127):
                    failed = True
            except Exception:
                failed = True
        for path in units:
            self.system.remove_file(path)
        if names:
            try:
                code = self.system.run(["systemctl", "daemon-reload"]).returncode
                if code not in (0, 127):
                    failed = True
            except Exception:
                failed = True
        if failed:
            raise StepError(STEP_SERVICES)

    def _remove_launchd(self):
        for plist in sorted(self._plist_paths()):
            try:
                code = self.system.run(["launchctl", "bootout", "system", plist]).returncode
                if code not in (0, 127):
                    self.system.run(["launchctl", "unload", "-w", plist])
            except Exception:
                pass
            self.system.remove_file(plist)

    def _paired(self):
        try:
            raw = self.system.read_bytes(self.paths.paired)
        except Exception:
            return "no"
        if not raw:
            return "no"
        try:
            text = raw.decode("utf-8", "replace").strip().lower()
        except Exception:
            return "no"
        return "yes" if text == "yes" else "no"

    def _proc_identity(self, pid):
        raw = self.system.read_bytes("/proc/" + pid + "/status")
        if not raw:
            return "", ""
        user = name = ""
        for line in _decode(raw).splitlines():
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Uid:"):
                parts = line.split()
                if len(parts) > 1:
                    user = self.system.user_name(parts[1])
        return user, name

    def _port_owned(self, port):
        if self.system.platform() == "darwin":
            result = self.system.run(["lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-Fpcu"])
            if result.returncode == 1:
                return False
            if result.returncode != 0:
                return None
            return parse_lsof_owned(_decode(result.stdout))
        result = self.system.run(["ss", "-H", "-ltnp", "src", ":" + str(port)])
        if result.returncode != 0:
            return None
        return parse_ss_owned(_decode(result.stdout), self._proc_identity)

    def _port_leftovers(self):
        left = []
        for port in PORTS:
            try:
                owned = self._port_owned(port)
            except Exception:
                owned = None
            if owned is None:
                left.append("port " + str(port) + " (could not check)")
            elif owned:
                left.append("port " + str(port))
        return left

    def _file_leftovers(self):
        left = []
        for path in (
            self.paths.config, self.paths.state, self.paths.log, self.paths.cache,
            self.paths.cert, self.paths.key, self.paths.ca,
        ):
            if path and self.system.exists(path):
                left.append(path)
        return left

    def _account_leftovers(self):
        left = []
        if self.system.user_exists(USER):
            left.append("user " + USER)
        if self.system.group_exists(GROUP):
            left.append("group " + GROUP)
        if self.system.user_in_group(USER, DOCKER_GROUP):
            left.append("docker group membership")
        return left

    def _service_leftovers(self):
        if self.system.platform() == "darwin":
            return self._plist_paths()
        return self._unit_paths()

    def _firewall_leftovers(self):
        left = []
        if self.system.platform() == "darwin":
            if self.paths.pf_anchor and self.system.exists(self.paths.pf_anchor):
                left.append(self.paths.pf_anchor)
            result = self.system.run(["pfctl", "-a", "grok-relay", "-sr"])
            if result.returncode == 0 and result.stdout.strip():
                left.append("pf anchor grok-relay")
            elif result.returncode not in (0, 1, 127):
                left.append("pf anchor grok-relay (could not check)")
            data = self.system.read_bytes(self.paths.pf_conf) or b""
            if b"grok-relay" in data:
                left.append(self.paths.pf_conf)
            return left
        result = self.system.run(["nft", "list", "table", "inet", "grok_relay"])
        if result.returncode == 0:
            left.append("nft table grok_relay")
        elif result.returncode not in (1, 127):
            left.append("nft table grok_relay (could not check)")
        result = self.system.run(["ufw", "status", "numbered"])
        if result.returncode == 0 and ufw_grok_numbers(_decode(result.stdout)):
            left.append("ufw rules commented grok-relay")
        return left

    def _credential_leftovers(self):
        # Presence only. Command output is discarded: a keychain listing can echo the secret.
        left = []
        if self.system.exists(self.paths.xai_key):
            left.append(self.paths.xai_key)
        if self.system.platform() == "darwin":
            try:
                result = self.system.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE])
            except Exception:
                left.append("keychain item grok-relay (could not check)")
                return left
            if result.returncode == 0:
                left.append("keychain item grok-relay")
        return left

    def _docker_leftovers(self):
        left = []
        specs = (
            ("container", ["docker", "ps", "-aq", "--filter", "label=" + COMPOSE_LABEL]),
            ("image", ["docker", "images", "-q", "--filter", "label=" + COMPOSE_LABEL]),
            ("volume", ["docker", "volume", "ls", "-q", "--filter", "label=" + COMPOSE_LABEL]),
        )
        for kind, argv in specs:
            try:
                result = self.system.run(argv)
            except Exception:
                left.append(kind + " (could not list)")
                continue
            if result.returncode != 0:
                left.append(kind + " (could not list)")
                continue
            for item in _decode(result.stdout).split():
                left.append(kind + " " + item)
        return left

    def _shim_leftovers(self):
        if self.system.exists(self.paths.shim):
            return [self.paths.shim]
        return []

    def _checklist(self):
        probes = (
            ("files", self._file_leftovers),
            ("account", self._account_leftovers),
            ("services", self._service_leftovers),
            ("listening ports 443/8443", self._port_leftovers),
            ("firewall rules", self._firewall_leftovers),
            ("credential store", self._credential_leftovers),
            ("containers/images/volumes", self._docker_leftovers),
            ("PATH shim", self._shim_leftovers),
        )
        hand = []
        for label, probe in probes:
            try:
                left = list(probe())
            except Exception:
                left = [label + " (check failed)"]
            print(label + ": " + ("FAIL" if left else "PASS"))
            hand.extend(left)
        if hand:
            print("Remove by hand:")
            for item in hand:
                print("- " + item)
            return 1
        return 0


def build_parser():
    # No --yes. An unknown flag, including --yes, is an argparse error.
    parser = argparse.ArgumentParser(prog="grok-relay")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="print version, pairing, and certificate expiry")
    sub.add_parser("rotate-xai-key", help="store a new xAI key (prompted, not echoed)")
    sub.add_parser("uninstall", help="remove this relay after you type uninstall")
    return parser


def main(argv=None, system=None, station=None, input_fn=None, getpass_fn=None):
    if argv is None:
        argv = sys.argv[1:]
    args = build_parser().parse_args(list(argv))
    system = system or RealSystem()
    tool = RelayCLI(system, station)
    if args.command == "status":
        return tool.status()
    if args.command == "rotate-xai-key":
        import getpass
        return tool.rotate(getpass_fn or getpass.getpass)
    if args.command == "uninstall":
        return tool.uninstall(input_fn or input)
    return 2


if __name__ == "__main__":
    sys.exit(main())
