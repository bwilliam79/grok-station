"""Shared path and host checks for the home-mode seal commands.

gs-seal-setup and gs-seal-reply import this file from their own directory.
No secrets are logged here.
"""
import argparse, base64, hashlib, hmac, json, os, re, stat, subprocess, sys
from urllib.parse import urlsplit

SLUG = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
REQUEST_ID = re.compile(r"^vs_[A-Za-z0-9_-]{22}$")
_UUID = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")
_IO_UUID = re.compile(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{36})"')

_IOREG = ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]


class Refused(Exception):
    """Fail closed. The message is never shown."""


class Unsupported(Exception):
    """A clear refusal. The message is safe to show."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


# Fixed paths. Callers do not pass these in.
MACHINE_ID_PATH = "/etc/machine-id"
PRODUCT_UUID_PATH = "/sys/class/dmi/id/product_uuid"
MOUNTINFO_PATH = "/proc/self/mountinfo"
CACHEDIR_TAG = "CACHEDIR.TAG"
CACHEDIR_SIGNATURE = "Signature: 8a477f597d28d172789f06886806bc55"
NOBACKUP_NAME = ".nobackup"

# Linux sync folders under $HOME, in addition to Dropbox and OneDrive*.
_LINUX_SYNC_DIRS = (
    "Nextcloud",
    "ownCloud",
    "pCloudDrive",
    "MEGA",
    "Insync",
    "Sync",
    "Google Drive",
)
_UNSAFE_EXACT_FS = ("cifs", "smb3", "9p", "drvfs", "virtiofs")
_PRODUCT_UUID = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write("refused\n")
        raise SystemExit(2)


def root_dir():
    return os.path.join(os.path.expanduser("~"), ".config", "grok-station")


def keys_dir():
    return os.path.join(root_dir(), "keys")


def _persona_name(slug, suffix):
    """Join a persona file under a fixed directory. Refuse a name that could escape it."""
    if not slug_ok(slug) or os.path.basename(slug) != slug:
        raise Refused()
    if "/" in slug or "\\" in slug or slug in (".", ".."):
        raise Refused()
    return slug + suffix


def key_path(slug):
    return os.path.join(keys_dir(), _persona_name(slug, ".key"))


def config_path(slug):
    return os.path.join(root_dir(), _persona_name(slug, ".json"))


def sent_path():
    return os.path.join(root_dir(), "sent-ids")


def log_path():
    return os.path.join(root_dir(), "gs-seal-reply.log")


def host_id_path():
    return os.path.join(root_dir(), "host-id")


def slug_ok(name):
    return isinstance(name, str) and name != "grok" and SLUG.fullmatch(name) is not None


def access_ok(st, limit):
    """Owned by this user, not a symlink, and no mode bit outside limit (0700 or 0600)."""
    try:
        mode = stat.S_IMODE(st.st_mode)
        uid = st.st_uid
        kind = st.st_mode
    except AttributeError:
        return False
    if uid != os.geteuid():
        return False
    if stat.S_ISLNK(kind):
        return False
    if mode & ~int(limit):
        return False
    return True


def _require_dir(path, limit=0o700):
    try:
        st = os.lstat(path)
    except OSError:
        raise Refused()
    if not stat.S_ISDIR(st.st_mode) or not access_ok(st, limit):
        raise Refused()


def _require_file(path, limit=0o600):
    try:
        st = os.lstat(path)
    except OSError:
        raise Refused()
    if not stat.S_ISREG(st.st_mode) or not access_ok(st, limit):
        raise Refused()


def _check_tree():
    """Refuse when the grok-station dir or the keys dir exists and is too open. Do not chmod them."""
    if os.path.lexists(root_dir()):
        _require_dir(root_dir(), 0o700)
    if os.path.lexists(keys_dir()):
        _require_dir(keys_dir(), 0o700)


def _open_private(path):
    """Open a 0600 file for read/write. Create it only when missing. Never tighten an existing file."""
    created = False
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        created = True
    except FileExistsError:
        try:
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            raise Refused()
    except OSError:
        raise Refused()
    try:
        if created:
            os.fchmod(fd, 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not access_ok(st, 0o600):
            raise Refused()
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_all(fd, data):
    view = data
    while view:
        n = os.write(fd, view)
        if n is None or n <= 0:
            raise Refused()
        view = view[n:]


def _pin(text):
    if not isinstance(text, str):
        return ""
    fp = text.replace(":", "").strip().lower()
    if len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp):
        return ""
    return fp


def _b64_32(text):
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        raw = base64.b64decode("".join(text.split()), validate=True)
    except Exception:
        return None
    if len(raw) != 32:
        return None
    return raw


def station_ok(url):
    if not isinstance(url, str) or not (8 <= len(url) <= 300):
        return False
    if any(ord(c) < 0x21 or ord(c) > 0x7e for c in url):
        return False
    if "@" in url or "\\" in url:
        return False
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    if parts.username is not None or parts.password is not None or parts.fragment or parts.query:
        return False
    if parts.path not in ("", "/"):
        return False
    return True


def _is_under(path, parent):
    path_f = os.path.realpath(path).casefold()
    parent_f = os.path.realpath(parent).casefold()
    if path_f == parent_f:
        return True
    if not parent_f.endswith(os.sep):
        parent_f += os.sep
    return path_f.startswith(parent_f)


def _read_nofollow(path, limit, optional=False):
    """Read a regular file. O_NOFOLLOW, size-capped. optional: missing or unreadable is None."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        if optional:
            return None
        raise Refused()
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused()
        raw = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise Refused()
    return raw


def _running_musl():
    """True when this process is linked against musl. glibc maps do not contain ld-musl."""
    if sys.platform != "linux":
        return False
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open("/proc/self/maps", flags)
    except OSError:
        return False
    try:
        blob = b""
        while len(blob) < 1024 * 1024:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            blob += chunk
            if b"ld-musl" in blob or b"libc.musl" in blob:
                return True
        return b"ld-musl" in blob or b"libc.musl" in blob
    finally:
        os.close(fd)


def refuse_musl():
    if _running_musl():
        raise Unsupported("Alpine/musl is not supported.")


def _unescape_mount(text):
    out = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 3 < len(text):
            octal = text[i + 1:i + 4]
            if len(octal) == 3 and all(c in "01234567" for c in octal):
                out.append(chr(int(octal, 8)))
                i += 4
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _mount_covers(path, mount):
    path = os.path.normpath(path)
    mount = os.path.normpath(mount)
    if path == mount:
        return True
    prefix = mount if mount.endswith(os.sep) else mount + os.sep
    return path.startswith(prefix)


def _fstype_unsafe(fstype):
    if not isinstance(fstype, str) or not fstype:
        return True
    kind = fstype.lower()
    if kind.startswith("fuse") or kind.startswith("nfs"):
        return True
    return kind in _UNSAFE_EXACT_FS


def _mount_fstype(path):
    """Filesystem type of the longest mountpoint prefix. Fail closed if unknown."""
    raw = _read_nofollow(MOUNTINFO_PATH, 1024 * 1024)
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        raise Refused()
    try:
        real = os.path.realpath(path)
    except OSError:
        raise Refused()
    best_len = -1
    best = None
    for line in text.splitlines():
        parts = line.split(" ")
        try:
            sep = parts.index("-")
        except ValueError:
            continue
        if sep < 5 or sep + 1 >= len(parts):
            continue
        mount = _unescape_mount(parts[4])
        if not mount.startswith("/"):
            continue
        if not _mount_covers(real, mount):
            continue
        length = len(os.path.normpath(mount))
        if length > best_len:
            best_len = length
            best = parts[sep + 1]
    if best is None:
        raise Refused()
    return best


def refuse_unsafe_location(root):
    """Refuse a key directory inside git, iCloud, CloudStorage, Dropbox, or OneDrive.

    On Linux, also refuse other sync folders, and a network or FUSE mount.
    """
    real = os.path.realpath(root)
    parent = os.path.dirname(real)
    while True:
        if os.path.lexists(os.path.join(parent, ".git")):
            raise Refused()
        nxt = os.path.dirname(parent)
        if nxt == parent:
            break
        parent = nxt
    home = os.path.expanduser("~")
    banned = [
        os.path.join(home, "Library", "Mobile Documents"),
        os.path.join(home, "Library", "CloudStorage"),
        os.path.join(home, "Dropbox"),
    ]
    if sys.platform == "linux":
        for name in _LINUX_SYNC_DIRS:
            banned.append(os.path.join(home, name))
    try:
        names = os.listdir(home)
    except OSError:
        raise Refused()
    for name in names:
        if not name.startswith("OneDrive"):
            continue
        path = os.path.join(home, name)
        if os.path.isdir(path):
            banned.append(path)
    for base in banned:
        if _is_under(real, base):
            raise Refused()
    if sys.platform != "linux":
        return
    if _fstype_unsafe(_mount_fstype(real)):
        raise Refused()


def _command_root(root):
    if not isinstance(root, str) or not root.startswith("/") or "\x00" in root:
        raise Refused()
    return root


def _marker_path(root, name):
    root = _command_root(root)
    if name not in (CACHEDIR_TAG, NOBACKUP_NAME):
        raise Refused()
    return os.path.join(root, name)


def linux_backup_markers_ok(root):
    """True only when CACHEDIR.TAG starts with the cache-directory signature and .nobackup is empty."""
    tag = _read_nofollow(_marker_path(root, CACHEDIR_TAG), 4096, optional=True)
    if tag is None:
        return False
    try:
        text = tag.decode("ascii")
    except UnicodeDecodeError:
        return False
    line = text.split("\n", 1)[0]
    if line.endswith("\r"):
        line = line[:-1]
    if line != CACHEDIR_SIGNATURE:
        return False
    body = _read_nofollow(_marker_path(root, NOBACKUP_NAME), 1, optional=True)
    return body == b""


def require_time_machine_exclusion(root):
    """Refuse unless this directory is kept out of backups.

    macOS: tmutil isexcluded prints [Excluded]. Fixed argv, no shell.
    Linux: CACHEDIR.TAG has the cache-directory signature and .nobackup exists and is empty.
    """
    root = _command_root(root)
    if sys.platform == "linux":
        if not linux_backup_markers_ok(root):
            raise Refused()
        return
    proc = subprocess.run(
        ["/usr/bin/tmutil", "isexcluded", root],
        capture_output=True)
    out = proc.stdout or b""
    if isinstance(out, str):
        text = out
    else:
        if len(out) > 8192:
            raise Refused()
        text = out.decode("utf-8", "replace")
    if "[Excluded]" not in text:
        raise Refused()


def _format_uuid(hex32):
    h = hex32.lower()
    if len(h) != 32 or any(c not in "0123456789abcdef" for c in h):
        raise Refused()
    text = "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])
    uuid = text.upper()
    if _UUID.fullmatch(uuid) is None:
        raise Refused()
    return uuid


def _one_line(raw, limit_ok):
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise Refused()
    if text.endswith("\n"):
        text = text[:-1]
    if text.endswith("\r"):
        text = text[:-1]
    if "\n" in text or "\r" in text or not limit_ok(text):
        raise Refused()
    return text


def _linux_hardware_uuid():
    """machine-id, mixed with the DMI product UUID when that file can be read.

    The mix is sha256(machine-id + newline + product uuid), lowercase, first 32 hex digits,
    shown as a UUID. machine-id alone is used when the product file cannot be opened.
    """
    raw = _read_nofollow(MACHINE_ID_PATH, 64)
    machine = _one_line(raw, lambda text: len(text) == 32 and all(c in "0123456789abcdefABCDEF" for c in text))
    machine = machine.lower()
    product_raw = _read_nofollow(PRODUCT_UUID_PATH, 128, optional=True)
    if product_raw is None:
        return _format_uuid(machine)
    product = _one_line(product_raw, lambda text: _PRODUCT_UUID.fullmatch(text) is not None)
    digest = hashlib.sha256()
    digest.update(machine.encode("ascii"))
    digest.update(b"\n")
    digest.update(product.lower().encode("ascii"))
    return _format_uuid(digest.hexdigest()[:32])


def _darwin_hardware_uuid():
    """Hardware UUID from IOPlatformExpertDevice. Fixed argv, no shell. Nothing is logged."""
    proc = subprocess.run(_IOREG, check=True, capture_output=True)
    raw = proc.stdout or b""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > 1024 * 1024:
        raise Refused()
    found = _IO_UUID.findall(raw.decode("utf-8", "replace"))
    del raw
    if len(found) != 1:
        raise Refused()
    uuid = found[0].upper()
    if _UUID.fullmatch(uuid) is None:
        raise Refused()
    return uuid


def hardware_uuid():
    """This computer's id. macOS: IOPlatformUUID. Linux: machine-id, plus the DMI uuid when readable."""
    if sys.platform == "darwin":
        return _darwin_hardware_uuid()
    if sys.platform == "linux":
        return _linux_hardware_uuid()
    raise Refused()


def _read_host_uuid():
    path = host_id_path()
    _require_file(path, 0o600)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise Refused()
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not access_ok(st, 0o600):
            raise Refused()
        raw = os.read(fd, 128)
        extra = os.read(fd, 1)
    finally:
        os.close(fd)
    if extra or not raw:
        raise Refused()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise Refused()
    if text.endswith("\n"):
        text = text[:-1]
    if "\n" in text or "\r" in text:
        raise Refused()
    uuid = text.upper()
    if _UUID.fullmatch(uuid) is None:
        raise Refused()
    return uuid


def _same_uuid(left, right):
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    a = left.upper().encode("ascii", "replace")
    b = right.upper().encode("ascii", "replace")
    if len(a) != len(b) or _UUID.fullmatch(left.upper()) is None or _UUID.fullmatch(right.upper()) is None:
        return False
    return hmac.compare_digest(a, b)


def require_same_host():
    """Refuse when host-id is missing or is not this Mac."""
    current = hardware_uuid()
    if not os.path.lexists(host_id_path()):
        raise Refused()
    stored = _read_host_uuid()
    if not _same_uuid(stored, current):
        raise Refused()


def _stdlib_chain_ok(path, uid, root_path="/"):
    """lstat each directory from path up to root_path: a real directory, owned by uid or root, no g/o write."""
    try:
        cur = os.path.normpath(path)
        stop = os.path.realpath(root_path)
        uid = int(uid)
    except (OSError, ValueError, TypeError):
        return False
    while True:
        try:
            st = os.lstat(cur)
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return False
        if st.st_uid != uid and st.st_uid != 0:
            return False
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
        if cur == stop:
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def _stdlib_dirs(uid=None, root_path="/"):
    """The standard library directory (where os.py lives) and its lib-dynload. No site-packages.

    Derived from os.__file__, not sysconfig: sysconfig imports _sysconfigdata lazily through sys.path.
    """
    base = os.path.normpath(os.path.realpath(os.path.dirname(os.__file__)))
    found = [base]
    dyn = os.path.join(base, "lib-dynload")
    if os.path.isdir(dyn):
        found.append(dyn)
    for norm in found:
        parts = norm.split(os.sep)
        if "site-packages" in parts or "dist-packages" in parts:
            sys.stderr.write("refused\n")
            raise SystemExit(1)
        if not _stdlib_chain_ok(norm, os.geteuid() if uid is None else uid, root_path):
            sys.stderr.write("refused\n")
            raise SystemExit(1)
    return found


def vendor_dir():
    return os.path.join(root_dir(), "vendor")


def vendor_manifest_path():
    return os.path.join(root_dir(), "vendor-manifest.json")


def _chain(top, bottom):
    """Absolute path components from top through bottom, symlinks left unresolved."""
    top = os.path.abspath(top)
    bottom = os.path.abspath(bottom)
    try:
        if os.path.commonpath([top, bottom]) != top:
            raise Refused()
    except ValueError:
        raise Refused()
    chain = []
    cur = bottom
    while True:
        chain.append(cur)
        if cur == top:
            chain.reverse()
            return chain
        parent = os.path.dirname(cur)
        if parent == cur:
            raise Refused()
        cur = parent


def _lstat_owned(path, uid, kind):
    """lstat: not a symlink, owned by uid, no group or other write bit."""
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        raise Refused()
    try:
        st = os.lstat(path)
    except OSError:
        raise Refused()
    if stat.S_ISLNK(st.st_mode):
        raise Refused()
    if kind == "dir" and not stat.S_ISDIR(st.st_mode):
        raise Refused()
    if kind == "file" and not stat.S_ISREG(st.st_mode):
        raise Refused()
    if st.st_uid != uid:
        raise Refused()
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise Refused()
    return st


def _sha256_file(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise Refused()
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused()
        digest = hashlib.sha256()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        return digest.hexdigest()
    finally:
        os.close(fd)


def vendor_file_hashes(vendor, uid):
    """Relative path to sha256 for each regular file. Refuse a symlink or an untrusted entry."""
    vendor = os.path.abspath(vendor)
    _lstat_owned(vendor, uid, "dir")
    found = {}
    for dirpath, dirnames, filenames in os.walk(vendor, followlinks=False):
        for name in dirnames:
            _lstat_owned(os.path.join(dirpath, name), uid, "dir")
        for name in filenames:
            path = os.path.join(dirpath, name)
            _lstat_owned(path, uid, "file")
            rel = os.path.relpath(path, vendor).replace(os.sep, "/")
            if not rel or rel.startswith("/") or rel.startswith("../") or "/../" in rel:
                raise Refused()
            found[rel] = _sha256_file(path)
    return found


def assert_vendor_trusted(home, uid, vendor=None, manifest=None):
    """Walk $HOME down through the vendor tree and require an exact manifest."""
    if not isinstance(home, str) or not home or "\x00" in home:
        raise Refused()
    home = os.path.abspath(os.path.expanduser(home))
    if vendor is None:
        vendor = os.path.join(home, ".config", "grok-station", "vendor")
    elif not isinstance(vendor, str) or "\x00" in vendor:
        raise Refused()
    else:
        vendor = os.path.abspath(vendor)
    if manifest is None:
        manifest = os.path.join(os.path.dirname(vendor), "vendor-manifest.json")
    elif not isinstance(manifest, str) or "\x00" in manifest:
        raise Refused()
    else:
        manifest = os.path.abspath(manifest)
    for path in _chain(home, vendor):
        _lstat_owned(path, uid, "dir")
    digests = vendor_file_hashes(vendor, uid)
    _lstat_owned(manifest, uid, "file")
    try:
        st = os.lstat(manifest)
    except OSError:
        raise Refused()
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise Refused()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(manifest, flags)
    except OSError:
        raise Refused()
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != int(uid) or stat.S_IMODE(st.st_mode) != 0o600:
            raise Refused()
        raw = os.read(fd, 8 * 1024 * 1024)
        if os.read(fd, 1):
            raise Refused()
    finally:
        os.close(fd)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except Exception:
        raise Refused()
    if not isinstance(doc, dict) or set(doc) != set(digests):
        raise Refused()
    for key, digest in digests.items():
        other = doc.get(key)
        if not isinstance(other, str):
            raise Refused()
        try:
            left = digest.encode("ascii")
            right = other.encode("ascii")
        except UnicodeError:
            raise Refused()
        if len(left) != len(right) or not hmac.compare_digest(left, right):
            raise Refused()


def load_nacl(home=None, uid=None):
    """Verify the vendor tree, then import nacl from that directory only."""
    if home is None:
        home = os.path.expanduser("~")
    if uid is None:
        uid = os.geteuid()
    home = os.path.abspath(os.path.expanduser(home))
    assert_vendor_trusted(home, uid)
    vendor = os.path.join(home, ".config", "grok-station", "vendor")
    # gs_seal_common is already imported. The script directory is no longer required.
    sys.path = [vendor] + _stdlib_dirs()
    import nacl
    return nacl
