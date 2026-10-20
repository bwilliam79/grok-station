#!/bin/sh
# Run on the Pi as root from the unpacked repo dir. Token goes in /etc/grok-station/env separately.
set -e
cd "$(dirname "$0")"
apt-get install -y --no-install-recommends plymouth plymouth-themes python3-pygame python3-qrcode libegl1 libegl-mesa0 libgles2 libgbm1 libgl1-mesa-dri
install -d /opt/grok-station
install -m755 app/grok_station.py /opt/grok-station/
install -m644 app/avatars.py /opt/grok-station/
install -d /opt/grok-station/assets/fonts /opt/grok-station/assets/portraits /opt/grok-station/assets/brand
install -m644 assets/fonts/* /opt/grok-station/assets/fonts/
if [ -n "$(find assets/portraits -name '*.png' -print -quit)" ]; then
  install -m644 assets/portraits/*.png /opt/grok-station/assets/portraits/
fi
rm -f /opt/grok-station/assets/portraits/grok.png
install -m644 assets/brand/*.png assets/brand/*.svg /opt/grok-station/assets/brand/
install -d /opt/grok-station/assets/avatars /opt/grok-station/assets/status
install -m644 assets/avatars/default-*.svg /opt/grok-station/assets/avatars/
install -m644 assets/status/*.svg /opt/grok-station/assets/status/
install -m644 systemd/grok-station.service /etc/systemd/system/
C=/boot/firmware/cmdline.txt
for o in quiet splash loglevel=3 vt.global_cursor_default=0 logo.nologo consoleblank=0 plymouth.ignore-serial-consoles; do
  grep -qw -- "$o" $C || sed -i "1s/\$/ $o/" $C
done
grep -q '^disable_splash=1' /boot/firmware/config.txt || echo 'disable_splash=1' >> /boot/firmware/config.txt
# Echo Plymouth theme. tools/install-splash.sh copies the theme to a temp dir, checks the files,
# then installs them. This line switches the default and rebuilds the initramfs.
# If the echo files are missing, Plymouth falls back to text. An older theme directory
# (for example /usr/share/plymouth/themes/grok) is left in place.
if sh tools/install-splash.sh "$PWD" --prepare; then
  plymouth-set-default-theme -R echo
else
  plymouth-set-default-theme -R text || true
fi
systemctl daemon-reload
systemctl mask getty@tty1.service
systemctl enable grok-station.service
# Admin web page (http://<hostname>.local): setup code on the screen, no default password. Needs the grok user (docs/wake.md).
id grok >/dev/null 2>&1 || useradd -r -m -s /usr/sbin/nologin grok
sh tools/install-files.sh "$PWD" "$(git rev-parse --short HEAD 2>/dev/null || echo dev)"
