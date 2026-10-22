# Single-device plan: relay, admin and training on the station Pi

Status: approved Oct 20 with changes: no training on the Pi (prebuilt Hey Grok model only; optional trainer app, see trainer-app-plan.md); minimum Pi 4 2 GB, 16 GB card. Sections on training sizing are kept for the trainer app. Base: origin/main a5a7d6f. Hardware target: a Pi 5 with 8 GB. Minimum: see "Training sizing".

## 1. Where things run today
- Display (`app/`), wake listener (`wake/`), admin page (`admin/`): on the Pi as systemd services.
- Relay (`relay/relay.py` + Caddy mTLS front, Docker compose): on the relay host. Holds the xAI key, bot tokens,
  personas, webhooks, handoff state, enroll inbox.
- **Wake-word training: on the training host, not the relay.** It's a launchd job
  that runs `enroll_watch.sh` in the openWakeWord training directory (venv, gen.py, sweep.sh, data/) and runs every
  60 s. It reads the relay's enroll inbox and publishes `<phrase>.onnx/.json` that the relay serves to the Pi.
  The `train/` scripts in this repo are copies of those scripts. They use macOS-only pieces: caffeinate, MPS
  env vars and the plist.

## 2. Target architecture (one Pi, no Docker)
```
 grok-station.service        display (pygame)                     127.0.0.1:8787
 grok-station-wake.service   wake listener                        127.0.0.1:8789
 grok-station-relay.service  relay.py, native venv, User=grok-relay 127.0.0.1:8790 (loopback only)
 grok-station-admin.service  admin page, HTTPS only (self-signed/local CA), LAN :443, no :80 listener at all
```
- Display and wake listener talk to the relay over loopback WebSocket with no TLS, pairing or client certs. Access is
  controlled by unix-user and file perms plus one local token in a 0600 file.
- Done: bot replies are `POST /voice/reply` on the admin HTTPS port (LAN only; never port-forward it), forwarded to the
  loopback relay. The question rides on the webhook, so the old request route is gone. See docs/bots.md.
- Enroll clips go straight to `/var/lib/grok-station/enroll/inbox`. Training runs locally. Published models go to
  `wake/models/enrolled/`. The `sync_wakewords` HTTP pull is removed and replaced with an inotify/poll reload.
- Updates: one `tools/grok-station update` on the Pi fetches the release tag, verifies it, installs it, restarts the
  services and rolls back if a health check fails.

## 3. Delete / merge
Delete:
- `admin/pairing.py` (827) and `admin/pair_listener.py` (295)
- `relay/pair_client.py` (153), `pair_transport.py` (175), `control.py` (369), `host_update.py` (378),
  `release_fetch.py` (79) and `install_support.py` (125)
- `relay/relay-install.sh` (240), `relay-install.ps1` (105), `grok-relay.ps1` (343), `grok-relay` (2)
- `relay/Dockerfile`, `Dockerfile.caddy`, `compose.yaml` and `Caddyfile` (~200)
- `systemd/grok-station-wake-tls.conf`
- the mTLS and fingerprint-lockout code in relay.py and grok_wake.py
- the related tests

Shrink:
- `relay/grok_relay_cli.py` (1218) becomes a small `grok-station relay ...` subcommand (~250 lines: status, rotate key,
  list bots).
- `relay/release.py` (879) merges into the single updater (~400).

Merge or move:
- relay.py's `/config`, `/bots` and `/state` paths become `/etc/grok-station` and `/var/lib/grok-station`. The env
  names stay, with Pi defaults.
- The admin page gains xAI key (write-only field, shows only "set / last 4 never"), Bots, Voices and Webhooks tabs.
  Today these proxy to the relay admin API. After the merge the admin server writes the files itself and the relay
  hot-reloads them.
- `train/` becomes Linux-native: systemd units replace the plist; drop caffeinate and the MPS vars; CPU torch;
  `nice`/`ionice`.
- New `install.sh` path: one device, which creates the users, venvs, dirs, units and the HTTPS cert.

## 4. Config and secrets on the Pi
| What | Path | Owner/mode |
|---|---|---|
| xAI key | /etc/grok-station/secrets/xai_api_key | grok-relay:grok-relay 0400, dir 0700 |
| bot voice tokens, webhooks.json | /var/lib/grok-station/bots/ | grok-relay 0600, dir 0700 |
| local relay token (display/wake → relay) | /etc/grok-station/secrets/local_token | root:grok-station 0440 |
| admin login/token, TLS key | /etc/grok-station/admin/ | grok-admin 0600 |
| personas.json, portraits | /var/lib/grok-station/personas/ | grok-relay 0600/0700 |
| handoff state, minted hashes, audit | /var/lib/grok-station/{state,voice,audit} | grok-relay 0700 |
| enroll inbox, training data, models | /var/lib/grok-station/train/ | grok-train 0750; models 0644 |
- The admin server writes secrets through the existing helper and sudoers (`grok-station-helper set-secret <name>`,
  value on stdin) so it never runs as the relay user. It never echoes values back or logs them.
- Accept the residual risk of keeping the key on the Pi; the owner decided this on Oct 20. Optional extra: full-disk/LUKS
  isn't required.

## 5. Migrating the old relay host state to the Pi
Inventory on the old relay host (read-only): relay secrets (xai_api_key, relay_token, admin_token, keyterms), the private
personas.json and portraits, the bots dir (webhooks.json, overlay, status, audit), voice/minted, handoff
state/pending, the enroll inbox, enroll/published models, and the training dir (data/, voices/, venv rebuilt
rather than copied).

Transfer, with no scp/rsync and no secrets printed:
1. `grok-station migrate export` on the old relay host builds a tar of the state dirs and encrypts it with `age` to a recipient
   key generated on the Pi (`grok-station migrate init` prints only the public key). The archive is 0600 and holds no
   plaintext secrets outside it.
2. Stream it over the existing SSH channel: `cat bundle.age | ssh grok-station "grok-station migrate import"`. This is
   the same `cat | ssh` pattern the deploys use. Import decrypts in place, installs files with their modes, verifies
   sha256 against a manifest and prints only file names, counts and hashes.
3. No training data migrates. The existing enrolled models (hey_hank, hey_finn, hey_rhea) can go in the bundle as
   drop-dir models.
4. Tokens: keep the existing bot voice tokens, so the bot routines don't change. Only `TILE_INGEST_URL` moves to the
   Pi's HTTPS URL. Each bot's routine config needs a URL edit, coordinated through Rhea. That's an external change, so
   get the owner's OK first.
5. Delete the bundle on both ends after verifying it. The the old relay host relay stays running (read-only fallback) until step 7
   of the ship order.

## 6. the security reviewer conditions for unsigned updates (still apply)
- GitHub account with passkey/hardware-key 2FA. Must be on before the first update ships.
- Protected release tags (`v*`) that only the owner can create, and no force-push to main.
- Updater pins the repo and installs only tags, never branch heads. It verifies that the tag commit is on main and
  checks the release-asset sha256 from the tag's manifest.
- Daily check off by default. Updates are manual from the admin page, with rollback on a failed health check.
- pii-guard on every release diff.

## 7. Training sizing (moved to the trainer app; the Pi doesn't train)

Shipped hardware: **minimum Pi 4 2 GB + 16 GB card; recommended Pi 4 4 GB / Pi 5, 32 GB.** The figures below size the
trainer app's host computer.

### Original sizing (measured on the current training host, Oct 20)
Measured (the old relay host training dir, 43 GB total):
- venv 1.3 GB (torch 308 MB on macOS; **est.** 0.8–1.2 GB for the aarch64 CPU torch + onnxruntime + piper venv)
- `data/` 21 GB, of which the ACAV100M negative features are 16 GB (`.npy`, memory-mapped), bgparquet 3.9 GB,
  background 918 MB, validation features 176 MB and RIRs 8.5 MB
- piper voices 1.8 GB and piper-sample-generator 199 MB
- `runs/` 19 GB across 18 runs, about 1 GB per phrase of intermediate synthetic clips and features. Most of it is
  sweep experiments that a Pi doesn't need.
- enroll inbox 4.4 MB, jobs 824 KB, finished models 415 KB `.onnx` + `.json` each
- Peak training footprint (health log `train_footprint_mb`): 1.79 GB RSS. The mmap of the 16 GB features inflates
  apparent RSS, but that's page cache, not committed RAM.
- Enroll job time on the old relay host (12 cores, 32 GB, synthetic set already cached): 5–6 min per job (four jobs on Oct 19).

Estimated for the Pi:
- Disk needed: ~27 GB (venv 1.2 + data 21 + voices 2 + 1–1.5 GB per enrolled phrase + scratch 2).
  Could drop to ~12 GB by subsampling the ACAV features to about 4 GB, but that's untested against the gate
  (FA/h ≤ 1.0).
- Peak RAM: ~2 GB for training, plus ~0.8 GB for the rest of the station, so ~3 GB. With a MemoryMax=3G cgroup.
- CPU time, **est.**:
  - Pi 5: 30–60 min per enroll job when the synthetic set is cached.
  - First-time synthetic generation for a new phrase (piper TTS, thousands of clips): 2–6 h.
  - Pi 4: roughly 2x the Pi 5 times.
  - Sustained load means a Pi 5 needs an active cooler.
- Validate on the Pi before ship (test plan T6).

Revised hardware:
| | Without training | With training |
|---|---|---|
| Minimum | Pi 4 2 GB, 16 GB A1 card | **Pi 4 4 GB, 64 GB A2 card** (training is disabled automatically below 4 GB RAM) |
| Recommended | Pi 4 4 GB / Pi 5, 32 GB | **Pi 5 8 GB, 128 GB A2 card or NVMe SSD, active cooler** |
- The existing station (Pi 5 8 GB) qualifies if its card is ≥ 64 GB. Check before migration.
- The Pi 4 2 GB stays supported for the station only, with training off and a note on the admin page.

## 8. Test plan
- T1: unit tests, with pairing and mTLS tests removed and new ones added: admin secret write (no echo, perms),
  loopback-only relay bind, no :80 listener, updater tag verification and rollback.
- T2: an `install.sh` dry run on a clean 64-bit Pi OS Lite image (spare SD card on the same Pi 5).
- T3: a full voice session, a multi-turn session, the goodbye path and a bot handoff (webhook request/reply via the Pi
  HTTPS ingress).
- T4: migration import on the spare card; diff the manifest hashes. Old relay host untouched.
- T5: reboot test: all services come up, and voice works within 60 s.
- T6: training: one enroll job on the Pi records disk, peak RSS (`/usr/bin/time -v`), wall time and temperature.
  The wake listener must stay responsive during the job. Then update the sizing in section 7 with the measured numbers.
- T7: update and rollback against a test tag.
- T8: optional Pi 4 4 GB canary, plus the Pi 3B experiment, which is unsupported.

## 9. Ship order (one ship)
1. Build everything on a branch. Tests green, pii-guard clean.
2. the owner reviews. Push to main as a normal fast-forward.
3. Install on a spare SD card in the station Pi. Keep the current card untouched as the rollback.
4. Migrate the state (section 5), then run T3–T7.
5. Check that no session is active, then swap. The Pi now runs everything, and the bot routines switch to the
   Pi URL with the owner's approval.
6. Soak for a few days.
7. With the owner's explicit approval, stop and remove the the old relay host relay containers, the training launchd job and the
   relay dirs. Keep an encrypted archive until the owner says to delete it.

## 10. Risks
- The key on the Pi means theft of the card exposes the xAI key and bot tokens. Accepted. Mitigations: key rotation
  from the admin page, 0400 perms, and no key in logs.
- Training on the Pi is slow and hot. A long job could starve the wake listener or audio. Mitigations: cgroup
  limits, idle IO and pausing training during an active session.
- SD wear and space from 27 GB of training data. Prefer NVMe, and check free space before a job.
- Self-signed HTTPS shows a browser warning. A local CA is installed once on the owner's devices.
- Bot routines depend on the URL change. During the cutover, keep the old ingress until all bots confirm.
- Unsigned updates rely on the GitHub account; see section 6.
- Synthetic-generation deps such as piper may lack aarch64 wheels. Check this first, during T2.

## 11. Rough line-count delta (est.)
- Removed: pairing, mTLS, installers, host updater and Docker/Caddy, about −4,300 lines (incl. tests ~−1,200).
- CLI and release shrink: about −1,450.
- Added: admin config tabs, secret helper, migrate export/import, single updater, Linux training units and the
  installer, about +1,500, plus tests about +600.
- Net: about −3,600 lines (repo ~15.8k Python lines today, excluding tests).
