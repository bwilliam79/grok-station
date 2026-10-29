#!/usr/bin/env python3
"""Create directories under a base and give them to an owner, never following a symlink.

`install -d -o grok` follows a symlink at any path it is given and chowns the target (checked as root, 0.13.6),
so a symlink grok planted at $O/wake or $O/wake/models would hand grok whatever it points at. Here every
component is opened with O_NOFOLLOW relative to its parent's descriptor, created if missing, and chowned
and chmodded through that descriptor. A symlink anywhere on the way is refused and nothing past it is touched.

Usage: safe-mkdirs.py [--mode 0750] USER BASE REL [REL ...]   (REL like wake/models; default mode 0755)
       safe-mkdirs.py --put USER BASE RELDIR SRC [SRC ...]

--put copies each SRC into the existing BASE/RELDIR (walked the same way, nothing created) as USER, mode 0644:
written to a fresh temp name through the directory's descriptor, fchowned, then renamed over the final name in
that same directory (Nix 0.13.6 L3: root never writes through a path grok could have swapped for a symlink).

BASE itself must be owned by root and not group- or other-writable, checked on its open descriptor (L4).
"""
import os
import sys

FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


ROOT_UID = 0  # the base must be owned by root; tests swap this for their own uid


class Refused(Exception):
    pass


def _open_base(base, root_uid):
    """Open BASE and trust it only if root owns it and neither group nor others can write it (Nix 0.13.6 L4)."""
    fd = _open(base, None, base)
    st = os.fstat(fd)
    uid = ROOT_UID if root_uid is None else root_uid
    if st.st_uid != uid:
        os.close(fd)
        raise Refused("%s is not owned by root (uid %d); refusing" % (base, st.st_uid))
    if st.st_mode & 0o022:
        os.close(fd)
        raise Refused("%s is group- or other-writable (%o); refusing" % (base, st.st_mode & 0o777))
    return fd


def _open(name, dir_fd, shown):
    try:
        return os.open(name, FLAGS, dir_fd=dir_fd)
    except OSError as e:
        if os.path.islink(shown):
            raise Refused("%s is a symlink; refusing" % shown)
        raise Refused("%s is not a plain directory (%s); refusing" % (shown, e.strerror))


def _parts(rel):
    parts = [p for p in rel.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise Refused("bad path %r" % rel)
    return parts


def mkdirs(base, rels, uid, gid, mode=0o755, root_uid=None):
    base = os.path.abspath(base)
    bfd = _open_base(base, root_uid)
    try:
        for rel in rels:
            fd, shown = os.dup(bfd), base
            try:
                for p in _parts(rel):
                    shown = os.path.join(shown, p)
                    try:
                        os.mkdir(p, mode, dir_fd=fd)
                    except FileExistsError:
                        pass
                    nfd = _open(p, fd, shown)
                    os.close(fd)
                    fd = nfd
                    os.fchown(fd, uid, gid)
                    os.fchmod(fd, mode)
            finally:
                os.close(fd)
    finally:
        os.close(bfd)


def put(base, rel_dir, srcs, uid, gid, mode=0o644, root_uid=None):
    base = os.path.abspath(base)
    fd, shown = _open_base(base, root_uid), base
    try:
        for p in _parts(rel_dir):
            shown = os.path.join(shown, p)
            nfd = _open(p, fd, shown)
            os.close(fd)
            fd = nfd
        for src in srcs:
            name = os.path.basename(src)
            if name in ("", ".", ".."):
                raise Refused("bad file name %r" % src)
            tmp = ".%s.new.%d" % (name, os.getpid())
            data = open(src, "rb").read()
            try:
                os.unlink(tmp, dir_fd=fd)
            except FileNotFoundError:
                pass
            out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                          0o600, dir_fd=fd)
            try:
                with os.fdopen(out, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fchown(f.fileno(), uid, gid)
                    os.fchmod(f.fileno(), mode)
                os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=fd)
                except OSError:
                    pass
                raise
    finally:
        os.close(fd)


def main(argv):
    import pwd
    if argv[:1] == ["--put"]:
        if len(argv) < 5:
            sys.stderr.write(__doc__)
            return 2
        pw = pwd.getpwnam(argv[1])
        try:
            put(argv[2], argv[3], argv[4:], pw.pw_uid, pw.pw_gid)
        except Refused as e:
            sys.stderr.write("safe-mkdirs: %s\n" % e)
            return 1
        return 0
    mode = 0o755
    if argv[:1] == ["--mode"]:
        mode, argv = int(argv[1], 8), argv[2:]
    if len(argv) < 3:
        sys.stderr.write(__doc__)
        return 2
    pw = pwd.getpwnam(argv[0])
    try:
        mkdirs(argv[1], argv[2:], pw.pw_uid, pw.pw_gid, mode=mode)
    except Refused as e:
        sys.stderr.write("safe-mkdirs: %s\n" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
