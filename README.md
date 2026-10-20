# grok-station

A Raspberry Pi voice station. Everything runs on the Pi: the display, the "Hey Grok" wake listener, the admin page (HTTPS) and the voice relay (loopback only).

These are the installer instructions for the single-device build.

## Requirements

- Raspberry Pi 4 with 2 GB RAM or more. A Pi 5 is recommended. A Pi 3B works as a small-hardware test.
- 64-bit Raspberry Pi OS Lite (Trixie). The installer stops on anything that isn't 64-bit (aarch64).
- A microSD card of 16 GB or more.
- A USB microphone and a speaker.
- A display attached to the Pi.
- An xAI API key.

## 1. Image the card

In Raspberry Pi Imager, choose **Raspberry Pi OS Lite (64-bit)** and open the OS customisation settings:

- Set a **hostname** that is unique on your network (for example `<hostname>`). You'll use it to reach the admin page at `https://<hostname>.local`.
- Set up **Wi-Fi** (or use Ethernet).
- Turn on **SSH** and create a user.

Write the card, boot the Pi, and SSH in: `ssh <user>@<hostname>.local`.

## 2. Install

```bash
sudo apt-get install -y git   # if git isn't installed yet
git clone https://github.com/bwilliam79/grok-station && cd grok-station && git checkout grok/single-device-20261020-115607 && sudo ./install.sh
```

`sudo ./install.sh --dry-run` prints every step without changing anything.

### What the installer does

- Installs the system packages it needs with `apt-get`.
- Creates the service users and generates local secrets in `/etc/grok-station/secrets`. Secrets are never printed.
- Installs the display app, the voice relay and the Hey Grok wake listener (each in its own Python venv) under `/opt/grok-station`.
- Sets quiet boot options in `cmdline.txt`, including `vt.global_cursor_default=0`, which hides the blinking console cursor. It adds `disable_splash=1` to `config.txt`, sets up the boot splash, and turns off the login prompt on the screen (tty1).
- Installs the `grok-station` command and creates a self-signed certificate for the admin page.
- Enables and starts four systemd services: `grok-station`, `grok-station-wake`, `grok-station-relay` and `grok-station-admin`.
- Runs a health check. Each service must be active and the admin page must answer on `https://127.0.0.1`. If something fails, the installer prints its last log lines and exits with an error.

If the installer changed the boot options it tells you to reboot (`sudo reboot`). The setup screen comes back after the reboot.

## 3. First-run setup in the browser

After the install, the station screen shows the addresses to open, `https://<hostname>.local` and `https://<IP>`, along with a 6-character setup code.

On a phone or laptop on the same network:

1. Open one of those addresses. The certificate is self-signed, so your browser shows a warning. Accept it to continue. (Port 80 only redirects to HTTPS.)
2. Enter the setup code from the station screen. It isn't case-sensitive and works once.
3. Set the admin password (at least 10 characters).
4. Paste your xAI API key. It is write-only: after saving, only the last 4 characters are shown.

Voice starts once the key is saved.

### Fallback: set the key from the command line

If you can't use a browser, set the key on the Pi:

```bash
sudo grok-station set-key
```

## Updating

From the cloned repo on the Pi:

```bash
cd grok-station && git pull && sudo ./install.sh
```

The installer is safe to re-run. It keeps the existing secrets, admin password and key.

## Troubleshooting

| Service | What it runs |
|---|---|
| `grok-station` | The display |
| `grok-station-wake` | The Hey Grok wake listener |
| `grok-station-relay` | The voice relay (127.0.0.1 only) |
| `grok-station-admin` | The admin page (HTTPS on 443) |

```bash
systemctl status grok-station grok-station-wake grok-station-relay grok-station-admin
journalctl -u grok-station-admin -n 50 --no-pager      # swap in any service name
journalctl -u grok-station-wake -f                    # follow live
sudo systemctl restart grok-station-relay              # restart one service
```

- **The admin page doesn't load:** check `grok-station-admin`, and try `https://<IP>` if `.local` names don't resolve on your network.
- **"No framebuffer (/dev/fb0)":** reboot so the display driver loads.
- **The installer says "Needs 64-bit Raspberry Pi OS (aarch64)":** re-image with the 64-bit Lite image.
