#!/usr/bin/env python3
"""Unpack the verified Vosk model zip for the wake listener's on-device "stop" word.

usage: vosk-unpack.py <zip> <models dir> <model name> <sha256>

The zip's SHA-256 is checked again here. Every entry must be a plain file or folder under <model name>/:
no absolute paths, no "..", no symlinks or other special files. It is extracted into a temporary folder
next to the target and renamed into place only after the whole extract worked. Any failure removes the
temporary folder and leaves no model folder behind (an older verified one is kept). Exit 0 on success.
"""
import hashlib
import os
import shutil
import stat
import sys
import tempfile
import zipfile


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_member(info, model):
    """True for a regular file or folder inside model/. Rejects absolute, parent, and symlink entries."""
    name = info.filename
    if not name or "\\" in name or name.startswith("/") or os.path.isabs(name) or (len(name) > 1 and name[1] == ":"):
        return False
    parts = name.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    if parts[0] != model:
        return False
    mode = info.external_attr >> 16
    if mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        return False  # symlink, device, fifo, socket
    return True


def unpack(zpath, dest, model, sha, extract=None):
    if sha256_file(zpath) != sha:
        return False
    os.makedirs(dest, exist_ok=True)
    final = os.path.join(dest, model)
    tmp = tempfile.mkdtemp(prefix=".vosk-", dir=dest)
    try:
        with zipfile.ZipFile(zpath) as z:
            infos = z.infolist()
            if not infos or not all(safe_member(i, model) for i in infos):
                return False
            for info in infos:
                (extract or z.extract)(info, tmp)
        staged = os.path.join(tmp, model)
        if not os.path.isdir(staged):
            return False
        with open(os.path.join(staged, ".sha256"), "w") as f:
            f.write(sha + "\n")
        old = None
        if os.path.lexists(final):
            old = final + ".old-" + os.path.basename(tmp)
            os.rename(final, old)
        try:
            os.rename(staged, final)
        except OSError:
            if old:
                os.rename(old, final)
            raise
        if old:
            shutil.rmtree(old, ignore_errors=True)
        return True
    except Exception:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    if len(argv) != 5:
        print("usage: vosk-unpack.py <zip> <models dir> <model name> <sha256>", file=sys.stderr)
        return 2
    return 0 if unpack(*argv[1:5]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
