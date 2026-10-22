# grok-station

A Raspberry Pi voice station. Everything runs on the Pi: the display, the "Hey Grok" wake listener, the admin page (HTTPS) and the voice relay (loopback only). Connecting a bot is in [docs/bots.md](docs/bots.md).

This README covers installation.

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

On the Pi, run:

```bash
wget -qO- https://raw.githubusercontent.com/bwilliam79/grok-station/main/bootstrap.sh | sudo sh
```

`bootstrap.sh` checks that the system is 64-bit, installs `git` if it's missing, clones the repo into `/usr/local/src/grok-station` (or updates an existing clone at `~/grok-station`), and then runs `install.sh`. It never prompts.

**Alternative (manual clone):**

```bash
sudo apt-get install -y git   # if git isn't installed yet
git clone https://github.com/bwilliam79/grok-station && cd grok-station && sudo ./install.sh
```

The installer prints one line per stage with a check or a cross. Everything else goes to `/var/log/grok-station-install.log`. Options: `--verbose` (`-v`) streams every command's output, and `--dry-run` shows what would run without changing anything (with the one-liner: `... | sudo sh -s -- --verbose`).

### What the installer does

- Installs the system packages it needs with `apt-get`.
- Creates the service users and generates local secrets in `/etc/grok-station/secrets`. Secrets are never printed.
- Installs the display app, the voice relay and the Hey Grok wake listener (the relay and the wake listener each in their own Python venv) under `/opt/grok-station`.
- Sets quiet boot options in `cmdline.txt`, including `vt.global_cursor_default=0`, which hides the blinking console cursor. It adds `disable_splash=1` to `config.txt`, sets up the boot splash, and turns off the login prompt on the screen (tty1).
- Installs the `grok-station` command and creates a self-signed certificate for the admin page.
- Enables four systemd services: `grok-station`, `grok-station-wake`, `grok-station-relay` and `grok-station-admin`. On a fresh install they are not started; they start at the reboot.
- Runs a static health check: unit files enabled, config and tokens present, venvs and Python imports working. If something fails, the installer prints its last log lines and exits with an error.

At the end of a fresh install it asks **Press Enter to reboot (Ctrl-C to skip)**. Without a terminal (or with `--no-reboot`, or `--dry-run`) it prints `Reboot to finish: sudo reboot` instead. After the reboot, open the addresses it printed and enter the code shown on the screen.

Updates (setup already done, or run by the admin page's Update button) restart the services in place, never prompt and never reboot. If an update changed the boot options, the Updates page says a reboot is needed.

## 3. First-run setup in the browser

After the install, the station screen shows the addresses to open, `https://<hostname>.local` and `https://<IP>`, along with a 6-character setup code.

On a phone or laptop on the same network:

1. Open one of those addresses. The certificate is self-signed, so your browser shows a warning. Accept it to continue. (Port 80 only redirects to HTTPS.)
2. Enter the setup code from the station screen. It isn't case-sensitive and works once.
3. Set the admin password (at least 10 characters). There is no username: you sign in with the password only.
4. Paste your xAI API key. It is write-only: after saving, the Voice page then shows **Key set, ending ••••1234** with a **Replace key** button; the key itself is never shown.

Voice starts once the key is saved.

### Fallback: set the key from the command line

If you can't use a browser, set the key on the Pi:

```bash
sudo grok-station set-key
```

## Updating

Run the same one-liner again:

```bash
wget -qO- https://raw.githubusercontent.com/bwilliam79/grok-station/main/bootstrap.sh | sudo sh
```

Or, from the cloned repo: `git pull && sudo ./install.sh`. Both are safe to re-run and keep the existing secrets, admin password and key.

Or use **Updates** on the admin page: **Check now** compares this station's version with `VERSION` on `main`, and **Update** installs a newer version (`grok-station-update.service`). It does not install an older `main`, and it does not ask for the admin password again. The page shows a spinner while that runs and the result when the admin page comes back. The full log stays in `journalctl -u grok-station-update` and `/var/log/grok-station-install.log`. The **Update** button only appears when a newer version is available, and History (collapsed by default) lists versions. If an update fails partway, the installer restarts every service so the station keeps running. The daily check only checks; it never installs. See [docs/versioning.md](docs/versioning.md).

**Bots** on the admin page adds a bot in steps (name and ID, key, webhook, then a summary). Voice is optional: leave it on **Station default** and change it later on the bot's row. The admin password is asked once, on the summary, and not again during the grace period (**System**, default 10 minutes, 0 always asks; signing out ends it). The relay icon is always on the station display. Until the first bot is paired it shows dim grey with a broken link; after that it is green, or red (crossed out) only after the last bot request timed out or its webhook failed.

## Troubleshooting

If a stage fails, the installer shows the failed stage, the last 20 log lines and the log path. The full log is `/var/log/grok-station-install.log`; re-run with `--verbose` to watch every command.

| Service | What it runs |
|---|---|
| `grok-station` | The display |
| `grok-station-wake` | The Hey Grok wake listener |
| `grok-station-relay` | The voice relay (127.0.0.1 only) |
| `grok-station-admin` | The admin page (HTTPS on 443) |
| `grok-station-update` | One-off update started from the admin page (`journalctl -u grok-station-update`) |

```bash
systemctl status grok-station grok-station-wake grok-station-relay grok-station-admin
journalctl -u grok-station-admin -n 50 --no-pager      # swap in any service name
journalctl -u grok-station-wake -f                    # follow live
sudo systemctl restart grok-station-relay              # restart one service
```

- **The admin page doesn't load:** check `grok-station-admin`, and try `https://<IP>` if `.local` names don't resolve on your network.
- **"No framebuffer (/dev/fb0)":** reboot so the display driver loads.
- **The installer says "Needs 64-bit Raspberry Pi OS (aarch64)":** re-image with the 64-bit Lite image.
