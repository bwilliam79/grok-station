#!/usr/bin/env python3
"""Wake listener code stays root-owned (0.13.14, Nix PI-01 and PI-03). Run as root by install.sh and install-files.sh.

The wake service runs as grok. Its code (grok_wake.py, gain.py, fewshot.py, wait_tone.py, wakewords.json, the shipped
models and setup lines) and the folders wake/, wake/models and wake/lines are root:root (files 0644, folders 0755);
install.sh and install-files.sh put them there. Only data the service writes stays grok-owned: wake/models/enrolled
(and everything in it). timers.json and chime.wav moved out of wake/ (see docs/operations.md).

This tool, through directory fds opened with O_NOFOLLOW (a planted symlink is never followed):
  pycache   empties and removes wake/__pycache__ (the unit sets PYTHONDONTWRITEBYTECODE=1, so nothing re-creates it).
            Regular files and symlinks are unlinked, never followed; a sub-folder is left and reported.
  old-venv  removes the unused root-owned wake/venv (pre-0.13.7), only when wake/ is a root-owned folder nobody else
            can write, wake/venv is a real folder (not a symlink) owned by root, and the installed wake unit does not
            mention wake/venv. A grok-owned wake/venv is left to install.sh (removed as grok).
  report    prints every entry under wake/ (not models/enrolled) that is not root-owned, so a leftover shows in the log.
Missing paths are fine (idempotent). Usage: python3 -I tools/wake-code-perms.py [--unit FILE] [/opt/grok-station]
"""
import os
import shutil
import stat
import sys

DIRF = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
UNIT = "/etc/systemd/system/grok-station-wake.service"
ROOT_UID = 0  # tests swap this for their own uid


def _log(msg):
    print("wake-code-perms: " + msg, file=sys.stderr)


def _open_wake(base):
    try:
        return os.open(os.path.join(base, "wake"), DIRF)
    except OSError:
        return None  # missing, or a symlink in place of wake/ (ELOOP): leave it alone


def remove_pycache(wfd):
    removed = []
    try:
        cfd = os.open("__pycache__", DIRF, dir_fd=wfd)
    except OSError:
        return removed
    left = False
    try:
        for name in os.listdir(cfd):
            try:
                st = os.stat(name, dir_fd=cfd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(st.st_mode):
                _log("left wake/__pycache__/%s (a folder)" % name)
                left = True
                continue
            try:
                os.unlink(name, dir_fd=cfd)
                removed.append("wake/__pycache__/" + name)
            except OSError as e:
                _log("left wake/__pycache__/%s (%s)" % (name, type(e).__name__))
                left = True
    finally:
        os.close(cfd)
    if not left:
        try:
            os.rmdir("__pycache__", dir_fd=wfd)
            removed.append("wake/__pycache__")
        except OSError as e:
            _log("left wake/__pycache__ (%s)" % type(e).__name__)
    return removed


def unit_uses_old_venv(unit):
    try:
        with open(unit, encoding="utf-8", errors="replace") as f:
            return "wake/venv" in f.read()
    except FileNotFoundError:
        return False
    except OSError:
        return True  # can't tell: keep it


def remove_old_venv(wfd, unit=UNIT, root_uid=None):
    uid = ROOT_UID if root_uid is None else root_uid
    wst = os.fstat(wfd)
    if wst.st_uid != uid or wst.st_mode & 0o022:
        _log("left wake/venv: wake/ is not a root-owned, root-only-writable folder")
        return False
    try:
        st = os.stat("venv", dir_fd=wfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
        return False  # a symlink or file, or grok-owned (install.sh removes that one as grok)
    if unit_uses_old_venv(unit):
        _log("left wake/venv: %s still mentions it" % unit)
        return False
    if not shutil.rmtree.avoids_symlink_attacks:
        _log("left wake/venv: this Python's rmtree can follow symlinks")
        return False
    try:
        shutil.rmtree("venv", dir_fd=wfd)
    except OSError as e:  # partly removed is fine; never abort the install over it
        _log("left part of wake/venv (%s)" % type(e).__name__)
        return False
    return True


def report(wfd, root_uid=None, rel="wake"):
    uid = ROOT_UID if root_uid is None else root_uid
    out = []
    for name in sorted(os.listdir(wfd)):
        path = rel + "/" + name
        if path == "wake/models/enrolled":
            continue
        try:
            st = os.stat(name, dir_fd=wfd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if st.st_uid != uid:
            out.append(path)
        if stat.S_ISDIR(st.st_mode) and path in ("wake/models", "wake/lines"):
            try:
                sfd = os.open(name, DIRF, dir_fd=wfd)
            except OSError:
                continue
            try:
                out.extend(report(sfd, root_uid, path))
            finally:
                os.close(sfd)
    return out


def main(argv):
    unit = UNIT
    if argv[:1] == ["--unit"] and len(argv) > 1:
        unit, argv = argv[1], argv[2:]
    base = argv[0] if argv else "/opt/grok-station"
    wfd = _open_wake(base)
    if wfd is None:
        return 0
    try:
        for p in remove_pycache(wfd):
            print("removed " + p)
        if remove_old_venv(wfd, unit):
            print("removed wake/venv")
        for p in report(wfd):
            _log("not root-owned: " + p)
    finally:
        os.close(wfd)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
