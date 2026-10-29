#!/bin/sh
# Install grok-station program files from an unpacked repo dir (used by install.sh and the admin page's updater).
# usage: install-files.sh <repo dir> <version>
set -eu
S=$1; V=${2:-dev}
id grok-admin >/dev/null 2>&1 || useradd -r -g grok -G adm -M -d /nonexistent -s /usr/sbin/nologin grok-admin
install -d -m755 /opt/grok-station /opt/grok-station/admin /opt/grok-station/admin/web /opt/grok-station/admin/web/assets
install -d -o root -g root -m755 /usr/local/lib/grok-station
install -m755 "$S/app/grok_station.py" /opt/grok-station/grok_station.py.new && mv /opt/grok-station/grok_station.py.new /opt/grok-station/grok_station.py
install -m644 "$S/app/dimming.py" /opt/grok-station/dimming.py
install -m644 "$S/app/cards.py" /opt/grok-station/cards.py.new && mv /opt/grok-station/cards.py.new /opt/grok-station/cards.py
if [ -d /opt/grok-station/wake ]; then  # wake listener installed (docs/wake.md)
  # Root writes into grok-owned dirs only through safe-mkdirs --put (dir fds, O_NOFOLLOW; Nix 0.13.6 L3).
  python3 -I - --put grok /opt/grok-station wake "$S/wake/grok_wake.py" "$S/wake/gain.py" "$S/wake/fewshot.py" "$S/wake/wait_tone.py" < "$S/tools/safe-mkdirs.py"
  # Not `install -d -o grok`: root would follow a symlink grok planted and chown its target (Nix 0.13.6 M1).
  python3 -I - grok /opt/grok-station wake/models wake/models/enrolled < "$S/tools/safe-mkdirs.py"
  python3 -I - --put grok /opt/grok-station wake/models "$S/wake/models/fewshot_negatives.npz" < "$S/tools/safe-mkdirs.py"
fi
if [ -d /opt/grok-station/wake ] && [ -d "$S/wake/lines" ]; then
  python3 -I - grok /opt/grok-station wake/lines < "$S/tools/safe-mkdirs.py"
  python3 -I - --put grok /opt/grok-station wake/lines "$S"/wake/lines/*.wav < "$S/tools/safe-mkdirs.py"
fi
install -m755 "$S/admin/grok_admin.py" /opt/grok-station/admin/
install -m644 "$S/admin/gs_routine.py" "$S/admin/bot_content.py" "$S/admin/gs_integrations.py" "$S/admin/bot-releases.txt" "$S/admin/routine-home.txt" "$S/admin/routine-cloud.txt" /opt/grok-station/admin/
# Every module grok_admin.py imports must be installed next to it (a missing one crash-loops the admin page).
rm -f /opt/grok-station/admin/pairing.py /opt/grok-station/admin/pair_listener.py /opt/grok-station/admin/release.py  # removed (single device)
install -m644 "$S"/admin/web/index.html "$S"/admin/web/deploy-pins.js "$S"/admin/web/app.js "$S"/admin/web/admin.css /opt/grok-station/admin/web/
install -m644 "$S"/admin/web/assets/* /opt/grok-station/admin/web/assets/
install -d /opt/grok-station/assets/brand
if [ -d "$S/assets/brand" ]; then
  install -m644 "$S"/assets/brand/*.png "$S"/assets/brand/*.svg /opt/grok-station/assets/brand/
fi
install -d /opt/grok-station/assets/sounds
if [ -d "$S/assets/sounds" ]; then
  install -m644 "$S"/assets/sounds/* /opt/grok-station/assets/sounds/
fi
install -m644 "$S/sounds.py" /opt/grok-station/sounds.py
# 0.13.7 (Nix M1b): the root helper loads gain.py, so it gets a root-owned copy beside sounds.py, never the
# grok-editable $O/wake/gain.py. /opt/grok-station and every parent must be root-owned and not group/other-writable.
python3 -I - /opt/grok-station <<'PY' || { echo "refusing to install gain.py: /opt/grok-station or a parent is not a root-owned, root-only-writable folder" >&2; exit 1; }
import os, stat, sys
p = sys.argv[1]
while True:
    st = os.lstat(p)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
        sys.exit(1)
    if p == "/":
        break
    p = os.path.dirname(p)
PY
install -o root -g root -m644 "$S/wake/gain.py" /opt/grok-station/gain.py.new && mv /opt/grok-station/gain.py.new /opt/grok-station/gain.py
rm -f /opt/grok-station/assets/portraits/grok.png /opt/grok-station/admin/web/assets/grok.png
install -o root -g root -m755 "$S/admin/grok-station-helper" /usr/local/lib/grok-station/grok-station-helper.new && mv /usr/local/lib/grok-station/grok-station-helper.new /usr/local/lib/grok-station/grok-station-helper
install -o root -g root -m755 "$S/scripts/gs-usb-reset" /usr/local/lib/grok-station/gs-usb-reset.new && mv /usr/local/lib/grok-station/gs-usb-reset.new /usr/local/lib/grok-station/gs-usb-reset
install -o root -g root -m755 "$S/tools/grok-station" /usr/local/bin/grok-station
/usr/local/bin/grok-station tls-cert >/dev/null  # per-station admin page cert (kept if still valid for this hostname)
install -o root -g root -m440 "$S/admin/sudoers.example" /etc/sudoers.d/grok-station-admin.new && visudo -cf /etc/sudoers.d/grok-station-admin.new >/dev/null && mv /etc/sudoers.d/grok-station-admin.new /etc/sudoers.d/grok-station-admin
install -o root -g root -m440 "$S/admin/sudoers-usb.example" /etc/sudoers.d/grok-station-usb.new && visudo -cf /etc/sudoers.d/grok-station-usb.new >/dev/null && mv /etc/sudoers.d/grok-station-usb.new /etc/sudoers.d/grok-station-usb
install -m644 "$S"/systemd/grok-station.service "$S"/systemd/grok-station-admin.service /etc/systemd/system/
[ -d /opt/grok-station/wake ] && install -m644 "$S"/systemd/grok-station-wake.service /etc/systemd/system/
echo "$V" > /opt/grok-station/VERSION
systemctl daemon-reload
systemctl enable grok-station-admin.service >/dev/null 2>&1 || true
if [ "${GROK_STATION_NO_RESTART:-0}" != 1 ]; then  # a fresh install starts everything at its reboot instead
  systemctl restart grok-station || true
  [ -d /opt/grok-station/wake ] && systemctl restart grok-station-wake || true
  systemctl restart grok-station-admin
fi
