# grok-station

grok-station is a voice station you keep at home. It runs on a Raspberry Pi, listens for "Hey Grok", and shows the answer on its own screen. You set it up from a phone or laptop on your home network.

## What it looks like

![The station waiting, with nothing playing](docs/images/idle.jpg)

![An answer on the station screen](docs/images/answer.jpg)

![A weather answer](docs/images/weather.jpg)

![Timers on the station](docs/images/timers.jpg)

![A reply from another bot](docs/images/other-bot.jpg)

## What you need

- A Raspberry Pi 4 with 2 GB of memory or more. A Pi 5 is the one to get. A Pi 3B is only for a small test.
- A display plugged into the Pi.
- An EMEET speakerphone. That is the USB microphone and speaker.
- A microSD card, 16 GB or larger.
- A power supply for the Pi.
- 64-bit Raspberry Pi OS Lite (Trixie).
- An xAI API key.

## Put it on the Pi

In Raspberry Pi Imager, choose **Raspberry Pi OS Lite (64-bit)**. Set a hostname, Wi-Fi (or use Ethernet), and SSH. Write the card, boot the Pi, and sign in.

You can instead build the card from this repository: see [docs/sd-image.md](docs/sd-image.md). That downloads the official Lite (64-bit) image and does not set Wi-Fi, SSH, a hostname, or a password.

Then run:

```bash
wget -qO- https://raw.githubusercontent.com/bwilliam79/grok-station/main/bootstrap.sh | sudo sh
```

That checks the system, downloads grok-station, and runs `install.sh`. It does not ask questions. If you already have the files:

```bash
git clone https://github.com/bwilliam79/grok-station && cd grok-station && sudo ./install.sh
```

When a fresh install finishes, it asks you to press Enter to reboot. After that, the screen shows where to open the admin page and a short code. The full install, update, and fix-it notes are in [docs/operations.md](docs/operations.md).

## First steps

1. **Wi-Fi.** If the Pi is not on a network, the screen shows a setup hotspot: a Wi-Fi name, a password, and a page to open. Join that Wi-Fi, open the page, and join your home Wi-Fi. The hotspot then turns off, and the page moves to your home network.
2. **Sign in.** Open the address on the screen (`https://<hostname>.local`, or the address with the IP). The certificate is self-signed, so the browser warns you; continue past that. Enter the code from the screen, then choose an admin password. There is no username.
3. **xAI key.** Paste your xAI API key and save it. The page will not show the key again. Voice starts once it is saved.
4. **Add your main bot first.** The bot needs Grok Bot running on a home machine on the same network. There is no hosted mailbox.

## More detail

- [What runs on the Pi](docs/architecture.md)
- [Install, update, and troubleshooting](docs/operations.md)
- [Connecting a bot](docs/bots.md)
- [Recording a wake word](docs/enroll.md)
- [Versions](docs/versioning.md)

Other notes in `docs/` cover the design, handoff between bots, and longer plans.

**0.8.0** ships the Mac home-mode scripts (`gs-seal-setup`, `gs-seal-reply`) with hash-locked PyNaCl. Add bot keeps offering cloud mode only until home mode's first validation run. See [docs/bots.md](docs/bots.md).

**0.6.0** adds bot list import, saves switches immediately (other fields keep a Save button), shows an add-bot routine to paste into the bot along with this station's certificate fingerprint, and leaves mic gain alone until that slider is moved. It also pins the wake packages (openwakeword 0.6.0, onnxruntime 1.31.0, numpy 2.5.4) and adds the peak sample and clipped ratio (`clipped=0.0012`) to the mic level line. There is no hosted mailbox: Grok Bot on a home machine on the station's LAN posts replies. The key lives on the cloud computer (default) or, on a Mac, on that home machine (`gs-seal-reply`, one command, about 3 seconds faster; enabled in Add bot after its first validation run). A key exists on only one machine; switching mode revokes the pinned key. Add your main bot first. Notes are in [docs/versioning.md](docs/versioning.md).

**Cards.** A paired bot can put a recipe, sports, calendar, or home card on the display. It uses the same sealed signature as a reply, on `POST /voice/show`. See [docs/cards.md](docs/cards.md).

Never route the admin HTTPS port (which also carries `/voice/reply` and `/voice/show`) through cloudflared, a tunnel, or a router port forward.

Locked out of the admin page (for example, your computer is on a different subnet or a VPN)? SSH to the Pi and forward the admin port over loopback, for example `ssh -L 8443:127.0.0.1:443 <user>@<pi-address>`, then open `https://127.0.0.1:8443`. Loopback is always allowed. There is no config allowlist to edit.
