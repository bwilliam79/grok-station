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
- **No hosted mailbox.** There is no hosted mailbox. Replies are posted by Grok Bot running on a home machine on the station's LAN.
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

## 0.8.3

Patch release. Installs over 0.8.2.

- **Plainer Add bot key step.** "Where the bot's key lives": Bot computer (default) or Home computer (replies come back faster), with one note that a computer on your home network must run the Grok Bot app and stay on and reachable 24/7. No commands or mailbox wording.
- **Shorter setup prompt.** The human-facing background paragraph is gone from both routines and the home setup line; the bot is also asked to reply with its public key. Operative instructions unchanged.
