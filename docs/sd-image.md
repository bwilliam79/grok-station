# SD card image

`tools/build-image/build-image.sh` builds a card image on a Linux computer. It is not a prebuilt grok-station image. The script downloads the official Raspberry Pi OS Lite (64-bit) image, checks the checksum Raspberry Pi publishes, and copies this repository onto that image. You write the result with any image tool.

Nothing in the image is an SSH key, a hostname, or a password. A Wi-Fi profile is added only with `--wifi-from-env` (see **Wi-Fi** below). The login account, if you want one, is created by Raspberry Pi OS on first boot, or by the imager you use to write the card. This script does not create it.

The copy is `git archive` of `HEAD`. Uncommitted files and `.git` are not included. On the Pi, a one-shot systemd unit runs `install.sh` and reboots. After that, the station services are the ones `install.sh` already enables. No new port is opened. With no cable and no saved Wi-Fi, the existing setup hotspot starts (`admin/grok_admin.py` `hotspot_loop`). That is the AP setup mode. This script does not start an access point of its own.

## Requirements

- A Linux computer. The script loop-mounts an ext4 filesystem. It does not run the image and does not need QEMU.
- `bash`, `curl`, `xz`, `sha256sum`, `git`, `python3`, and util-linux (`losetup`, `mount`, `umount`).
- Root for a real build. `losetup` attaches the image file as a block device, and `mount` attaches its root partition. Both are privileged. `--dry-run` does not need root and does not use the network.
- Several gigabytes free. The script keeps the download, the uncompressed image, and the output at the same time.
- A microSD card of 16 GB or more, as in the main install guide.

## Build on macOS, Windows, or Linux

`tools/build-image/docker-build.sh` (macOS, Linux, WSL) and `tools/build-image/docker-build.ps1` (Windows PowerShell) run `build-image.sh` inside a `debian:trixie` container with `--privileged`, because the build attaches the image file with `losetup`. The image is written to the repository folder. Docker Desktop has no partition devices for loop images, so the build attaches the root partition by its offset when `<loop>p2` does not appear. The offset path was tested on Linux without Docker; the Docker scripts themselves have not been run yet, on any OS.

```bash
tools/build-image/docker-build.sh grok-station.img            # macOS / Linux
```

```powershell
.\tools\build-image\docker-build.ps1 -Name grok-station.img   # Windows
```

## Wi-Fi

The card needs the internet on its second boot, because `install.sh` downloads packages and Python wheels. The setup hotspot belongs to the installed station, so it cannot carry that first install. Any network works: a cable, Wi-Fi set in Raspberry Pi Imager, or Wi-Fi added at build time:

```bash
export GS_WIFI_SSID=... GS_WIFI_PSK=... GS_WIFI_COUNTRY=US
sudo -E tools/build-image/build-image.sh --wifi-from-env --output "$HOME/grok-station.img"
```

The values are read from the environment, checked, and never printed. They are written as a root-only NetworkManager profile (`grok-station-wifi`) and a country file that first boot uses to unblock Wi-Fi. The password is inside the image file, so don't share the file. With saved Wi-Fi in range, the station joins it and does not start the setup hotspot.

## Build on Linux directly

From a clean checkout of this repository (the script refuses uncommitted changes, because the image is `git archive HEAD`):

```bash
tools/build-image/build-image.sh --dry-run
sudo tools/build-image/build-image.sh --output "$HOME/grok-station-raspios-lite-arm64.img"
sudo tools/build-image/build-image.sh --xz --output "$HOME/grok-station-raspios-lite-arm64.img.xz"
```

`--output` must be a new file. The script refuses a path under `/dev` and refuses to overwrite. `--xz` compresses the finished image. The default name in the current directory is `grok-station-raspios-lite-arm64.img` (or `.img.xz` with `--xz`).

The work directory is removed when the script exits, including after a failed checksum, so a bad download is not left behind. `*.img` and `*.img.xz` are gitignored. Do not commit a card image.

## Steps

The script prints the same step numbers in `--dry-run` and in a real build.

1. **Fetch the official OS list.** The URL is `https://downloads.raspberrypi.com/os_list_imagingutility_v4.json`, the list Raspberry Pi Imager uses. No checksum is stored in this repository. Downloads are https only, with a size ceiling, and a redirect is refused unless the final URL is still on `downloads.raspberrypi.com`.

2. **Select Raspberry Pi OS Lite (64-bit).** The entry's name contains `Lite` and `64-bit`, and its URL is under `https://downloads.raspberrypi.com/raspios_lite_arm64/images/<release>/<file>.img.xz`. The file name contains `lite` and `arm64`. The entry must publish `extract_sha256`. The newest `release_date` wins. Two different images with the same date stop the script. Desktop, 32-bit, Legacy (bookworm), and any other host are ignored.

3. **Download the `.img.xz`.** The ceiling is 2 GB. If the list publishes `image_download_size`, the file must be that size.

4. **Verify the download.** The hash is `<image>.img.xz.sha256`, published beside the file, and `image_download_sha256` from the OS list when the list has it (the current list often does not). When both exist they must be the same; when neither exists the build stops. The script then runs `sha256sum -c`. A mismatch stops the build before anything is mounted.

5. **Decompress.** `xz --robot --list` must show an uncompressed size of at most 8 GB, and that size must match `extract_size` when the list publishes one.

6. **Verify the `.img`.** `sha256sum -c` against `extract_sha256` from the same OS list.

7. **Loop-mount the root.** `losetup --find --show --partscan` attaches only this image file. Partition 1 is the FAT boot firmware; it is not mounted and not modified. Partition 2 is the ext4 root and is the only mount. The mount must contain `/etc/rpi-issue` or `/usr/lib/raspberrypi-sys-mods`, or the script stops. Root is required for this step. It does not scan or write the computer's own disks.

8. **Copy grok-station.** `git archive --format=tar HEAD` is extracted to `/usr/local/src/grok-station` and owned by root. The archive is refused if it lists `.git`, `.env`, `id_rsa`, `id_ed25519`, `.ssh/`, a `.pem`, `credentials.json`, `secrets.json`, or `.netrc`. A sibling file `/usr/local/src/grok-station-image-commit` records the commit and the `VERSION` that were copied.

9. **Install the first-boot unit.** The unit file is taken from the archive (so it matches `HEAD`) and installed as `/etc/systemd/system/grok-station-firstboot.service`. A symlink under `multi-user.target.wants` enables it. An empty stamp `/var/lib/grok-station-firstboot` is created. `install.sh` on a normal checkout does not enable this unit.

10. **Write the output.** The loop device is detached first. With `--xz`, the output is compressed; otherwise the `.img` is moved into place.

## First boot

Raspberry Pi OS expands the card on its own first boot and reboots. This script does not change that, because it does not edit the boot partition.

On the next boot, `userconfig.service` (Raspberry Pi OS) may ask on the console for a username and password. The first-boot unit is ordered after that service when it exists. This repository does not write the account.

The unit then runs `tools/build-image/firstboot.sh`, which runs `install.sh --no-reboot`. The screen shows "Installing Grok Station" and the plain step names; details go to the log. First boot waits up to 10 minutes for any network (see **Wi-Fi**) and exits if there is none, so the next boot tries again. `GROK_STATION_NONINTERACTIVE` is left unset on purpose. That variable selects update mode and would start the services immediately. Fresh mode enables them and leaves them stopped until reboot.

If `install.sh` fails, the stamp stays and the unit runs again on the next boot. If it succeeds, the script turns `/usr/local/src/grok-station` into a git clone of the GitHub repository (installing `git` first, since Lite does not include it; fetch `main`, then point at the image's own commit), so the Updates page can update the station later. If that fetch fails the station still works, but in-app updates need a clone from `bootstrap.sh`. It then removes the stamp and the enable symlink, then reboots.

The boot after that starts the station. The screen shows the setup code. Leave the cable in to open the admin page from another device on that network. Unplug the cable and reboot to use the setup hotspot: with no wired link and no saved Wi-Fi, `hotspot_loop` starts the access point and the display shows that hotspot screen. Join the network named on the screen and open the address shown there.

`install.sh` masks the login prompt on tty1 for the kiosk. Create the local account at the Raspberry Pi OS prompt before that, or skip a shell account and use the admin page. SSH is not turned on.

Logs: `journalctl -u grok-station-firstboot` and `/var/log/grok-station-install.log`.

## How to audit

- Read `tools/build-image/build-image.sh`. It is the whole build. The first-boot unit and `firstboot.sh` are next to it.
- Run `tools/build-image/build-image.sh --dry-run` and read every line. It prints the OS-list URL, the `sha256sum -c` checks, `losetup`, `git archive`, and the unit paths. It does not download or mount.
- Save the OS list yourself (`curl` the URL in step 1) and run `tools/build-image/build-image.sh --select-only os-list.json`. Confirm the printed URL is Lite arm64 on `downloads.raspberrypi.com`.
- `tools/build-image/build-image.sh --check-image-url URL` exits 0 only for an allowlisted Lite arm64 image URL.
- After a real build, loop-mount the output yourself and compare `/usr/local/src/grok-station` with `git archive` of the commit in `/usr/local/src/grok-station-image-commit`. Confirm the stamp, the unit, and the enable symlink. Confirm the boot partition was not changed and that no Wi-Fi profile, SSH key, hostname, or password file was added.

## How to write the card

Use any tool that writes a whole-disk image. Wi-Fi in the imager is fine; the station then uses that network instead of its setup hotspot. Leave the hostname and SSH keys unset. A username and password in the imager are optional; they only create the local account Raspberry Pi OS would otherwise ask for.

- **Raspberry Pi Imager:** choose **Use custom** and select the `.img` or `.img.xz`. Skip OS customisation, or set a user only.
- **balenaEtcher:** select the image file, select the card, and flash.
- **dd:** identify the card with `lsblk` first. The example device below is a placeholder.

```bash
sudo dd if=grok-station-raspios-lite-arm64.img of=/dev/sdX bs=4M conv=fsync status=progress
```

Writing the wrong device destroys that disk.

## Same install as a normal setup

The image does not copy install steps. First boot runs the repository's own `install.sh`, the same file `bootstrap.sh` runs, so every install fix (packages including `git`, users and secrets, Wi-Fi power save off, journal cap, display, relay, wake listener and mic gain, boot splash, admin page and update unit, health check) applies to an image-built station the same way. `tests/test_sd_image.py` (`ImageInstallParity`) fails if first boot or the build script starts doing install work of its own, or if `bootstrap.sh` installs a package `install.sh` does not.

## Validation on the Pi

What changed: a card written from this image contains the grok-station tree at `/usr/local/src/grok-station`, the commit file beside it, and `grok-station-firstboot.service`. No Wi-Fi profile, SSH key, hostname, or password is added. The station services and ports are only those `install.sh` already sets up.

How to test:

1. Build with `sudo tools/build-image/build-image.sh` (add `--xz` for a compressed file). `--dry-run` on the build machine must print the steps and create no file.
2. Write the image as above. Do not set Wi-Fi.
3. Insert the card. Connect Ethernet and a display. Connect a keyboard if you did not set a user in the imager. Power on.
4. The first boot is Raspberry Pi OS expanding the card. It reboots by itself.
5. If the console asks for a username and password, that is Raspberry Pi OS, not this project. Create the account or use one the imager already set.
6. The next boot runs `install.sh`. The HDMI console shows its stages. It then reboots again. If the cable was unplugged, install fails and the stamp remains; reconnect Ethernet and reboot to retry.
7. Expected with the cable still in: the station screen shows the setup code. From another device on that network, open the admin page shown on the screen and finish setup (code, admin password, xAI key).
8. Unplug Ethernet and reboot. Expected: the screen switches to the setup hotspot (name and passphrase generated on the Pi, shown on the screen). Join that network from a phone and open the address on the screen. No extra service is listening beyond the admin page and the other station services.

The image commit on the Pi (`/usr/local/src/grok-station-image-commit`) matches the commit the build script printed.
