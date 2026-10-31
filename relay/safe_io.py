"""Safe file I/O for group-writable station folders (NIX-1312-01, NIX-1313-01/03).

state/, bots/, voice/*, audit/, personas/, portraits/, alerts/, late/, home-status/ and integrations/ can be written
by another group member. Every open here uses O_NOFOLLOW (a planted symlink is refused) and O_NONBLOCK (a planted
FIFO cannot block open()), then fstat must show a regular file, and reads are size-capped.

A missing file raises FileNotFoundError and is not logged, so callers keep their missing-file fallback. Any other
refusal is logged as '{what} read refused {reason} {path}' (or 'write refused') and raised as Refused, an OSError,
so a caller's existing `except OSError` fallback applies unchanged.
"""
import errno
import json
import os
import stat
import sys

SMALL_JSON = 64 * 1024        # one small state document (updates, avatar-auto, replies, pending, policy, ...)
LARGE_JSON = 1024 * 1024      # registries and lists that grow with the number of bots (personas, webhooks, keys)
PORTRAIT_MAX = 2 << 20        # same as the upload limit (botreply.AVATAR_IMAGE_MAX)

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_log = [lambda m: print(m, file=sys.stderr, flush=True)]


class Refused(OSError):
    """A symlink, non-regular, oversized or otherwise refused file. Already logged."""


def set_log(fn):
    """Route refusal lines to the caller's logger (relay.log)."""
    _log[0] = fn


def _refuse(what, verb, reason, path, log=None):
    (log or _log[0])(f"{what} {verb} refused {reason} {path}")
    return Refused(errno.EPERM, f"{what} {verb} refused {reason}")


def _reason(e):
    if e.errno == errno.ELOOP:
        return "symlink"
    if e.errno == errno.ENXIO:
        return "not-regular"
    return type(e).__name__


def read_bytes(path, max_bytes, what, log=None):
    """At most max_bytes from a regular file. FileNotFoundError when missing; Refused otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError as e:
        raise _refuse(what, "read", _reason(e), path, log) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _refuse(what, "read", "not-regular", path, log)
        if st.st_size > max_bytes:
            raise _refuse(what, "read", "oversized", path, log)
        data = b""
        while len(data) <= max_bytes:
            chunk = os.read(fd, max_bytes + 1 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > max_bytes:
            raise _refuse(what, "read", "oversized", path, log)
        return data
    except Refused:
        raise
    except OSError as e:
        raise _refuse(what, "read", type(e).__name__, path, log) from None
    finally:
        os.close(fd)


def read_text(path, max_bytes, what, encoding="utf-8", log=None):
    """read_bytes, decoded. A decode error is a ValueError (UnicodeDecodeError), as open().read() raised."""
    return read_bytes(path, max_bytes, what, log).decode(encoding)


def read_json(path, max_bytes, what, log=None):
    """read_text, parsed. Malformed JSON raises ValueError, as json.load did."""
    return json.loads(read_text(path, max_bytes, what, log=log))


def read_json_or_none(path, max_bytes, what, log=None):
    """None when missing (quiet) or refused or malformed (logged)."""
    try:
        return read_json(path, max_bytes, what, log)
    except FileNotFoundError:
        return None
    except Refused:
        return None
    except (OSError, ValueError) as e:
        (log or _log[0])(f"{what} read refused {type(e).__name__} {path}")
        return None


def _open_w(path, flags, mode, what):
    try:
        fd = os.open(path, flags | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC, mode)
    except FileExistsError:
        raise
    except FileNotFoundError:
        raise
    except OSError as e:  # ELOOP: symlink; ENXIO: a FIFO with no reader
        raise _refuse(what, "write", _reason(e), path) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _refuse(what, "write", "not-regular", path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_write(path, what, mode=0o600, excl=False):
    """Write fd on a regular file, truncated. O_CREAT (O_EXCL when excl). Refused for a symlink or a FIFO."""
    fd = _open_w(path, os.O_CREAT | (os.O_EXCL if excl else 0), mode, what)
    if not excl:
        try:
            os.ftruncate(fd, 0)
        except BaseException:
            os.close(fd)
            raise
    return fd


def open_append(path, what, mode=0o600, create=True):
    """(fd, created) for O_APPEND. create=True tries O_CREAT|O_EXCL first, then appends to the existing file."""
    if create:
        try:
            return _open_w(path, os.O_APPEND | os.O_CREAT | os.O_EXCL, mode, what), True
        except FileExistsError:
            pass
    return _open_w(path, os.O_APPEND, mode, what), False


def write_file(path, data, what, mode=0o600, excl=False, fsync=False):
    """open_write, fchmod to mode, write all of data (bytes or str), optional fsync, close."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd = open_write(path, what, mode, excl)
    try:
        try:
            os.fchmod(fd, mode)  # by fd: the mode does not depend on umask, and no path is followed
        except OSError:
            pass
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)
