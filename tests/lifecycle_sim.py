"""Run tools/install-files.sh (and install.sh's relay file list) for real into a temporary root.

Used by tests/test_lifecycle_scenarios.py and tools/release-check.sh. The script runs as root inside a user and
mount namespace (`unshare -rm`), chrooted into a tmpfs root with the host's /usr, /bin and /lib bound read-only
under it (a failed read-only remount stops the run), so every path it writes (/opt/grok-station, /usr/local, /etc) lands in that tmpfs and nothing on the
host changes. Commands that only talk to the host system (useradd, usermod, gpasswd, getent, systemctl, visudo)
are stubs. The service users map to uid 0 inside the namespace (only one uid can be mapped without privilege), so
file modes are real and owners are not checked here. The result is copied out to <out>/root.
"""
import os
import re
import shutil
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUBS = {"useradd": "exit 0", "usermod": "exit 0", "gpasswd": "exit 0", "systemctl": "exit 0", "visudo": "exit 0",
         "getent": "exit 2"}


class Unavailable(Exception):
    """This machine can't make a user and mount namespace (no unshare, or user namespaces are off)."""


def relay_files(src=ROOT):
    """The relay modules install.sh st_relay installs into $O/relay (the `for f in ...` line)."""
    with open(os.path.join(src, "install.sh"), encoding="utf-8") as fh:
        m = re.search(r"^for f in ([a-z0-9_. ]+); do run install -m644 relay/\$f \$O/relay/; done$", fh.read(), re.M)
    if not m:
        raise AssertionError("install.sh has no relay install line")
    return m.group(1).split()


def available():
    if not shutil.which("unshare"):
        return False
    r = subprocess.run(["unshare", "-rm", "sh", "-c", "d=$(mktemp -d) && mount -t tmpfs none \"$d\" && umount \"$d\""],
                       capture_output=True, timeout=30)
    return r.returncode == 0


RUNNER = r'''set -eu
PATH=$PATH:/usr/sbin:/sbin
R=$1; S=$2; OUT=$3; V=$4; RELAY_FILES=$5
mount -t tmpfs -o mode=755 none "$R"
for d in usr bin sbin lib lib64 lib32 libx32; do
  if [ -L "/$d" ]; then ln -s "$(readlink "/$d")" "$R/$d"
  elif [ -d "/$d" ]; then mkdir "$R/$d"; mount --rbind "/$d" "$R/$d"; mount -o remount,bind,ro "$R/$d"
    if ( : > "$R/$d/.gs-sim-write-probe" ) 2>/dev/null; then
      rm -f "$R/$d/.gs-sim-write-probe"; echo "lifecycle_sim: /$d is writable inside the temporary root" >&2; exit 3
    fi
  fi
done
mount -t tmpfs -o mode=755 none "$R/usr/local"
mkdir -p "$R/usr/local/bin" "$R/usr/local/sbin" "$R/usr/local/lib"
mkdir -p "$R/etc/systemd/system" "$R/etc/sudoers.d" "$R/opt" "$R/tmp" "$R/dev" "$R/src" "$R/stub" "$R/out" "$R/var/tmp"
chmod 1777 "$R/tmp"
mount --rbind /dev "$R/dev"
mount --rbind "$S" "$R/src"
mount --rbind "$OUT" "$R/out"
for f in ld.so.cache ld.so.conf localtime nsswitch.conf; do [ -e "/etc/$f" ] && cp -L "/etc/$f" "$R/etc/$f" || true; done
[ -d /etc/ssl ] && cp -rL /etc/ssl "$R/etc/" 2>/dev/null || true
printf "grok-station\n" > "$R/etc/hostname"
[ -d /etc/ld.so.conf.d ] && cp -rL /etc/ld.so.conf.d "$R/etc/" || true
[ -d /etc/alternatives ] && cp -rP /etc/alternatives "$R/etc/" || true
printf 'root:x:0:0::/root:/bin/sh\ngrok:x:0:0::/nonexistent:/bin/false\ngrok-admin:x:0:0::/nonexistent:/bin/false\n' > "$R/etc/passwd"
printf 'root:x:0:\ngrok:x:0:grok-admin\nsystemd-journal:x:0:grok-admin\n' > "$R/etc/group"
STUBS
mkdir -p "$R/opt/grok-station/wake"   # a station with the wake listener installed (docs/wake.md)
chroot "$R" /usr/bin/env -i PATH=/stub:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/tmp \
  GROK_STATION_NO_RESTART=1 sh ${GS_SIM_X:-} /src/tools/install-files.sh /src "$V"
for f in $RELAY_FILES; do chroot "$R" install -m644 "/src/relay/$f" /opt/grok-station/relay/; done
mkdir -p "$R/out/root/usr/local" "$R/out/root/etc"
cp -a "$R/opt" "$R/out/root/"
cp -a "$R/usr/local/lib" "$R/usr/local/bin" "$R/out/root/usr/local/"
cp -a "$R/etc/systemd" "$R/etc/sudoers.d" "$R/out/root/etc/"
'''


def install(out, src=ROOT, version=None):
    """Install src into <out>/root. Returns (root path, combined log). Raises Unavailable or AssertionError.

    Refuses to run as real root: there a failed read-only bind would leave the host's /usr and /lib writable from
    inside the chroot. A failed read-only remount, or a host directory still writable under the root, stops the
    run before install-files.sh starts.
    """
    if os.geteuid() == 0:
        raise Unavailable("refusing to run the install sim as root; run it as an ordinary user")
    if not available():
        raise Unavailable("unshare -rm with tmpfs mounts is not available here")
    if not version:
        with open(os.path.join(src, "VERSION"), encoding="utf-8") as fh:
            version = fh.read().strip()
    stubs = "\n".join("printf '#!/bin/sh\\n%s\\n' > \"$R/stub/%s\" && chmod 755 \"$R/stub/%s\"" % (body, name, name)
                      for name, body in STUBS.items())
    stubs += ("\nprintf '#!/bin/sh\\ncase \"${1:-}\" in -u) echo 0 ;; -nG) echo grok-admin grok systemd-journal ;; esac\\n"
              "exit 0\\n' > \"$R/stub/id\" && chmod 755 \"$R/stub/id\"")
    runner = RUNNER.replace("STUBS", stubs)
    rdir = tempfile.mkdtemp(prefix="gs-sim-root-")
    try:
        r = subprocess.run(["unshare", "-rm", "sh", "-c", runner, "runner", rdir, os.path.abspath(src),
                            os.path.abspath(out), version, " ".join(relay_files(src))],
                           capture_output=True, text=True, timeout=300)
    finally:
        shutil.rmtree(rdir, ignore_errors=True)
    log = r.stdout + r.stderr
    if r.returncode != 0:
        raise AssertionError("install-files.sh failed in the temporary root (exit %d):\n%s" % (r.returncode, log[-3000:]))
    return os.path.join(out, "root"), log
