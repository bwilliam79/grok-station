#!/bin/sh
# grok-station single-device installer. Run on the Pi as root from the cloned repo:
#   sudo ./install.sh            install or update (safe to re-run)
#   sudo ./install.sh --dry-run  print what would run, change nothing
# Everything runs on this Pi: display, wake listener, admin page (HTTPS) and the voice relay (loopback only).
# Secrets are generated here and never printed. The xAI key is entered on the admin page during first-run setup.
set -eu
cd "$(dirname "$0")"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
run() { if [ "$DRY" = 1 ]; then echo "+ $*"; else "$@"; fi; }
sh_() { if [ "$DRY" = 1 ]; then echo "+ sh -c '$1'"; else sh -c "$1"; fi; }
[ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || { echo "Run with sudo." >&2; exit 1; }
[ "$(uname -m)" = aarch64 ] || [ "$DRY" = 1 ] || { echo "Needs 64-bit Raspberry Pi OS (aarch64)." >&2; exit 1; }
O=/opt/grok-station; S=/etc/grok-station/secrets; V=/var/lib/grok-station
VER="$(git rev-parse --short HEAD 2>/dev/null || echo dev)"

echo "== packages"
run apt-get update
run apt-get install -y --no-install-recommends plymouth plymouth-themes python3-pygame python3-qrcode python3-venv \
  python3-pip python3-cryptography alsa-utils openssl curl libegl1 libegl-mesa0 libgles2 libgbm1 libgl1-mesa-dri

echo "== users"
sh_ 'id grok >/dev/null 2>&1 || useradd -r -m -s /usr/sbin/nologin grok'
sh_ 'getent group grok-admin >/dev/null || groupadd -r grok-admin'
sh_ 'id grok-relay >/dev/null 2>&1 || useradd -r -g grok-admin -M -d /nonexistent -s /usr/sbin/nologin grok-relay'
run usermod -aG audio grok

echo "== display files"
run install -d $O $O/assets/fonts $O/assets/portraits $O/assets/brand $O/assets/avatars $O/assets/status
run install -m755 app/grok_station.py $O/
run install -m644 app/avatars.py $O/
sh_ "install -m644 assets/fonts/* $O/assets/fonts/"
sh_ "install -m644 assets/brand/*.png assets/brand/*.svg $O/assets/brand/"
sh_ "install -m644 assets/avatars/default-*.svg $O/assets/avatars/"
sh_ "install -m644 assets/status/*.svg $O/assets/status/"
run install -m644 systemd/grok-station.service /etc/systemd/system/

echo "== secrets and state (generated, never printed)"
run install -d -o root -g root -m 0711 /etc/grok-station $S
gen() {  # gen <file> <owner:group> <mode>: create once with a random token
  sh_ "[ -s $1 ] || { umask 077; t=\$(mktemp $S/.t.XXXXXX); openssl rand -hex 32 > \$t; chown $2 \$t; chmod $3 \$t; mv -f \$t $1; }"
}
gen $S/local_token grok-relay:grok 0440       # display + wake -> relay
gen $S/admin_token grok-relay:grok-admin 0440 # admin page -> relay bots API
sh_ "[ -s /etc/grok-station/env ] || { umask 077; printf 'GROK_STATION_TOKEN=%s\nGROK_RELAY_URL=ws://127.0.0.1:8790/ws\n' \$(openssl rand -hex 24) > /etc/grok-station/env; chown root:grok /etc/grok-station/env; chmod 0640 /etc/grok-station/env; }"
sh_ "[ -e /etc/grok-station/wake.env ] || { install -m 0640 -o root -g grok /dev/null /etc/grok-station/wake.env; }"
for d in $V/state $V/bots $V/voice $V/voice/minted $V/voice/pending $V/voice/replies $V/audit $V/personas \
         $V/portraits $V/alerts $V/wakewords; do
  run install -d -o grok-relay -g grok-admin -m 0770 $d
done
run chmod 0755 $V
run install -d -o grok -g grok -m 0750 $V/enroll $V/enroll/inbox

echo "== voice relay (native venv, loopback only)"
run install -d -o root -g root -m 0755 $O/relay
for f in relay.py handoff.py tools.py t1.py release.py; do run install -m644 relay/$f $O/relay/; done
sh_ "[ -x $O/relay/venv/bin/python ] || python3 -m venv $O/relay/venv"
run $O/relay/venv/bin/pip install --quiet --require-hashes -r relay/requirements.txt
run install -m644 systemd/grok-station-relay.service /etc/systemd/system/

echo "== wake listener (Hey Grok)"
run install -d -o grok -g grok $O/wake $O/wake/models $O/wake/models/enrolled $O/portraits  # portraits: persona cache the wake listener writes
sh_ "[ -x $O/wake/venv/bin/python ] || python3 -m venv $O/wake/venv"
run $O/wake/venv/bin/pip install --quiet --no-deps openwakeword
run $O/wake/venv/bin/pip install --quiet onnxruntime numpy tqdm scipy scikit-learn requests "websockets>=17"
run $O/wake/venv/bin/python -c 'import openwakeword.utils as u; u.download_models(["feature-models-only"])'
run install -o grok -g grok -m644 wake/grok_wake.py wake/wakewords.json $O/wake/
run install -o grok -g grok -m644 wake/models/hey_grok.onnx $O/wake/models/

echo "== boot splash and kiosk"
BOOT_CHANGED=no
if [ "$DRY" = 1 ]; then echo "+ python3 tools/boot-config.py"; else
  [ "$(python3 tools/boot-config.py)" = reboot ] && BOOT_CHANGED=yes
fi
sh_ "grep -q '^disable_splash=1' /boot/firmware/config.txt || echo disable_splash=1 >> /boot/firmware/config.txt"
sh_ "if sh tools/install-splash.sh '$PWD' --prepare; then plymouth-set-default-theme -R echo; else plymouth-set-default-theme -R text || true; fi"
run systemctl mask getty@tty1.service

echo "== admin page (HTTPS) and services"
run sh tools/install-files.sh "$PWD" "$VER"
run usermod -aG grok-admin,audio grok-admin   # admin_token (group grok-admin); audio: list mics and speakers
run systemctl daemon-reload
run systemctl enable grok-station.service grok-station-wake.service grok-station-relay.service grok-station-admin.service
run systemctl restart grok-station-relay grok-station-wake grok-station-admin grok-station
# Post-install check: every service must be active and the admin page must answer on HTTPS.
if [ "$DRY" = 0 ]; then
  sleep 3
  for u in grok-station-relay grok-station-admin grok-station grok-station-wake; do
    i=0; until systemctl is-active -q $u || [ $i -ge 20 ]; do sleep 1; i=$((i+1)); done
    if systemctl is-active -q $u; then echo "ok   $u"; else
      echo "!!   $u is not running. Last log lines:"; journalctl -u $u -n 25 --no-pager; FAILED=1; fi
  done
  i=0; until curl -ks -o /dev/null --max-time 5 https://127.0.0.1/ || [ $i -ge 10 ]; do sleep 2; i=$((i+1)); done
  if curl -ks -o /dev/null --max-time 5 https://127.0.0.1/; then echo "ok   admin page answers on https://127.0.0.1"; else
    echo "!!   admin page does not answer on https://127.0.0.1. Last log lines:"; journalctl -u grok-station-admin -n 25 --no-pager; FAILED=1; fi
  [ -e /dev/fb0 ] || echo "!!   No framebuffer (/dev/fb0) yet. Reboot to load the display driver."
fi
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Done ($VER). The display and the admin page are running."
echo "Open https://$(hostname).local${IP:+ or https://$IP} on a phone or laptop and follow the station screen:"
echo "setup code, admin password, then your xAI API key. Voice starts once the key is saved."
if [ "$BOOT_CHANGED" = yes ]; then
  echo
  echo "*** Boot options changed (cmdline.txt). REBOOT NOW: sudo reboot"
  echo "*** The setup screen comes back after the reboot."
fi
echo "(Fallback without a browser: sudo grok-station set-key)"
[ "${FAILED:-0}" = 0 ] || { echo "Install finished with errors (see above)."; exit 1; }
