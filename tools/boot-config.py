#!/usr/bin/env python3
"""Idempotent kernel command line for grok-station (cmdline.txt). Prints "reboot" if anything changed.

Adds the quiet-boot options, including vt.global_cursor_default=0 (no blinking console cursor), only when missing
(an option already set to another value is kept). The file is replaced atomically (temp file fsynced, rename,
folder fsynced, original mode kept) with no backup copy. Since there is no backup, it refuses to write (error, exit 1,
file untouched) unless cmdline.txt is a single line with a root= word and the new line keeps every original word. Also a cmdline.txt.grok-station.bak left by an older release is deleted. config.txt is not touched here.
usage: boot-config.py [--boot DIR] [--dry-run]
"""
import errno, os, re, sys, tempfile

CMDLINE_OPTS = ["quiet", "splash", "loglevel=3", "vt.global_cursor_default=0", "logo.nologo", "consoleblank=0",
                "plymouth.ignore-serial-consoles"]


def cmdline_txt(text):
    line = text.strip().splitlines()[0] if text.strip() else ""
    words = line.split()
    for o in CMDLINE_OPTS:
        key = o.split("=", 1)[0]
        if not any(w == o or w.split("=", 1)[0] == key for w in words):
            words.append(o)
    return " ".join(words) + "\n"


def drop_stale_backup(path, dry):
    """Delete the backup copy older releases made (owner rule: no backup copies)."""
    if not dry:
        try:
            os.remove(path + ".grok-station.bak")
        except OSError:
            pass


class UnsafeEdit(Exception):
    pass


class ChangedButUnsynced(OSError):
    """cmdline.txt was replaced, but syncing its folder failed afterwards."""


def check_safe(old, new):
    """No backup to roll back to, so never write a line the Pi might not boot from."""
    if len([l for l in old.splitlines() if l.strip()]) > 1:
        raise UnsafeEdit("cmdline.txt has more than one line (damaged?); not changing it")
    old_words = old.strip().splitlines()[0].split() if old.strip() else []
    if not any(w.startswith("root=") and len(w) > 5 for w in old_words):
        raise UnsafeEdit("cmdline.txt has no root= option (empty or damaged?); not changing it")
    new_words = new.split()
    if new_words[:len(old_words)] != old_words or new.count("\n") != 1:
        raise UnsafeEdit("new cmdline.txt would drop or reorder original options; not changing it")


DIR_FSYNC_SKIP = {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF}


def write_durable(path, text):
    folder = os.path.dirname(path) or "."
    mode = os.stat(path).st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".cmdline.", suffix=".tmp")  # O_EXCL, random name, 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    try:  # cmdline.txt has changed now: any error from here on still needs a reboot
        dfd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dfd)
        except OSError as e:  # FAT /boot may not support fsync on a folder; the file itself is already synced
            if e.errno not in DIR_FSYNC_SKIP:
                raise
        finally:
            os.close(dfd)
    except OSError as e:
        raise ChangedButUnsynced(str(e)) from e


def apply(path, fn, dry):
    drop_stale_backup(path, dry)
    try:
        old = open(path).read()
    except OSError:
        return False
    new = fn(old)
    if new == old:
        return False
    check_safe(old, new)
    if not dry:
        write_durable(path, new)
    return True


def main(argv):
    boot = "/boot/firmware"
    if "--boot" in argv:
        boot = argv[argv.index("--boot") + 1]
    dry = "--dry-run" in argv
    try:
        changed = apply(os.path.join(boot, "cmdline.txt"), cmdline_txt, dry)
    except ChangedButUnsynced as e:  # the change is in place: ask for the reboot as usual, then fail
        print("reboot")
        print("boot-config: cmdline.txt changed but syncing its folder failed: " + str(e), file=sys.stderr)
        sys.exit(1)
    except (UnsafeEdit, OSError) as e:
        print("boot-config: " + str(e), file=sys.stderr)
        sys.exit(1)
    print("reboot" if changed else "unchanged")


if __name__ == "__main__":
    main(sys.argv[1:])
