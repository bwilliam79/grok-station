#!/usr/bin/env bash
# First boot of a card written by tools/build-image/build-image.sh.
# The systemd unit grok-station-firstboot.service runs this once, as root.
# No network is needed: the image build already installed the packages, the
# Python environments and the wake models by running install.sh in build mode.
# This runs the same install.sh with GROK_STATION_OFFLINE=1, which does the
# parts that belong to this Pi: its own secrets and admin certificate, the
# station files, boot screen, services, and the health check.
# It does not write an SSH key, a hostname, or a password.
set -euo pipefail

STAMP=/var/lib/grok-station-firstboot
SRC=/usr/local/src/grok-station
WANTS=/etc/systemd/system/multi-user.target.wants/grok-station-firstboot.service
COUNTRY_FILE=/var/lib/grok-station-firstboot-wifi-country

if [[ ! -f "$STAMP" ]]; then
  printf 'grok-station firstboot: no stamp at %s; not running\n' "$STAMP"
  exit 0
fi

# Plain on-screen status. The unit sends stdout to the console. Details stay
# in the journal and /var/log/grok-station-install.log.
say() {
  if command -v plymouth >/dev/null 2>&1 && plymouth --ping 2>/dev/null; then
    plymouth display-message --text="$1" 2>/dev/null || true
  fi
  printf '\n  ==  %s  ==\n\n' "$1"
}
if [[ -w /dev/tty1 ]]; then
  printf '\033[2J\033[H\n\n    Getting Grok Station ready\n\n    This takes a few minutes. The station restarts by itself when it is done.\n\n' > /dev/tty1 || true
fi
say "Getting Grok Station ready"

# Optional Wi-Fi from build-image.sh --wifi-from-env. Raspberry Pi OS keeps
# Wi-Fi blocked until a country is set. Nothing here waits for a network.
if [[ -f "$COUNTRY_FILE" ]] && command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_wifi_country "$(tr -dc '[:upper:]' < "$COUNTRY_FILE" | head -c 2)" || true
  rm -f -- "$COUNTRY_FILE"
fi

# Leave GROK_STATION_NONINTERACTIVE unset: that selects update mode, which
# starts the services now. Fresh mode enables them for the reboot below, so
# the setup code (or the setup hotspot when no network is saved) shows once.
# --no-reboot: the stamp must be removed before rebooting.
GROK_STATION_OFFLINE=1 sh "$SRC/install.sh" --no-reboot

say "Almost done. Restarting..."

# Only reached when install.sh exits 0. A failure keeps the stamp, so the
# next boot tries again.
rm -f -- "$WANTS"
rm -f -- "$STAMP"
sync

# With no cable and no saved Wi-Fi, admin/grok_admin.py hotspot_loop starts
# the setup hotspot after this reboot and the screen shows how to join it.
if ! systemctl reboot; then
  printf 'grok-station firstboot: reboot failed. Reboot to finish.\n' >&2
  exit 1
fi
