# grok-station

Raspberry Pi 5 (Pi OS Lite 64-bit, Trixie, no desktop) kiosk display:

1. **Boot splash**: Plymouth `script` theme, a centered Grok mark on black with a slow opacity pulse.
2. **Station UI**: one Python process (`grok-station.service`) that runs a small stdlib HTTP listener on `:8787`
   and draws the screen by rendering with pygame off-screen into a 16bpp surface written straight to `/dev/fb0`
   (no X/Wayland/browser; SDL KMS/DRM frames never reached the screen on the Pi 5).

## Screens

Design follows the "Hearth" palette (warm near-black, warm white text). Fonts are Inter Tight 600 (display)
and Inter 500 (body), bundled in `assets/fonts`. Sizes come from a 1080p spec and scale with the screen
(`min(h/1080, w/1920)`), so any resolution or aspect ratio works (the Pi runs 2560x1706).

| Screen | When | Shows |
| --- | --- | --- |
| Idle | default | Large clock + AM/PM, date, weather top-right (icon, temp, condition, high/low). Lower third is reserved for a future "Next up" card (`draw_next_up` hook). |
| Message | `POST /show` | With `from`: circular portrait with a warm ring at left; name, the question (`heard`, muted italic) and the reply at right, sized to fit. Without `from`: the text, large and centered. Stays up `GROK_STATION_HOLD` seconds after the last message, then fades back. |
| Night | `GROK_STATION_NIGHT` hours | Only a dim warm clock on black, drifting a few px every 10 min against burn-in. |
| Weather offline | no successful fetch for 1 h | Small amber cloud-off icon in place of stale weather. |

The display redraws once a minute (plus on messages and weather updates); otherwise the process sleeps in
`pygame.event.wait`. The clock colon is steady (an optional blink, `GROK_STATION_BLINK=1`, repaints only the colon's
rectangle once a second). While **listening**, a solid teal ring (the thinking arc's style, but full and continuous, no ticks) around every persona (Grok included) pulses once a second (thickens, brightens and glows, smooth cosine); while **thinking** the violet ring spins (one turn per 1.2 s, `GROK_STATION_SPIN_FPS`, default
15) by repainting only the portrait rectangle from a cached circle-cropped photo. Each animation's loop (15 pulse frames, 18 spin frames) is rendered once per portrait, then reused. Speaking and idle rings are static. The listening screen has no wake-word hint (the station is already awake). The colon is centered between the
hour and minute digits on their ink (not the font box), and the date is centered under HH:MM (AM/PM excluded).
Tests: `SDL_VIDEODRIVER=dummy python3 -m unittest discover tests`. Weather is from [Open-Meteo](https://open-meteo.com/) (no key), refreshed every 15 min,
last good reading cached in `/var/cache/grok-station/weather.json`.

Portraits: `assets/portraits/<name>.png` (lowercased `from`), then `/opt/grok-station/portraits/` for
local-only images. Missing portrait = initial monogram. `grok.png` ships in the repo.

The session ring is drawn by a painter looked up in `RINGS` by state (only `speaking` exists today), so
listening/thinking states can be added later as new painters without touching the layout.

Preview without a Pi: `python3 app/grok_station.py --demo /tmp/out 2560x1706` renders every screen to PNG.

## Config (`/etc/grok-station/env`, all optional except the token)

| Variable | Default | |
| --- | --- | --- |
| `GROK_STATION_TOKEN` | (required) | Bearer token for `/show` |
| `GROK_STATION_LAT` / `GROK_STATION_LON` | (none) | Weather location; weather is off until both are set |
| `GROK_STATION_UNITS` | `fahrenheit` | or `celsius` |
| `GROK_STATION_HOLD` | `60` | Seconds a message stays up |
| `GROK_STATION_NIGHT` | `22-6` | Night clock hours (24h start-end); empty disables |
| `GROK_STATION_FADE` | `1` | `0` disables the fade back to idle |
| `GROK_STATION_PORT` | `8787` | |

## Resolution

The display draws from the real framebuffer size; nothing assumes 2560×1706. Every size, margin, ring width and font
is a spec value at 1920×1080 times `s = min(w/1920, h/1080)` (the limiting edge), placed relative to the real edges,
and text auto-fits (wraps to its line limit, then shrinks to a floor, then ellipsizes).

- **Supported:** 800×480 up to 3840×2160 (any aspect ratio in that range).
- **Larger screens** are letterboxed: the UI draws in a centered 3840×2160 area (at most, per axis) on the
  background color.
- **Smaller screens** still draw at native size (best effort), with a log line saying the size is below the
  supported minimum.

Offscreen proof: `python3 app/grok_station.py --demo OUT WxH` renders every screen. The tests in `tests/test_display.py`
render the enrollment screens at 800×480, 1280×720, 1920×1080, 2560×1706 and 3840×2160. They check that no ink
crosses the side margins or the bottom safe area, and they cover the letterbox above 3840×2160.

## Layout

| Path | Installed to |
| --- | --- |
| `splash/grok.plymouth`, `grok.script`, `grok.png` | `/usr/share/plymouth/themes/grok/` |
| `app/grok_station.py` | `/opt/grok-station/grok_station.py` |
| `assets/fonts/*`, `assets/portraits/*` | `/opt/grok-station/assets/` |
| `systemd/grok-station.service` | `/etc/systemd/system/` |
| (token, not in repo) | `/etc/grok-station/env` (root:root 600): `GROK_STATION_TOKEN=...` |
| `tools/make_fonts.py` | (dev only) rebuilds the static, subset fonts from the Inter variable fonts |

`install.sh` (run as root on the Pi) installs packages (plymouth, python3-pygame, Mesa EGL/GBM), copies files,
appends `quiet splash loglevel=3 vt.global_cursor_default=0 logo.nologo consoleblank=0 plymouth.ignore-serial-consoles`
to `/boot/firmware/cmdline.txt` (single line), adds `disable_splash=1` to `config.txt`, runs
`plymouth-set-default-theme -R grok` (rebuilds initramfs), masks `getty@tty1` (kiosk; use SSH), and enables the service.

The service starts before `plymouth-quit`, runs `plymouth quit --retain-splash`, then takes the display.
It runs as `grok` (groups video/render/input).

## API

- `POST /show`: header `Authorization: Bearer <token>`, JSON `{"text": "...", "from": "Grok", "heard": "..."}`.
  `text` is required (max 500 chars); `from` (speaker name, max 40) and `heard` (what was asked, max 300) are optional.
  `from` equal to the voice assistant's name (`GROK_STATION_VOICE_NAME`, default Grok) is dropped, so a posted
  message can't appear as the assistant.
  200 ok · 400 missing text/bad JSON · 401 bad token · 403 non-private client IP · 413 body > 8 KB.
- `GET /health`: 200.
- `POST /state` (**localhost only**, no token): `{"state": "...", "heard": "...", "text": "...", "done": false}`.
  Used by the wake listener on the same Pi. States: `listening` (solid pulsing teal ring, `GROK_STATION_LISTEN_HOLD` s,
  default 12), `thinking` (violet arc, question shown), `speaking` (warm ring, question + reply text; with
  `"done": true` it holds `GROK_STATION_REPLY_HOLD` s, default 15), `notice` ("Didn't catch that.", 4 s), `error` (relay
  unreachable, 5 s), `idle`.
  Each state has a safety timeout back to idle. 403 from any other address.

Anything on the LAN with the token can post to it (a script, a home automation, an Apple Shortcut using
"Get Contents of URL"). Each message render is saved to `/run/grok-station/last.png` for remote verification.

```sh
curl -H "Authorization: Bearer $TOKEN" -d '{"text":"hello"}' http://grok-station.local:8787/show
curl -H "Authorization: Bearer $TOKEN" -d '{"from":"Grok","heard":"what is the weather","text":"Mild and clear."}' \
  http://grok-station.local:8787/show
```

## Wake word (proof of concept)

`wake/grok_wake.py` runs as its own service, `grok-station-wake.service` (user `grok`, group `audio`), so an audio
crash can't take down the display. It reads 16 kHz mono from the USB speakerphone (EMEET OfficeCore M0 Plus) via
`arecord`; the device is picked by name (`WAKE_DEVICE_NAME`, default `EMEET`) for both capture and playback (chime
and replies go to the same card, `plughw:CARD=...`), runs [openWakeWord](https://github.com/dscripka/openWakeWord) with ONNX
Runtime, and on a hit (score ≥ 0.5, 2 s cooldown) plays a 200 ms two-tone chime on the speakerphone and POSTs
`{"state":"listening"}` to `http://127.0.0.1:8787/state`. Mic level and peak score are logged every 30 s, plus
each wake word's peak when it reached 0.05 (`peaks hey_rhea=0.712 ...`), so a missed real-voice wake can be tuned.

**Wake words pick the persona.** "Hey Grok" plus one phrase per teammate on voice: Hey Ash, Hey Miles, Hey Kai,
Hey Piper, Hey Jules and Hey Rhea (**provisional**: just under the 0.80 recall goal, threshold 0.8 with patience 2:
0.78 recall / 1.03 false wakes per hour on the 10.7 h validation set, versus 0.995 / 1 that missed the real voice,
until real-voice enrollment). Hey Finn isn't shipped yet (pending real-voice enrollment). The models are
generic phrase models trained with openWakeWord on synthetic speech, in `wake/models/` (~400 KB each), installed
to `/opt/grok-station/wake/models`. `wake/wakewords.json` (installed as `/opt/grok-station/wake/wakewords.json`,
override with `WAKE_CONFIG`) lists each word's `name` (model file stem), `persona` (registry slug, or `grok`),
`threshold`, `patience` (consecutive 80 ms frames at or above the threshold) and optional `provisional`. The
optional `fallback` entry (`hey_jarvis` → Grok) keeps the built-in openWakeWord model loaded as a safety net;
drop it to retire Hey Jarvis. If several words fire on the same frame the highest score wins; one cooldown is
shared. A wake opens the session as that persona: the Pi connects to `/ws?persona=<slug>`, and the relay starts
the session with that persona's voice and instructions (unknown slugs fall back to Grok) and sends the `persona`
event so the display shows them from the first "listening" frame. The persona event keeps the current phase
(listening stays listening; the violet ring spins only once a response has started), so a teammate's listening
screen matches Grok's: same solid pulsing ring, with their portrait (Grok's is the logo). Without a config file the old single
`WAKE_MODEL` / `WAKE_THRESHOLD` behaviour applies. Wake detection is fully local; audio leaves the Pi only after a wake, and only to
the voice relay below (when configured).

Setup on the Pi (venv, since openWakeWord's tflite dependency has no Trixie arm64 wheel):

```sh
sudo install -d -o grok -g grok /opt/grok-station/wake
python3 -m venv /opt/grok-station/wake/venv
/opt/grok-station/wake/venv/bin/pip install --no-deps openwakeword
/opt/grok-station/wake/venv/bin/pip install onnxruntime numpy tqdm scipy scikit-learn requests
/opt/grok-station/wake/venv/bin/python -c 'import openwakeword.utils as u; u.download_models(["hey_jarvis"])'
sudo install -m755 wake/grok_wake.py /opt/grok-station/wake/
sudo install -m644 wake/wakewords.json /opt/grok-station/wake/
sudo install -d /opt/grok-station/wake/models && sudo install -m644 wake/models/*.onnx /opt/grok-station/wake/models/
sudo install -m644 systemd/grok-station-wake.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now grok-station-wake
```

Optional overrides in `/etc/grok-station/wake.env`: `WAKE_MODEL`, `WAKE_THRESHOLD`, `WAKE_COOLDOWN`,
`WAKE_DEVICE_NAME`, `WAKE_STATE_URL`, `WAKE_GAIN` (software gain; default 2 when `WAKE_DEVICE_NAME` contains EMEET, else 4 for the quieter Razer), `WAKE_CAPTURE_RATE` (default 16000;
set it to the device's native capture rate, see `/proc/asound/cardN/stream0`. The EMEET mic is 16 kHz only, so it
is used as is and upsampled ×1.5 to 24 kHz for the voice session. Use 48000 for devices whose 16 kHz mode is poor,
such as the Razer USB Sound Card (muffled, the wake word never scores); 48 kHz is decimated to 16 and 24 kHz).
Playback is 48 kHz stereo on the EMEET; `aplay` via `plughw` converts the 24 kHz mono reply. Offline check: `grok_wake.py --test clip.wav` (16 kHz mono) runs the clip through
the same detector and prints which word and persona it triggers plus every word's peak score (no session is
opened). `grok_wake.py --bench [seconds]` times the loaded models on noise. On a Pi 5 the listener used about 12%
of one core with Hey Jarvis alone; `--bench` measures 10.6% of one core for all eight models (seven custom words plus the Hey Jarvis fallback) vs 9.4% for Hey Jarvis alone (the shared audio front end dominates). The spinning ring renders one revolution once (about 66% of a core for 1.2 s), then reuses those 18 frames at about 6% of a core; a colon blink costs about 2 ms.

## Voice (proof of concept)

A wake starts a conversation: "Hey Grok" (or a teammate's wake word) → chime + **listening** → speak → **thinking** (Grok started a
response) → **speaking** (reply plays on the speakerphone, text on screen) → idle. Mic audio keeps streaming until a
response actually starts, so a short noise or the tail of the wake word (which can trip the server VAD without
producing a reply) doesn't swallow the real question. The first `VOICE_SKIP_MS` (default 300) of post-wake audio
is dropped for the same reason. Nothing heard (or only noise) for 10 s → "Didn't catch that." and idle.

**Multi-turn (follow-ups without the wake word):** after each reply the session stays open about 10 s (the relay's
`NO_SPEECH`). The screen goes back to the pulsing **listening** ring for the same persona, and you can just ask the
next question. Silence closes the session quietly. A short "thanks", "that's all", "that's it" or "bye" on a
follow-up ends it right away. Caps:
- 60 s per turn (`MAX_SESSION`);
- about 3 min per conversation (`MAX_TOTAL`, default 180);
- no daily talk-time limit;

The mic is muted while a reply plays, and the reply's echo is dropped before the follow-up opens.
`MULTI_TURN=0` on the relay restores one answer per wake. Design: `docs/multiturn.md`.

```
Pi: grok_wake.py ──LAN wss, client cert + device token──▶ tls (Caddy) ──▶ relay/ ──▶ wss://api.x.ai/v1/realtime
     mic PCM16 24 kHz ▲  reply PCM16 24 kHz + JSON events ▼   (Docker, any LAN host)   grok-voice-3.0, server_vad
```

- **The xAI API key lives only on the relay host**, never on the Pi. The Pi holds a random device token.
- `relay/relay.py` (aiohttp): `GET /ws` (`Authorization: Bearer $RELAY_TOKEN`) opens one xAI realtime session,
  forwards mic audio up and reply audio, transcripts and turn events down, then (multi-turn) sends `turn_done`
  after each reply and closes after 10 s with no speech in progress and no response (counted from the last end
  of speech or the start of the follow-up), on a closing phrase, at 60 s per turn or 180 s in total (cost guard;
  realtime voice bills by the minute). One session at a time. The instructions get the current local date and
  time (`TZ_NAME`, default America/Chicago); the optional `keyterms` secret (comma-separated) biases
  transcription toward names the station should recognize.
  `GET /health` returns `{"ok":true}`; with the token it adds key presence, usage since start and the caps. Logs carry
  event types and durations only, no transcripts or audio.
- **Guards:** requests with an `Origin` header (any browser) get 403; one session at a time (409); no daily
  talk-time or session cap (the per-turn and per-session caps bound each conversation); 5 bad tokens in 10 min lock the source out for 15 min (429). Behind the
  `tls` front the source is the **client certificate** (Caddy sets `X-Client-Cert-Fp`; the relay trusts it only
  from the `tls` container), so bad tries with one certificate can't lock out the station; direct loopback callers
  are keyed by address. Upstream audio is capped at `MAX_UP_SECONDS` (`MAX_TOTAL` + 5 = 185 with multi-turn, else 65) per session. 80% / 100% of a cap and
  lockouts raise an alert: a JSON file `{title, message, priority, tags}` dropped into the dir mounted at `/alerts`.
  Point `GROK_STATION_ALERTS` at a folder used only by this relay that your ntfy forwarder drains (stats-dashboard's
  ingest-relay drains `/drop/<name>` folders, labels each push with the folder name and caps it at 6/hour);
  otherwise it's a log line.
- **Container:** read-only root, `/tmp` tmpfs, all capabilities dropped, `no-new-privileges`, 64 pids, 0.5 CPU,
  128 MB with no swap, non-root user, base image pinned by digest, dependencies installed with
  `--require-hashes` (`requirements.txt` is `pip-compile --generate-hashes` output from `requirements.in`).
- Run it: create a secrets dir outside the repo (mode 700) holding `xai_api_key`, `relay_token` and `keyterms`
  (each mode 600; `keyterms` may be empty), plus a state dir. In `relay/.env` (gitignored, paths only) set
  `GROK_STATION_SECRETS=<dir>`, `GROK_STATION_STATE=<dir>` and optionally `GROK_STATION_ALERTS=<dir>`, then
  `docker compose up -d --build`. Secrets are compose file mounts at `/run/secrets/*`, not environment
  variables, so `docker inspect` doesn't show them. The relay's port 8790 is published on the host's loopback only;
  LAN clients go through the `tls` service. Keep both off any public proxy or tunnel.
- **TLS front (`tls` service, `relay/Caddyfile`):** Caddy on LAN port `GROK_STATION_TLS_PORT` (8791) terminates
  TLS 1.3 and requires a client certificate signed by your private CA (`client_auth require_and_verify`), then
  proxies only `/ws`, `/health`, `/personas` and `/portrait/<slug>` to the relay; anything else is 404. No ACME,
  no admin API, no plain-HTTP listener, same container hardening as the relay, and it runs as a non-root user
  (`relay/Dockerfile.caddy` copies the binary to drop its bind-port file capability; 8443 doesn't need it). The device token is still required behind it. Restrict the port
  to the station's address in the host firewall too. Set `GROK_STATION_PKI=<dir>` in `relay/.env`; that dir holds
  `server.crt`, `server.key` (600) and `client-ca.crt` only, never the CA key. One-time setup with openssl
  (EC P-256; keep the CA dir mode 700 on the relay host, outside the repo):
  ```sh
  openssl ecparam -name prime256v1 -genkey -noout -out ca.key
  openssl req -x509 -new -key ca.key -sha256 -days 3650 -subj "/CN=grok-station private CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" -out ca.crt
  # server cert: CSR from a new server.key, signed by ca.key with
  #   extendedKeyUsage=serverAuth, subjectAltName=DNS:<relay-host>.local,IP:<relay-host LAN IP>
  # client cert: generate client.key + CSR ON THE PI (the key never leaves it), sign on the relay host with
  #   extendedKeyUsage=clientAuth, CN=grok-station; copy back only client.crt and ca.crt
  ```
  Certs are valid 825 days; re-issue before expiry (`openssl x509 -enddate -noout -in <cert>`). To revoke the Pi,
  re-key the CA (or issue a new client CA) and restart `tls`.
- **Rotating tokens** (use `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`; never reuse a token
  between the relay, `/show`, or anything else):
  - Relay device token: add the new token as a second line of `relay_token` and `docker compose up -d` (both are
    accepted), set `GROK_RELAY_TOKEN` in the Pi's `/etc/grok-station/wake.env` and
    `sudo systemctl restart grok-station-wake`, confirm a session works, then delete the old line and
    `docker compose up -d` again.
  - `/show` token: set the new `GROK_STATION_TOKEN` in `/etc/grok-station/env`, `sudo systemctl restart
    grok-station`, then update every sender (e.g. the Apple Shortcut) right away; there is no overlap window.
- Pi side: `/opt/grok-station/wake/venv/bin/pip install "websockets>=17"`, then add to `/etc/grok-station/wake.env`
  (root, 600) `GROK_RELAY_URL=wss://<relay-host>.local:8791/ws` and `GROK_RELAY_TOKEN=...`. Put `client.key`,
  `client.crt` and `ca.crt` in `/etc/grok-station/tls/` (root, 600) and install the drop-in
  `systemd/grok-station-wake-tls.conf` as `/etc/systemd/system/grok-station-wake.service.d/tls.conf`; it passes
  them to the unprivileged listener as systemd credentials. Only that CA is trusted for the server, and TLS 1.3 is
  required. (`GROK_RELAY_CA` / `GROK_RELAY_CERT` / `GROK_RELAY_KEY` override the paths for manual runs.) A plain
  `ws://` URL still works for a relay published without the TLS front. Without `GROK_RELAY_URL`
  the listener stays wake-word-only. `VOICE_GAIN` sets gain on audio sent to Grok (default = `WAKE_GAIN`).
- Mic audio for Grok is decimated 48 → 24 kHz from the same capture as the wake word; the wake detector is paused
  during a session (and reset after) so the reply can't re-trigger it. Mic streaming stops when the VAD reports
  end of speech. If the relay is unreachable the screen shows "Can't reach Grok right now." for 5 s.
- **Personas (teammates on voice).** Optional private registry `personas.json` in `GROK_STATION_PRIVATE` (dir 700,
  file 600, mounted read-only at `/config`; format in `relay/personas.example.json`) plus up to two read-only
  portrait dirs (`GROK_STATION_PORTRAITS_A` / `_B`). Keep the registry and photos out of git. Each entry:
  `slug` (`^[a-z]{2,16}$`), `name`, `aliases` (≤ 5), `voice` (must be in `GET /v1/tts/voices`, checked at startup),
  `portrait` (file name), `prompt` (≤ 200 words), `facts` (≤ 10 lines of ≤ 140 chars), `on_voice`. Name, aliases,
  prompt and facts all pass a privacy lint: any format/control character (zero-width, bidi, soft hyphen) rejects the
  field; then, after NFKC, links/emails, 6+ digit runs, IPv4, IPv6, 17-character VINs, `.lan/.local/.internal` names
  and the bare host names listed in the registry's top-level `private_hostnames` (kept in the private file, not the
  repo). Unknown fields or a failed check skip the entry (logged by slug and reason). If xAI's voice list can't be
  fetched, no persona goes on voice (fail closed; the relay retries every 5 min). The file is re-read when it changes. Every persona prompt gets a fixed safety line from
  the code (shared room, guest-safe, no personal data, never claims an action).
  - Grok gets a `talk_to(persona, question)` tool listing the roster. "Ask Sam …", "let me talk to Lee", "back to
    Grok" switch the **same** realtime session to that persona's voice and instructions (`session.update`), and the
    persona answers. A backup rule in the relay switches on "ask/tell/talk to/switch to <name or alias>" if the model
    didn't. The relay sends `{"type":"persona","slug","name"}`, and the display shows that name and portrait.
  - Personas answer live from their prompt and facts only. Anything beyond that gets "I can't reach my full notes
    from the station yet". Handing questions to the real teammate isn't built yet.
  - `GET /personas` (token) lists slugs, names and aliases; `GET /portrait/<slug>` (token) serves a registry
    persona's photo only. The Pi caches both in `GROK_STATION_PORTRAITS` (default `/opt/grok-station/portraits`,
    owned by the listener user) at startup and hourly (`grok_wake.py --sync` runs it once). The display reads
    `personas.json` there. `/show` never shows a photo (monogram only), and drops the name if it, or any word in it,
    matches Grok or a persona name, slug or alias after normalization (NFKC, hidden characters removed, casefolded,
    letters and digits only), so "Grok.", "Hank!" or "Ha\u00adnk" don't pass. `.jpg` portraits work for voice states.
- End-to-end check without talking: `grok_wake.py --ask question.wav [reply.wav]` (24 kHz mono) streams the
  file through the same path, plays the reply, and prints the end-of-speech → first-audio latency.

## Enroll mode (real-voice wake words)

Enrollment teaches the station a wake phrase from real recordings of the person who will say it. The station
records the clips, the relay passes them to a *training host*, and the training host trains and checks a model.
If the model passes, the station picks it up automatically. It's never started by voice. Architecture:
`docs/enroll.md`. Host scripts: `train/`.

### 1. The training host

The training host is any always-on machine you own that trains wake-word models. It can be the machine that
runs the relay, or another one; the relay just needs a filesystem path to it (usually it's the same machine, with
two folders mounted into the relay container). Requirements:

- **Hardware:** an Apple Silicon Mac (generation runs on `mps`), or Linux with an NVIDIA GPU (`GEN_DEVICE=cuda`).
  CPU-only works but is slow. 16 GB+ RAM.
- **Disk:** about 25 GB for the shared training data, plus about 2–3 GB per phrase for its synthetic set and
  features.
- **Software:** Python 3.10+ and an openWakeWord training setup in one directory (for example
  `~/wakeword-train`):
  - `venv/` with openWakeWord and its training dependencies;
  - an `openWakeWord/` checkout;
  - the synthetic-speech generator `gen.py` with `phrases.py`;
  - `mkcfg.py` and `sweep.sh`;
  - `data/` holding `background/`, `rirs/`, the ACAV100M negative features and `validation_set_features.npy`.
- **Time:** the first run of a new phrase generates its synthetic set (about 10 min on an M-series Mac). Every run
  then trains for about 6–15 min. Jobs run one at a time under `nice 10`, so the machine stays usable.

### 2. Set up the training host

1. Copy `train/enroll_*.sh` and `train/enroll_*.py` into the training directory, then run
   `chmod +x enroll_*.sh`.
2. Run the watcher every 60 s. It starts one job per ready session and is the job's watchdog.
   - **macOS:** copy `train/grok-enroll-watch.plist.example` to
     `~/Library/LaunchAgents/grok-enroll-watch.plist` and fix the paths. Then run
     `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/grok-enroll-watch.plist`.
   - **Linux:** copy `train/grok-enroll-watch.{service,timer}.example` to `~/.config/systemd/user/` (drop
     `.example`). Then run `systemctl --user enable --now grok-enroll-watch.timer` and
     `loginctl enable-linger $USER`.
   - **Important:** the launchd plist must set `AbandonProcessGroup` to true, and the systemd service must set
     `KillMode=process`. Without that, the scheduler kills the training job the moment `enroll_watch.sh`
     exits. The job then dies instantly with nothing in its logs, and after two tries the session is marked
     FAILED.
3. Create `enroll/inbox` and `enroll/published` in the training directory, and make the inbox writable by the
   relay container (uid 10001): `chmod 777 enroll/inbox`.

### 3. Connect the station

- **Relay:** in the relay's `.env`, set `GROK_STATION_ENROLL=<training dir>/enroll/inbox` and
  `GROK_STATION_WAKEWORDS=<training dir>/enroll/published`, then run `docker compose up -d --build relay`. Upload
  routes need the relay token and mTLS, the same as voice sessions. Without these two paths, enrollment is
  disabled.
- **Station:** `GROK_RELAY_URL` and `GROK_RELAY_TOKEN` are already set for voice, and nothing else is needed.
  Recorded clips are also kept on the Pi in `/opt/grok-station/wake/enroll/<phrase>/<session>/`.

### 4. Start an enrollment

Stand where you'd normally talk to the station, about an arm's length away. Then start it from the LAN with the
`/show` token, or on the Pi:

```sh
curl -H "Authorization: Bearer $TOKEN" -d '{"phrase":"hey_finn"}' http://grok-station.local:8787/enroll
/opt/grok-station/wake/venv/bin/python /opt/grok-station/wake/grok_wake.py --enroll hey_finn          # on the Pi
# top-up after "Needs 10 more clips" (trains on this session plus the previous one):
curl -H "Authorization: Bearer $TOKEN" -d '{"phrase":"hey_finn","topup":true}' http://grok-station.local:8787/enroll
/opt/grok-station/wake/venv/bin/python /opt/grok-station/wake/grok_wake.py --enroll hey_finn --topup
```

**Clips:**
- The default is **10 clips** (about 1 min). If training misses the recall gate, the station shows "Needs 10 more
  clips". A top-up records 10 more and trains on both sessions together.
- **Advanced:** `"count"` (10–40) overrides the default; a top-up needs 5 or more clips.
- Each clip goes like this: the screen says "Get ready…", a chime plays, then "Recording… speak now", and
  you say the phrase once, naturally.
- Vary it a little: normal and quiet voice, a little faster or slower, facing slightly away.
- **Timing per clip:** a 0.9 s get-ready pause (the warm ring fills over it), the chime, 2.4 s of recording, and
  a short "Got it" pulse. A rejected clip holds its message for 1.5 s, then retries.

**What the screen shows** (`docs/design.md`, screens 01–10): the persona's portrait (or the Grok mark for a wake
word without a persona) with a state ring and label, plus:
- a headline and sub-line;
- "Clip N of M" with a progress bar.

The screens are, in order:
1. Ready
2. Get ready (warm arc fills)
3. Recording (teal pulse + level bars)
4. Too loud
5. Didn't hear it (quiet ring + text, no red)
6. Saved (one teal pulse)
7. All clips saved / Uploaded ✓
8. Training queued (lavender spin, place in line)
9. "Hey Finn" is ready
10. Needs 10 more clips

The station polls `GET /enroll/<phrase>/<session>/status` on the relay every minute until training finishes. Then it
shows the pass or fail notice, waiting until any voice session has ended. The numbers stay on the training host.

All text auto-fits inside the safe area, up to 2 lines. Clips are trimmed to the speech and uploaded
as you go. When you're done, the session is marked ready and the training host picks it up within a minute.

Don't restart `grok-station-wake` during a session. The journal shows `enroll start …` and `enroll end …`.

### 5. Add a new wake phrase and map it to a persona

1. On the training host, add the phrase to `phrases.py`, the spellings the speech generator should say (for
   example `"hey_hank": ["hey hank"]`).
2. In `wake/wakewords.json`, add `{"name": "hey_hank", "persona": "hank"}` to the `enroll` list. Phrases are
   `hey_` plus 2–16 lowercase letters.
3. The persona slug must exist in the relay's private persona registry (`personas.json`, see Voice). If it doesn't,
   the relay ignores it and Grok answers.
4. Deploy `wakewords.json` to the Pi, then enroll the phrase as in step 4.

When a model passes, it's published with its own threshold and patience. That entry overrides any repo entry for
the same phrase, so no repo change is needed to start using it.

### 6. Read the results

Each job logs to `enroll/enroll.log` on the training host and writes `enroll/jobs/<phrase>-<session>/result.json`.
`enroll/status.json` lists the sessions as recording, ready, done or failed. A typical log:

```
[hey_finn 20261019-150740] JOB start (persona finn, 25 clips)
[hey_finn 20261019-150740] FEATURES real / TRAIN / EVAL real held-out
[hey_finn 20261019-150740] RESULT {"recall_real": 0.65, "fa_h": 0.654, "threshold": 0.95, "patience": 3, "passed": false, ...}
[hey_finn 20261019-150740] JOB DONE below the gate, not published
```

- **The gate:** recall is measured on a held-out 20 % of your real clips. Each clip is tested clean and mixed
  with background at 15, 10 and 5 dB. A model passes with `recall_real ≥ 0.80` at a threshold/patience setting
  that also has `fa_h ≤ 1.0` false wakes per hour on the validation set.
- **Pass:** "JOB DONE published". The station syncs the new model every 5 min for 3 h after an enrollment (and
  otherwise hourly, sha256 checked), then reloads between sessions.
- **Fail:** "below the gate, not published". Nothing changes on the station.

**Reduced-clip check:** `./enroll_check.sh enroll/inbox/<phrase>/<session> 10` re-trains a finished session on its
first 10 clips only, and evaluates it on the same held-out clips as the full run. The result is logged as
`JOB DONE check (pass|fail)` and never published.

### 7. Troubleshooting

- **"Too loud, step back a little":** more than 0.1 % of the raw mic samples hit full scale. This is checked
  before software gain, so the gain setting can't cause it. Step back or talk more softly.
- **"Didn't hear it, a bit louder please":** no speech above about −45 dBFS. Move closer, or raise the gain.
- **Gain:** `WAKE_GAIN` in the wake env sets the software gain. It defaults to 2 when `WAKE_DEVICE_NAME` contains
  EMEET, and 4 otherwise (the Razer headset mic runs quiet). Gain affects detection and the uploaded clips, but not
  the clipping check. Restart `grok-station-wake` after a change, but only between enrollments.
- **Recall below 0.80:** run a top-up (`--topup`, 10 more clips, same speaker, normal distance). It trains on
  the new clips plus the previous session's.
- **False wakes too high:** usually the phrase sounds like common speech. Pick a more distinctive phrase.
- **A job died or stalled:** look for `[watch] STALL` lines. A job that dies, or that has no file activity for
  30 min, is killed and restarted once. On the second failure the session is marked FAILED.
  - If a job died instantly with an empty log, check the `AbandonProcessGroup` / `KillMode=process` setting from
    step 2.
  - After fixing the cause, delete `FAILED` and `.tries` in the session's inbox folder, and the watcher retries it.
- **Nothing happens after a session:** check that `enroll/status.json` shows the session as `ready`. If it does,
  check the watcher with `launchctl print gui/$(id -u)/grok-enroll-watch` or
  `systemctl --user status grok-enroll-watch.timer`, and look at `enroll/watch.err`.

## Admin page (`http://grok-station.local`)
A small web page on the station (port 80, home network only) so nobody has to SSH in. Design: Noor's admin UI spec (Hearth theme). Service `grok-station-admin` runs as its own `grok-admin` user; the few root actions go through one allowlisted helper (`/usr/local/lib/grok-station/grok-station-helper`, sudoers entry for that file only).

**First run, no default password.** Until an admin password exists the station shows **Setup needed** with a one-time 6-character code (grouped 3+3, no 0/O/1/I; new on every boot until used) and voice stays off: a wake word gives the chime and no conversation, enrollment is refused. Open the page, type the code, choose a password (at least 10 characters, stored as a salted scrypt hash). Five wrong codes or passwords lock sign-in for 15 minutes.

**Setup wizard** (resumes where you left off; "Run setup again" in System): 1 code and password, 2 Wi-Fi (skip if wired; required when the station is on its setup hotspot), 3 xAI key (lives on the voice service computer, never on the station), 4 voice relay address + station token (tested before saving), 5 time zone, town/ZIP lookup, °F/°C, 6 wake words, 7 audio test, done.

**Pages:** Home (status, live screen preview, mic level, tiles) · xAI key and relay · Wake words (on/off, sensitivity, record a voice; speaker names stay on the station, training only sees ids like `spk-3f9a`) · Personas (read-only; the registry lives on the voice service computer) · Audio · Display · Time and location · Network (IPs, rename the station, scan, join, forget; the current connection stays up while joining) · System (update from GitHub, health, logs, restart voice, reboot, change password). Laptop sidebar, tablet icon rail, phone tab bar.

**Secrets are write-only.** The page never gets a stored secret back: it shows `••••` + last 4, Replace and Test. A new token only replaces the old one after it tests OK. Nothing secret in logs, URLs or browser storage.

**No network?** 90 s after boot with no connection and no saved Wi-Fi in range, the station starts its own hotspot (`grok-station-XXXX`) and shows QR codes to join it and open `http://10.42.0.1`. It never starts while connected and stops once home Wi-Fi is back.

**Forgot the password?** Physical access only, never by voice: either put an empty file named `grok-station-reset` on the SD card's boot partition and power on (the file is deleted after use), or run `sudo grok-station reset-admin`. Only the password is wiped; the station shows **Password reset** with a new code and every other setting is kept.

Install: `install.sh` runs `tools/install-files.sh` (also used by the page's updater). Tests: `python3 -m pytest tests/test_admin.py`.
Not yet: spoken setup lines need recorded clips in `wake/lines/` (chime only for now); the xAI key and personas are changed on the voice service computer; display brightness depends on the monitor.

## Credits

Inter and Inter Tight by The Inter Project Authors, SIL Open Font License 1.1 (`assets/fonts/OFL.txt`).


## Teammate handoff (off by default)
A persona can ask its own Grok Bot and speak the answer, answer-only (T0), with per-bot tokens, limits, nonce binding
and a privacy lint on both ends. Off until enabled per bot. See [docs/handoff.md](docs/handoff.md).

## Logo

Grok mark extracted from Wikimedia Commons `File:Grok-feb-2025-logo.svg` (X.AI Corp., PD-textlogo; trademark of xAI),
rendered white-on-transparent at 300 px. Source in `splash/grok-src.svg`, mark-only in `splash/grok-mark.svg`.
