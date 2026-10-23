# SD card image

`tools/build-image/build-image.sh` builds a card image that needs **no network on the Pi**. You write it, boot it, join the station's setup hotspot, and set up Wi-Fi there. Updates afterwards come through the Updates page, the same as a station installed with `bootstrap.sh`.

It is not a prebuilt download. The script fetches the official Raspberry Pi OS Lite (64-bit) image from downloads.raspberrypi.com (the list Raspberry Pi Imager uses), checks Raspberry Pi's published checksums, and then runs this repository's own `install.sh` inside the image.

The image has no SSH key, hostname, or password. It has no Wi-Fi profile unless you add one with `--wifi-from-env`.

## One install, two halves

There is one installer, `install.sh`. The image runs it twice:

| When | How | What runs |
|---|---|---|
| Build time, on your computer | `chroot` into the image (arm64; `qemu-user-static` on an Intel/AMD host) with `GROK_STATION_IMAGE_BUILD=1` | `apt-get install` of the station packages (including `git`), the `grok`/`grok-relay` users, the relay and wake-listener Python venvs (hash-locked pins), the wake models. Writes `/opt/grok-station/.image-build` with the VERSION |
| First boot, on the Pi, offline | `grok-station-firstboot.service` runs `GROK_STATION_OFFLINE=1 install.sh --no-reboot` | Everything else: this Pi's own secrets and station key, admin certificate, station files, boot screen and kiosk, Wi-Fi power save off, journal cap, services enabled, health check. It checks the marker instead of downloading |

Build mode skips only the steps that belong to one Pi or need its hardware: secrets (each card gets its own), the admin certificate, `systemctl` service setup, the boot command line, and the health check. Offline mode skips only the downloads. A normal install or update runs every step. `tests/test_sd_image.py` checks both modes with `install.sh --dry-run` and fails if the image starts doing install steps of its own.

The repository at `/usr/local/src/grok-station` is a real `git clone` of GitHub `main` at the commit you built, with `origin` set. That is the same layout `bootstrap.sh` makes, so the web updater works unchanged. Because of that, **push your commit before you build**; the build refuses a commit that is not on GitHub `main`.

The root filesystem is grown by 3 GB in the image file to fit the packages and venvs. Raspberry Pi OS still expands it to the full card on first boot.

## Build

### Linux

Debian or Ubuntu, Intel/AMD or arm64:

```bash
sudo apt-get install -y git curl xz-utils python3 e2fsprogs qemu-user-static binfmt-support
git clone https://github.com/bwilliam79/grok-station && cd grok-station
tools/build-image/build-image.sh --dry-run
sudo tools/build-image/build-image.sh --output "$HOME/grok-station.img"
```

On an arm64 Linux host `qemu-user-static` is not needed. The build needs root for `losetup`, `mount` and `chroot`. It attaches only the image file and never writes the computer's own disks. Expect it to take a while: the package and Python installs run under arm64 emulation on Intel/AMD.

The image builds on Linux only: a Linux PC or a Linux VM you already have. There are no macOS, Windows, Lima, WSL or Docker scripts.

## Options

- `--output PATH`: a new file (never under `/dev`, never overwritten).
- `--xz`: compress the finished image.
- `--wifi-from-env`: optional. Set `GS_WIFI_SSID`, `GS_WIFI_PSK` and `GS_WIFI_COUNTRY` (for example `US`) first. The values are never printed; they become a root-only NetworkManager profile, and first boot sets the Wi-Fi country. The image then holds that password, so don't share it. With saved Wi-Fi in range, the station joins it instead of starting the setup hotspot. Not needed: the hotspot is the normal way to set up Wi-Fi.
- `--dry-run`: print every step. No root, no network, nothing written.

## Write the card

Any whole-disk image tool works: Raspberry Pi Imager (**Use custom**; skip OS customisation, or set only a user), balenaEtcher, or `dd` (check the device with `lsblk` first; the wrong device is destroyed):

```bash
sudo dd if=grok-station.img of=/dev/sdX bs=4M conv=fsync status=progress
```

## First boot

No cable and no Wi-Fi are needed.

1. Put in the card. Connect the display and the USB speaker and microphone. Power on.
2. Boot 1: Raspberry Pi OS expands the card and reboots. If it asks for a username and password, that is Raspberry Pi OS; create one (a keyboard is needed then) or set one in the imager.
3. Boot 2: the screen shows "Getting Grok Station ready" and the step names while `install.sh` finishes offline. Then the Pi restarts. If something fails, the next boot tries again. Logs: `journalctl -u grok-station-firstboot` and `/var/log/grok-station-install.log`.
4. Boot 3: with no cable and no saved Wi-Fi, the station starts its setup hotspot and the screen shows the network name and passphrase. Join it from a phone, open the address on the screen, set up Wi-Fi, then finish setup (code, admin password, xAI key). With a cable plugged in, it shows the setup code instead.

Later updates: the Updates page, as on any station.

## Validation on the Pi

1. Build with `--dry-run` first and read the steps, then build for real.
2. Write the card and boot it with no cable and no Wi-Fi (First boot above).
3. Expected: "Getting Grok Station ready" on boot 2, then the setup hotspot on boot 3. Join it, set up Wi-Fi and finish setup.
4. On the Updates page, the station reports the version you built and can update from GitHub.
5. If boot 2 fails, read `journalctl -u grok-station-firstboot` and `/var/log/grok-station-install.log`. If the build fails in the chroot, its log is saved next to the output as `<output>.build.log`.

## How to audit

- Read `tools/build-image/build-image.sh`, `firstboot.sh` and the unit beside them, and the two mode switches near the top of `install.sh`.
- `build-image.sh --dry-run` prints every step: the OS-list URL, both `sha256sum -c` checks, the grow, `losetup`, the clone, the chroot `install.sh` run, and the unit paths.
- After a build, loop-mount the output and check `/usr/local/src/grok-station` (`git -C ... log -1` and `remote -v`), `/opt/grok-station/.image-build`, the first-boot unit and stamp, and that `/etc/grok-station/secrets` does not exist yet.
