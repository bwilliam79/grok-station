#!/bin/sh
# Install grok-station program files from an unpacked repo dir (used by install.sh and the admin page's updater).
# usage: install-files.sh <repo dir> <version>
set -eu
S=$1; V=${2:-dev}
id grok-admin >/dev/null 2>&1 || useradd -r -g grok -G adm -M -d /nonexistent -s /usr/sbin/nologin grok-admin
install -d -m755 /opt/grok-station /opt/grok-station/admin /opt/grok-station/admin/web /opt/grok-station/admin/web/assets /usr/local/lib/grok-station
install -m755 "$S/app/grok_station.py" /opt/grok-station/grok_station.py.new && mv /opt/grok-station/grok_station.py.new /opt/grok-station/grok_station.py
install -m644 "$S/app/dimming.py" /opt/grok-station/dimming.py
install -m644 "$S/app/cards.py" /opt/grok-station/cards.py.new && mv /opt/grok-station/cards.py.new /opt/grok-station/cards.py
if [ -d /opt/grok-station/wake ]; then  # wake listener installed (docs/wake.md)
  install -o grok -g grok -m644 "$S/wake/grok_wake.py" /opt/grok-station/wake/grok_wake.py.new && mv /opt/grok-station/wake/grok_wake.py.new /opt/grok-station/wake/grok_wake.py
  install -o grok -g grok -m644 "$S/wake/gain.py" /opt/grok-station/wake/gain.py.new && mv /opt/grok-station/wake/gain.py.new /opt/grok-station/wake/gain.py
  install -o grok -g grok -m644 "$S/wake/fewshot.py" /opt/grok-station/wake/fewshot.py.new && mv /opt/grok-station/wake/fewshot.py.new /opt/grok-station/wake/fewshot.py
  install -d -o grok -g grok /opt/grok-station/wake/models /opt/grok-station/wake/models/enrolled
  install -o grok -g grok -m644 "$S/wake/models/fewshot_negatives.npz" /opt/grok-station/wake/models/fewshot_negatives.npz
fi
[ -d /opt/grok-station/wake ] && [ -d "$S/wake/lines" ] && install -d -o grok -g grok /opt/grok-station/wake/lines && install -o grok -g grok -m644 "$S"/wake/lines/*.wav /opt/grok-station/wake/lines/
install -m755 "$S/admin/grok_admin.py" /opt/grok-station/admin/
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
rm -f /opt/grok-station/assets/portraits/grok.png /opt/grok-station/admin/web/assets/grok.png
install -o root -g root -m755 "$S/admin/grok-station-helper" /usr/local/lib/grok-station/grok-station-helper.new && mv /usr/local/lib/grok-station/grok-station-helper.new /usr/local/lib/grok-station/grok-station-helper
install -o root -g root -m755 "$S/tools/grok-station" /usr/local/bin/grok-station
/usr/local/bin/grok-station tls-cert >/dev/null  # per-station admin page cert (kept if still valid for this hostname)
install -o root -g root -m440 "$S/admin/sudoers.example" /etc/sudoers.d/grok-station-admin.new && visudo -cf /etc/sudoers.d/grok-station-admin.new >/dev/null && mv /etc/sudoers.d/grok-station-admin.new /etc/sudoers.d/grok-station-admin
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
