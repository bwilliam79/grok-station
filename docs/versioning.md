# Versions

`VERSION` at the repo root is the station's version, `MAJOR.MINOR.PATCH`.

- **Patch** — fixes only. Safe to install over the previous version.
- **Minor** — new features. Still installs over the previous version.
- **Major** — breaking changes. Expect a reinstall, not a quiet update.

Bump `VERSION` in every change that ships to `main`. The number only goes up.

Before shipping, run:

```bash
python3 tools/check-version.py
```

It fails unless `HEAD`'s `VERSION` is strictly newer than `HEAD~1`'s. The first commit that adds `VERSION` passes. The updater also refuses to install `origin/main` when that version is equal to or lower than the one already on the station (equal means up to date). A station whose installed version is still a short commit id can move onto the first semantic version.

The installed copy is `/opt/grok-station/VERSION`, written by the installer from this file. The Updates page compares it with `origin/main`'s `VERSION` after a fetch. A short commit id may be shown beside the version as a detail. The installer log stays on the station; the page shows one line if an update fails.

## 0.2.0

Minor feature: a cached "Asking <name>…" clip plays as soon as a handoff is known. See [handoff.md](handoff.md).

## 0.3.0

Minor feature: named station timers (up to eight, countdown rings, an amber ringing screen). See [timers.md](timers.md).

## 0.4.0

Minor: the display can dim from a light sensor, a clock schedule, or sunrise and sunset computed on the station. Settings are on the Display page. See [dimming.md](dimming.md).

## 0.5.0

Minor: `tools/build-image/build-image.sh` builds an SD image from the official Raspberry Pi OS Lite (64-bit) image. See [sd-image.md](sd-image.md).

## 0.6.0

Minor release. Installs over 0.5.0.

- **Bot list import.** The station can ask the list-source bot for names only. On Bots, each name shows as New, Already set up, or Not on the last list. Set up opens Add bot with the name filled in. A list never changes a bot that is already configured, and nothing in it is run.
- **Save consistency.** Switches apply as soon as you click them. Text, number, and select fields keep a Save button, which stays off until the field differs from what is saved. After a successful save the page shows "All changes saved".
- **Add bot.** The webhook step shows a routine to paste into the bot (the same text as in [bots.md](bots.md)), with the station address and certificate fingerprint filled in. The fingerprint is also on the public-key step, with a copy button, so you do not leave the dialog.
- **Audio gain.** The mic boost sliders show the gain the listener actually uses for the saved device (2 for an EMEET speakerphone, 4 otherwise; voice follows wake when voice has no line of its own). Save writes a gain only when that slider was moved.
- **Wake dependencies.** The wake virtualenv pins openwakeword 0.6.0 (still with `--no-deps`), onnxruntime 1.31.0, and numpy 2.5.4.
- **Mic level.** The periodic level line also reports the peak sample in that window and the clipped ratio, the fraction of samples at full scale (`clipped=0.0012`).
- **Replies come from home.** Replies are posted by Grok Bot running on a home machine on the station's LAN.
- **Where the key lives.** Cloud is the default: the bot's cloud computer seals, and the home machine posts. Home mode (macOS) generates the key, seals, and posts in one command on that home machine, about 3 seconds faster. A key exists on only one machine. Switching the mode revokes the pinned key and asks for a new public key.
- **Main bot first.** Add your main bot first. The add-bot dialog says so on the first step.

## 0.7.0

Minor: display cards (recipe, sports, calendar, home) on the signed show path. The reply tool still posts a reply when no card is given. See [cards.md](cards.md).

## 0.8.0

Minor release. Installs over 0.7.0.

- **Home key mode scripts.** `scripts/gs-seal-setup` (once: `vendor-install`, `keygen`, `init`) and `scripts/gs-seal-reply` (one `reply` command) seal and post a reply from a Mac home machine. macOS only, bound to that Mac's hardware UUID, key folder excluded from Time Machine and refused inside git, iCloud, CloudStorage, Dropbox or OneDrive. One pinned POST per request id, no retry.
- **Trusted imports.** Before importing anything, both scripts check the script, its helper and every parent folder, and every standard library folder they put on the path (owned by you or root, no group or world write, no symlinks). PyNaCl comes from a hash-locked vendor folder (`scripts/vendor-lock.txt`, `--require-hashes --only-binary :all:`), checked against a SHA-256 manifest on every run.
- **Not enabled yet.** The Add bot flow still offers cloud mode only. Home mode will be enabled after its first validation run.


## 0.8.1

Patch release. Installs over 0.8.0.

- **Symlinked call path refused.** `gs-seal-setup` and `gs-seal-reply` now check the path they were invoked by, before resolving it: if that path or any parent folder is a symlink, they print `refused` and exit 1. Run them from the real install directory. Home mode stays off in Add bot.

## 0.8.2

Patch release. Installs over 0.8.1.

- **Home mode available.** After its validation run on a Mac, Add bot (and a bot's key mode) now offers **Home machine** as an opt-in choice. **Cloud computer** stays the default and is pre-selected. No script or station changes.

## 0.8.11

Patch release. Installs over 0.8.10.

- **Main bot.** The List source field is gone. The main bot has a star on the Bots page, and Configure has **Set as Main Bot**. The first bot you add becomes the main bot. Removing it clears it, or picks the only bot left. With no main bot, Grok says you can set one on the Bots page.

## 0.8.10

Patch release. Installs over 0.8.9.

- **No "Asking" on goodbye.** After a handoff, the station waits for your words before saying "Asking <name>…". Saying goodbye now just ends the session.

## 0.8.5

Patch release. Installs over 0.8.4.

- **Key choice wording.** "Bot computer" loses "(default)", and the home-computer speed note joins the 24/7 note under the choice (Add bot and bot settings).

## 0.8.4

Patch release. Installs over 0.8.3.

- **Simpler Add bot.** Plainer Add bot: the key step only asks where the key lives; the webhook step is required and collects the webhook URL, webhook key and bot public key; the instructions sit behind View instructions and Copy instructions; station key, fingerprint and technical notes are gone from the flow; README lists any USB speaker and microphone and the supported screen sizes (docs/display.md).

## 0.8.3

Patch release. Installs over 0.8.2.

- **Plainer Add bot key step.** "Where the bot's key lives": Bot computer (default) or Home computer (replies come back faster), with one note that a computer on your home network must run the Grok Bot app and stay on and reachable 24/7. No commands or technical wording.
- **Shorter setup prompt.** The human-facing background paragraph is gone from both routines and the home setup line; the bot is also asked to reply with its public key. Operative instructions unchanged.

## 0.8.12

Patch: the Updates page has a "View update log" button that shows the last 200 lines of the latest update attempt (admin sign-in only, passwords and keys hidden). A failed update now says why. When main on GitHub was rewritten, the updater moves to the official `origin/main` (only when `origin` is the grok-station GitHub repo) instead of failing; untracked files and the settings in `/etc/grok-station` and `/var/lib/grok-station` are kept. Stations on 0.8.10 or older need the one-time recovery in [update-recovery.md](update-recovery.md). pii-guard's push check now scans every commit not on a remote when the remote's old commit isn't known locally.

## 0.9.0

Minor release. Installs over 0.8.12.

- **Deploy step in the setup prompt.** The instructions copied from Add bot replace the one-time keygen lines with a section, "Deploy bot key setup on system running Grok Bot (one time)". It names the files and the directory they land in. For a bot-computer key, run it on your own computer and on the home computer running Grok Bot. For a home-computer key, run it on that home computer. It asks before the script runs, it is one time, and the station never pushes code or asks for those files again. The script is filled in with the bot id, the station address, the certificate fingerprint, and the station key. After it runs, reply with the public key it printed. Never send the private key or the seed. Changing the key later means pairing again with Replace key. The routine the bot follows for each question is unchanged.
- **No partial installs.** Files move into place only after key setup succeeds. Clear messages on a failed step or a missing pinned file. Downloads never follow redirects.
- **Release check.** `tools/check-deploy-pins.py` proves the pinned files match git and GitHub. Home setup is macOS only.

## 0.9.1

Patch release. Installs over 0.9.0.

- **Card image builds again.** `tools/build-image/build-image.sh` works with the current Raspberry Pi OS list, which no longer publishes a download checksum for Lite; the `.sha256` file published beside the image is used instead.
- **Any network on first boot.** The card no longer needs Ethernet. It waits up to 10 minutes for a cable, Wi-Fi set in Raspberry Pi Imager, or Wi-Fi added at build time with `--wifi-from-env` (values come from the environment and are never printed).
- **Build on macOS, Windows or Linux.** `tools/build-image/docker-build.sh` and `docker-build.ps1` run the Linux build in a container. When a container has no partition devices, the build attaches the root partition by offset.
- **Same install as a normal setup.** The card runs the same `install.sh` on first boot. `install.sh` now installs `git` and `ca-certificates` itself, so an image-built station gets a git clone and in-app updates. A test fails if first boot starts doing install steps of its own.
- **Clear screen while installing.** First boot shows "Installing Grok Station" and the plain step names; details go to the log.

## 0.9.2

Patch release. Installs over 0.9.1.

- **Card image without a network.** `install.sh` runs inside the image at build time (`GROK_STATION_IMAGE_BUILD=1`: packages, venvs, wake models; no secrets) and finishes offline on first boot (`GROK_STATION_OFFLINE=1`: secrets, files, services). The source is a real clone of GitHub `main`, so the Updates page works. Wi-Fi is set up through the setup hotspot. Native build scripts: Linux (`build-image.sh`), macOS (`build-image-macos.sh`, Lima VM) and Windows (`build-image.ps1`, WSL 2); the Docker scripts are gone.
- **Main bot first.** The main bot's star is gold (`--gs-gold`) and the main bot is listed first.
- **Wake words.** Every shipped model was installed (0.9.4 removes all but Hey Grok). Training is a fixed 10 recordings per session; when the training gate says it isn't good enough, **Train more** records 10 more and adds them. Wake words other than Hey Grok can be deleted (recordings, model, entry; the listener reloads). The station still does not train words itself (see docs/enroll.md).
- **Team logos.** Logo hosts default to `a.espncdn.com` and move under an advanced option.
- **Display cards from bots.** The Add bot instructions teach recipe, sports, calendar and home cards (`grok-station-reply seal-show`, then `post --show`). Configure has **Copy updated instructions** for bots already set up.

## 0.9.3

Patch release. Installs over 0.9.2.

- **Linux-only image build.** The card image builds with `tools/build-image/build-image.sh` on a Linux PC or a Linux VM. The macOS (`build-image-macos.sh`, Lima) and Windows (`build-image.ps1`, WSL) scripts are removed. The normal install is unchanged.

## 0.9.4

Patch release. Installs over 0.9.3.

- **Hey Grok only.** Hey Grok is the only wake word model in the repo and the only one the installer copies. On update, the installer deletes the six starter models 0.9.2 installed and their entries in the wake overrides. Custom words you trained or uploaded (`wake/models/enrolled`) are never touched. Reach any bot with "Hey Grok, ask <bot>", or give it its own trained word.

## 0.10.0

Minor release. Installs over 0.9.4.

- **Wake words made from your recordings.** The station builds a custom wake word from the 10 recordings you make on it. No training computer is needed. It reuses the speech features Hey Grok already computes (openWakeWord's melspectrogram and embedding models) and compares them against your recordings (`wake/fewshot.py`). The station sets how strict each word is from your recordings, the room noise it records first, a bank of everyday speech (`wake/models/fewshot_negatives.npz`, made from synthetic voices), Hey Grok, and your other words. When it can't tell the word apart well enough, the page says "Not good enough yet" and **Train more** records 10 more and rebuilds it.
- A word that sounds too much like Hey Grok or another of your words is refused, with a message saying which one.
- Deleting a wake word also deletes its recordings and what was built from them.
- A model trained on a separate computer (`.onnx` in `/var/lib/grok-station/wakewords`) still loads, and replaces a recorded word with the same name.
- `grok_wake.py --bench file.wav` times detection on recorded audio. `tools/fewshot_eval.py` and `tools/fewshot_tts.py` are the offline eval (dev only).
- Not yet tested on a Pi.

## 0.10.1

Patch release. Installs over 0.10.0.

- **Recorded words stayed off.** Bots get a `hey_<bot>` entry that starts switched off (0.9.2), and deleting a word leaves it hidden. Recording that phrase in 0.10.0 built the word but kept it off, so the listener logged "turned off on the admin page" and never woke on it. Starting a recording now clears the off switch and the hidden flag for that phrase.
- **The dialog never finished.** The admin page now marks a word Ready as soon as the built word appears in `wakewords.enrolled.json` (new `built_at` field; a 0.10.0 entry without it counts too). The dialog then shows Ready and closes. If building throws an error, the word is marked failed with a plain message. If the listener stops answering for 3 minutes, or restarts during a build (a `building.json` marker is left behind), the word is also marked failed. All three cases are logged.
- **Unfinished words in the list.** In-progress and failed words show on the Wake words page with Record voice and Delete.
- **Less memory while building.** The build reuses the listener's loaded feature models and Hey Grok model. Before, it loaded second copies, peaking around 310 MB against the unit's 350 MB cap; now it's about 215 MB. The cap is raised to 450 MB.
- **Quieter log.** A bot with no portrait no longer logs "relay get portrait failed: HTTPError" every sync. That 404 is normal (the station shows initials), and other errors are still logged with their HTTP code.

