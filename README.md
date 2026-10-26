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
- A display plugged into the Pi, from 800x480 up to 4K (3840x2160). See **Screens** below.
- Any USB speaker and microphone, or a combined USB speakerphone.
- A microSD card, 16 GB or larger.
- A power supply for the Pi.
- 64-bit Raspberry Pi OS Lite (Trixie).
- An xAI API key.

### Screens

The station draws straight to the Pi's screen, in landscape. Sizes from 800x480 to 3840x2160 fill the whole screen, and the layout scales to fit. Bigger screens get a centered 3840x2160 picture with black borders. Smaller ones still work, but things may not fit. The automated tests check layouts at 800x480, 1024x600, 1280x720, 1280x800, 1920x1080, 2560x1706 and 3840x2160. Not every size has been tried on a real screen. Portrait (rotated) screens are untested. Touch isn't used, so a touchscreen works as a plain screen. Details are in [docs/display.md](docs/display.md).

## Put it on the Pi

In Raspberry Pi Imager, choose **Raspberry Pi OS Lite (64-bit)**. Set a hostname, Wi-Fi (or use Ethernet), and SSH. Write the card, boot the Pi, and sign in.

Then run:

```bash
wget -qO- https://raw.githubusercontent.com/bwilliam79/grok-station/main/bootstrap.sh | sudo sh
```

That checks the system, downloads grok-station, and runs `install.sh`. It does not ask questions. If you already have the files:

```bash
git clone https://github.com/bwilliam79/grok-station && cd grok-station && sudo ./install.sh
```

When a fresh install finishes, it asks you to press Enter to reboot. After that, the screen shows where to open the admin page and a short code. The full install, update, and fix-it notes are in [docs/operations.md](docs/operations.md).


### Or build a ready-to-boot card

`tools/build-image/build-image.sh` makes a card that needs **no network on the Pi**: boot it, join the station's setup hotspot, and set up Wi-Fi there. It runs this repository's own `install.sh` inside the image at build time (packages, Python, wake models), and the rest of `install.sh` offline on first boot, so it is the same install as `bootstrap.sh`. Updates come through the Updates page as usual. Full details and the per-OS steps: [docs/sd-image.md](docs/sd-image.md).

1. Push the commit you want. On Linux: `sudo apt-get install -y git curl xz-utils python3 e2fsprogs qemu-user-static binfmt-support`, then `sudo tools/build-image/build-image.sh --output "$HOME/grok-station.img"`. The image builds on Linux only (a Linux PC or a Linux VM you already have); no Lima, WSL or Docker.
2. Write the image with Raspberry Pi Imager (**Use custom**), balenaEtcher or `dd`.
3. Put in the card, connect the display and the USB speaker and microphone, and power on. No cable is needed.
4. The Pi expands the card and restarts, then shows "Getting Grok Station ready" and restarts again.
5. The screen shows the setup hotspot. Join it from a phone, open the address shown, set up Wi-Fi, and finish setup.

## First steps

1. **Wi-Fi.** If the Pi is not on a network, the screen shows a setup hotspot: a Wi-Fi name, a password, and a page to open. Join that Wi-Fi, open the page, and join your home Wi-Fi. The hotspot then turns off, and the page moves to your home network.
2. **Sign in.** Open the address on the screen (`https://<hostname>.local`, or the address with the IP). The certificate is self-signed, so the browser warns you; continue past that. Enter the code from the screen, then choose an admin password. There is no username.
3. **xAI key.** Paste your xAI API key and save it. The page will not show the key again. Voice starts once it is saved.
4. **Add your main bot first.** A computer on your home network must be running the Grok Bot app, and be on and reachable 24/7, for replies to reach the station.

## More detail

- [What runs on the Pi](docs/architecture.md)
- [Install, update, and troubleshooting](docs/operations.md)
- [Connecting a bot](docs/bots.md)
- [Recording a wake word](docs/enroll.md)
- [Versions](docs/versioning.md)

Other notes in `docs/` cover the design, handoff between bots, and longer plans.

**0.11.8** Finish update. After a station update, an amber Finish update button (page header and Updates page) opens one dialog that lists each bot by computer and updates them in one click: each bot runs one pinned update script that checks the commit is on the project's main branch, refuses an older or same release, checks every file's sha256 against the release manifest, keeps keys and config, and only accepts instructions that match the fixed template. A row turns green only when the bot's signed reply names the exact instructions the dialog showed; red means the bot refused. Bots set up before 0.11.8 need one last paste (Copy in the dialog). The first bot on a home computer updates the shared helper for the rest (Use another bot picks a different one). One ask per bot every 10 minutes. The Bots-row Update helper button is gone. See [docs/bots.md](docs/bots.md). Also in 0.11.8: adding a bot runs the same connection test as Test; the row shows Checking connection… until it answers, then Online, or Offline with the reason, and a passing test quietly asks the bot for its picture, which swaps in when it arrives. A bot's own wake word (Hey Kai) goes straight to that bot with no Asking announcement. Bots whose key is on a home computer can now put display cards (score, recipe, calendar) on the screen with `gs-seal-reply show`; before, their instructions said to skip cards, so a score came back as speech only. The home helper is now 0.11.8; Finish update installs it. **0.11.7** Bots that reply from a home computer show Helper out of date when their station helper is behind, with an Update helper button (the bot runs the station's pinned installer in a new update mode that keeps its key and pairing; a copyable command is the fallback). A deleted wake word no longer comes back a minute later. The setup wizard no longer has a Wake words step; set wake words per bot on the Bots page. **0.11.6** Ask bot for picture now waits up to about 2 minutes and says whether a new picture arrived, the bot didn't send one (its station helper may be out of date), or the bot couldn't be reached. The Configure tab row no longer shows a stray vertical scrollbar. **0.11.5** After an update the admin page waits until the station answers again (up to about 90 seconds, showing Reconnecting…) before it reloads, and a page that loaded while the station was down redraws itself once it is back, unless you are typing in a field or have a panel open. **0.11.4** Configure is four tabs (Wake word, Voice and photo, Connection, More) with one main button per wake-word state, the webhook fields under Edit connection, and Delete bot last under More. Bots send their own profile picture: the routines say to send it once after pairing, and Ask bot for picture asks again. Deleting a wake word removes every recording, its model, and its row, and deleting one that is already gone still succeeds. See [bots.md](docs/bots.md) and [enroll.md](docs/enroll.md). **0.11.3** Wake word and Display fixes. Train more leaves the not-good-enough screen as soon as it starts, recording shows only the per-recording check marks, the training dialog has one Close, and the pass line is 80% of the test clips ("It needs 24" for 30). The recording count skips empty, unfinished, and duplicate sessions. Display fits one desktop screen: settings on the left, both previews on the right. A saved town or ZIP shows in the accent color with a pin. Hey Grok uses the station logo. See [enroll.md](docs/enroll.md) and [display.md](docs/display.md). **0.11.2** Admin layout. The security certificate shows SHA-256, the fingerprint, and copy on one row. On Bots, Revoke all bots sits next to Add bot, and an empty list is the line "No other bots yet. Hey Grok always works." Audio is tighter, and Play test sound sits next to the speaker. Display is grouped into Clock and night clock, Screens, Brightness, and Units, with team logo sources at the bottom. Night clock hours and the dim window stay separate: the hours are the black clock when Brightness is Off, and Dim the screen from/until is when Schedule lowers the backlight. A saved town or ZIP shows as text with Change location. See [docs/display.md](docs/display.md) and [docs/bots.md](docs/bots.md). **0.11.1** The xAI key lives on the xAI API page under System (the old Voice address opens it). A bot added again with the same id reclaims its wake word, and a word whose bot is gone can be deleted under Unused wake words. "Hey Grok, import the rest of my team" asks the main bot for names; those names show as Not set up on Bots, and Copy team setup message copies one note for the main bot to send. The station sends nothing. The computer name is only the label from the Grok Bot app. See [docs/bots.md](docs/bots.md). **0.11.0** Home mode runs on macOS and Linux. Alpine (musl) is not supported. On Linux the key folder is tied to this computer's machine id, and it is marked with a cache-directory tag plus an empty `.nobackup` file so backup tools that honor those skip it. btrfs and snapper snapshots are not skipped. Python 3.14 is supported (cffi 2.0.0). Setup no longer needs pip. See [docs/bots.md](docs/bots.md). **0.10.3** Wake words live on the Bots page (the Wake words item is gone; #wake opens Bots). Hey Grok stays built in and always on. Configure sets a bot's wake word, trains more, or removes it; a removed bot's word stays under Unused wake words. Change computer asks for a name when the key is on the bot computer, and a home-computer key needs Replace key (a home-key bot that still has no computer name can set it). "Hey Grok, can you connect me with Kai" (also "connect me to" and "put me through to") is handled on the station: it says it is connecting you, switches the session to that bot, and the bot's first line is "Hey, this is Kai. How can I help you?" Follow-ups go to that bot until the session ends. See [docs/bots.md](docs/bots.md) and [docs/enroll.md](docs/enroll.md). **0.10.2** Bots show which home computer sends their replies ("Replies from: <name>"). Add bot asks your bot to pick the always-on computer and send its name back. When a reply doesn't come, the station says that computer may be offline. Configure has Change computer. See [docs/bots.md](docs/bots.md). **0.10.1** Fixes recording a wake word. A word recorded for a bot that had an old switched-off or deleted entry (like Hey Ash or Hey Rhea) was built but stayed off; recording it now turns it on. The recording dialog closes with Ready when the word is built, and shows a plain message if building fails or the station restarts partway. Unfinished words show in the list with Record voice and Delete. Building uses less memory. **0.10.0** Custom wake words work on the station itself: record a phrase 10 times on the Wake words page and the station builds it from those recordings, with no training computer. If it isn't reliable enough yet, the page says so and Train more records 10 more. Each word starts its own bot. Uploading a model trained on another computer still works as an advanced option. See [docs/enroll.md](docs/enroll.md). **0.9.4** Hey Grok is the only shipped wake word; updating removes the old starter models (custom trained or uploaded words are kept). Reach any bot with "Hey Grok, ask <bot>". **0.9.3** The card image builds on Linux only (a Linux PC or a Linux VM); the macOS (Lima) and Windows (WSL) build scripts are removed. **0.9.2** The card image needs no network on the Pi: install.sh runs inside the image at build time and finishes offline on first boot, and Wi-Fi is set up through the setup hotspot. Native build scripts for Linux, macOS (Lima) and Windows (WSL). The main bot has a gold star and is listed first. Wake words: every shipped model was installed (removed again in 0.9.4), training is a fixed 10 recordings per session with Train more, and wake words can be deleted. Team logos default to ESPN's logo CDN (a.espncdn.com). Bots can put display cards on the screen, and Configure has Copy updated instructions for bots already set up. **0.9.1** The card image builds again, needs no Ethernet (Imager or build-time Wi-Fi works), builds on macOS, Windows or Linux through Docker, runs the same `install.sh` as a normal setup, and shows "Installing Grok Station" on screen. See [docs/sd-image.md](docs/sd-image.md). **0.9.0** Add bot instructions include a one-time deploy step, "Deploy bot key setup on system running Grok Bot": the script installs the pinned programs on your own computer and on the home computer running Grok Bot when the key lives on the bot computer, or on the home computer when the key lives there. It asks before it runs. The station never pushes code or asks for those files again. After it runs, reply with the public key it printed, never the private key or the seed. Changing the key later means pairing again with Replace key. See [docs/bots.md](docs/bots.md). **0.8.5** "Bot computer" loses "(default)", and the home-computer speed note joins the 24/7 note under the choice (Add bot and bot settings). **0.8.4** plainer Add bot: the key step only asks where the key lives; the webhook step is required and collects the webhook URL, webhook key and bot public key; the instructions sit behind View instructions and Copy instructions; station key, fingerprint and technical notes are gone from the flow; README lists any USB speaker and microphone and the supported screen sizes (docs/display.md). **0.8.3** plainer Add bot key step (Bot computer or Home computer, one note on what must stay on) and a shorter setup prompt. **0.8.2** turns on home mode in Add bot as an opt-in choice; cloud stays the default. **0.8.0** ships the Mac home-mode scripts (`gs-seal-setup`, `gs-seal-reply`) with hash-locked PyNaCl. Add bot keeps offering cloud mode only until home mode's first validation run. See [docs/bots.md](docs/bots.md).

**0.6.0** adds bot list import, saves switches immediately (other fields keep a Save button), shows an add-bot routine to paste into the bot along with this station's certificate fingerprint, and leaves mic gain alone until that slider is moved. It also pins the wake packages (openwakeword 0.6.0, onnxruntime 1.31.0, numpy 2.5.4) and adds the peak sample and clipped ratio (`clipped=0.0012`) to the mic level line. Grok Bot on a home computer on the station's network posts replies. The key lives on the cloud computer (default) or, on a Mac, on that home machine (`gs-seal-reply`, one command, about 3 seconds faster; enabled in Add bot after its first validation run). A key exists on only one machine; switching mode revokes the pinned key. Add your main bot first. Notes are in [docs/versioning.md](docs/versioning.md).

**Cards.** A paired bot can put a recipe, sports, calendar, or home card on the display. It uses the same sealed signature as a reply, on `POST /voice/show`. See [docs/cards.md](docs/cards.md).

Never route the admin HTTPS port (which also carries `/voice/reply` and `/voice/show`) through cloudflared, a tunnel, or a router port forward.

Locked out of the admin page (for example, your computer is on a different subnet or a VPN)? SSH to the Pi and forward the admin port over loopback, for example `ssh -L 8443:127.0.0.1:443 <user>@<pi-address>`, then open `https://127.0.0.1:8443`. Loopback is always allowed. There is no config allowlist to edit.
