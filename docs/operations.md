# Install, update, and troubleshooting

Day-to-day setup for a station. What the installer installs, and which services run, is in [architecture.md](architecture.md). The short getting-started guide is the [README](../README.md).

## Image the card

In Raspberry Pi Imager, choose **Raspberry Pi OS Lite (64-bit)** and open the OS customisation settings:

- Set a **hostname** that is unique on your network (for example `<hostname>`). You'll use it to reach the admin page at `https://<hostname>.local`.
- Set up **Wi-Fi** (or use Ethernet).
- Turn on **SSH** and create a user.

Write the card, boot the Pi, and SSH in: `ssh <user>@<hostname>.local`.

## Install

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

What those stages install is listed in [architecture.md](architecture.md).

At the end of a fresh install it asks **Press Enter to reboot (Ctrl-C to skip)**. Without a terminal (or with `--no-reboot`, or `--dry-run`) it prints `Reboot to finish: sudo reboot` instead. After the reboot, open the addresses it printed and enter the code shown on the screen.

Updates (setup already done, or run by the admin page's Update button) restart the services in place, never prompt and never reboot. If an update changed the boot options, the Updates page says a reboot is needed.

## First-run setup in the browser

After the install, the station screen shows the addresses to open, `https://<hostname>.local` and `https://<IP>`, along with a 6-character setup code.

On a phone or laptop on the same network:

1. Open one of those addresses. The certificate is self-signed, so your browser shows a warning. Accept it to continue. (Port 80 only redirects to HTTPS.)
2. Enter the setup code from the station screen. It isn't case-sensitive and works once.
3. Set the admin password (at least 10 characters). There is no username: you sign in with the password only.
4. Paste your xAI API key. It is write-only: after saving, the Voice page then shows **Key set, ending ••••1234** with a **Replace key** button; the key itself is never shown.

Voice starts once the key is saved.

If the Pi has no network at boot, the screen shows a setup hotspot (a Wi-Fi name, a password, and a page to open). Join that Wi-Fi, open the page, and then join your home Wi-Fi. The hotspot turns off once the station is on your network, and the admin page moves with it.

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

Or use **Updates** on the admin page: **Check now** compares this station's version with `VERSION` on `main`, and **Update** installs a newer version (`grok-station-update.service`). It does not install an older `main`, and it does not ask for the admin password again. The page shows a spinner while that runs and the result when the admin page comes back. The full log stays in `journalctl -u grok-station-update` and `/var/log/grok-station-install.log`. The **Update** button only appears when a newer version is available, and History (collapsed by default) lists versions. If an update fails partway, the installer restarts every service so the station keeps running. The daily check only checks; it never installs. See [versioning.md](versioning.md).

**0.6.0** adds bot list import, saves switches immediately (other fields keep a Save button), shows an add-bot routine to paste into the bot along with this station's certificate fingerprint, and leaves mic gain alone until that slider is moved. It also pins the wake packages (openwakeword 0.6.0, onnxruntime 1.31.0, numpy 2.5.4) and adds the peak sample and clipped ratio (`clipped=0.0012`) to the mic level line. There is no hosted mailbox: Grok Bot on a home machine on the station's LAN posts replies. The key lives on the cloud computer (default) or, on a Mac, on that home machine (`gs-seal-reply`, one command, about 3 seconds faster; enabled in Add bot after its first validation run). A key exists on only one machine; switching mode revokes the pinned key. Add your main bot first. Notes are in [versioning.md](versioning.md).

## Troubleshooting

If a stage fails, the installer shows the failed stage, the last 20 log lines and the log path. The full log is `/var/log/grok-station-install.log`; re-run with `--verbose` to watch every command.

Service names and the `systemctl` / `journalctl` commands are in [architecture.md](architecture.md).

- **The admin page doesn't load:** check `grok-station-admin`, and try `https://<IP>` if `.local` names don't resolve on your network.
- **"No framebuffer (/dev/fb0)":** reboot so the display driver loads.
- **The screen stays bright at night:** open Display and set Brightness to Schedule, Sunrise, or Light sensor. Off leaves the backlight alone. With no backlight file, the station draws a dark layer instead. See [dimming.md](dimming.md).
- **The installer says "Needs 64-bit Raspberry Pi OS (aarch64)":** re-image with the 64-bit Lite image.
