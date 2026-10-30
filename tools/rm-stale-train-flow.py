#!/usr/bin/env python3
"""Remove the pre-0.13.8 grok-owned copy of train_flow.py (and its .pyc) from <base>/wake.

Run as root by install.sh and tools/install-files.sh. grok owns <base>/wake, so every step goes
through directory fds opened with O_NOFOLLOW: a planted symlink is never followed. Only
<base>/wake/train_flow.py and <base>/wake/__pycache__/train_flow.*.pyc are removed, and only
when they are regular files. Missing paths are fine (idempotent).
Usage: python3 -I tools/rm-stale-train-flow.py [/opt/grok-station]
"""
import os
import re
import stat
import sys

DIRF = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
PYC = re.compile(r"^train_flow\.[A-Za-z0-9_-]+(\.opt-[12])?\.pyc$")


def _unlink_regular(dfd, name):
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    try:
        os.unlink(name, dir_fd=dfd)
    except OSError as e:  # swapped between stat and unlink (e.g. for a folder): skip, never abort install
        print("rm-stale-train-flow: left %s (%s)" % (name, type(e).__name__), file=sys.stderr)
        return False
    return True


def remove(base="/opt/grok-station"):
    removed = []
    try:
        wfd = os.open(os.path.join(base, "wake"), DIRF)
    except (FileNotFoundError, NotADirectoryError):
        return removed
    except OSError:
        return removed  # a symlink in place of wake/ (ELOOP): leave it alone
    try:
        if _unlink_regular(wfd, "train_flow.py"):
            removed.append("wake/train_flow.py")
        try:
            cfd = os.open("__pycache__", DIRF, dir_fd=wfd)
        except OSError:
            return removed
        try:
            for name in os.listdir(cfd):
                if PYC.match(name) and _unlink_regular(cfd, name):
                    removed.append("wake/__pycache__/" + name)
        finally:
            os.close(cfd)
    finally:
        os.close(wfd)
    return removed


if __name__ == "__main__":
    for r in remove(sys.argv[1] if len(sys.argv) > 1 else "/opt/grok-station"):
        print("removed stale " + r)
