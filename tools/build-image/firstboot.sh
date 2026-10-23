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

# Plain on-screen status. The unit sends stdout to the console; when the boot
# splash is running, its message line shows the same words. Details stay in
# the journal and /var/log/grok-station-install.log.
say() {
  if command -v plymouth >/dev/null 2>&1 && plymouth --ping 2>/dev/null; then
    plymouth display-message --text="$1" 2>/dev/null || true
  fi
  printf '\n  ==  %s  ==\n\n' "$1"
}
if [[ -w /dev/tty1 ]]; then
  printf '\033[2J\033[H\n\n    Installing Grok Station\n\n    This takes a while. The station restarts by itself when it is done.\n\n' > /dev/tty1 || true
fi
say "Installing Grok Station"

# install.sh downloads packages, so it needs the internet. The setup hotspot
# is part of the station and does not exist until install.sh has run, so it
# cannot be used for this step. Any working network will do: Ethernet, Wi-Fi
# set in Raspberry Pi Imager, or Wi-Fi added with build-image.sh --wifi-from-env.
COUNTRY_FILE=/var/lib/grok-station-firstboot-wifi-country
if [[ -f "$COUNTRY_FILE" ]] && command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_wifi_country "$(tr -dc '[:upper:]' < "$COUNTRY_FILE" | head -c 2)" || true
  rm -f -- "$COUNTRY_FILE"
  nmcli connection up grok-station-wifi >/dev/null 2>&1 || true
fi
waited=0
until curl -fsS --max-time 10 -o /dev/null https://deb.debian.org/ 2>/dev/null \
   || curl -fsS --max-time 10 -o /dev/null https://archive.raspberrypi.com/ 2>/dev/null; do
  if [[ "$waited" -ge 600 ]]; then
    say "No internet connection. Plug in a network cable, then restart to try again."
    exit 1
  fi
  [[ $((waited % 60)) -eq 0 ]] && say "Waiting for a network connection (cable or Wi-Fi)"
  sleep 10
  waited=$((waited + 10))
done

printf 'grok-station firstboot: running install.sh from %s\n' "$SRC"

# Leave GROK_STATION_NONINTERACTIVE unset. That variable selects update mode,
# which starts the services immediately. Fresh mode enables them and leaves
# them stopped until the reboot below, so the setup code appears once.
# --no-reboot: install.sh must not reboot before the stamp is removed, or the
# next boot would run install.sh again.
sh "$SRC/install.sh" --no-reboot

# The image holds a git archive (no .git). The in-app updater
# (admin/grok-station-helper update_remote, tools/grok-station-update) needs a
# clone in the recorded source dir, so turn the tree into one now that the
# network is up. Best effort: a failure leaves the station working, only
# without in-app updates (re-run bootstrap.sh to get a clone).
REPO_URL=https://github.com/bwilliam79/grok-station.git
COMMIT=$(awk '$1 == "commit" { print $2; exit }' /usr/local/src/grok-station-image-commit 2>/dev/null || true)
# git comes from install.sh (st_packages), the same as a bootstrap install.
if [[ ! -d "$SRC/.git" ]] && ! command -v git >/dev/null 2>&1; then
  printf 'grok-station firstboot: no git; in-app updates need a clone (bootstrap.sh)\n' >&2
elif [[ ! -d "$SRC/.git" ]]; then
  if git -C "$SRC" init --quiet \
    && git -C "$SRC" symbolic-ref HEAD refs/heads/main \
    && git -C "$SRC" remote add origin "$REPO_URL" \
    && git -C "$SRC" fetch --quiet origin main:refs/remotes/origin/main; then
    # Point main at the image's own commit when GitHub still has it, so the
    # updater sees the real installed version; otherwise origin/main.
    target=origin/main
    if [[ -n "$COMMIT" ]] && git -C "$SRC" cat-file -e "${COMMIT}^{commit}" 2>/dev/null; then
      target=$COMMIT
    fi
    git -C "$SRC" reset --quiet --mixed "$target"
    git -C "$SRC" branch --quiet --set-upstream-to=origin/main main || true
    printf 'grok-station firstboot: source is now a git clone at %s\n' "$target"
  else
    rm -rf -- "$SRC/.git"
    printf 'grok-station firstboot: could not fetch %s; in-app updates need a clone (bootstrap.sh)\n' "$REPO_URL" >&2
  fi
fi

say "Almost done. Restarting..."

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
