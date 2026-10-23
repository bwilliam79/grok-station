#!/usr/bin/env bash
# First boot of a card written by tools/build-image/build-image.sh.
# The systemd unit grok-station-firstboot.service runs this once, as root.
# It does not write a Wi-Fi profile, an SSH key, a hostname, or a password.
set -euo pipefail

STAMP=/var/lib/grok-station-firstboot
SRC=/usr/local/src/grok-station
WANTS=/etc/systemd/system/multi-user.target.wants/grok-station-firstboot.service

if [[ ! -f "$STAMP" ]]; then
  printf 'grok-station firstboot: no stamp at %s; not running\n' "$STAMP"
  exit 0
fi

printf 'grok-station firstboot: running install.sh from %s\n' "$SRC"

# Leave GROK_STATION_NONINTERACTIVE unset. That variable selects update mode,
# which starts the services immediately. Fresh mode enables them and leaves
# them stopped until the reboot below, so the setup code appears once.
# --no-reboot: install.sh must not reboot before the stamp is removed, or the
# next boot would run install.sh again.
sh "$SRC/install.sh" --no-reboot

# Only reached when install.sh exits 0. Drop the enable symlink and the stamp
# before rebooting, so this unit does not run again.
rm -f -- "$WANTS"
rm -f -- "$STAMP"
sync

# The next boot starts the station services install.sh enabled.
# No network profile was written into this image. With no cable and no saved
# Wi-Fi, admin/grok_admin.py hotspot_loop starts the setup access point and
# the display shows that hotspot screen. That is the existing AP setup mode.
# This script does not start an access point and does not listen on a port.
printf 'grok-station firstboot: rebooting into the station\n'
if ! systemctl reboot; then
  printf 'grok-station firstboot: reboot failed. The services are enabled. Reboot to finish.\n' >&2
  exit 1
fi
