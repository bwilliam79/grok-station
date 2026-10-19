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
rectangle once a second). While a voice session is **thinking** the violet ring spins (one turn per 1.2 s, `GROK_STATION_SPIN_FPS`, default
15) by repainting only the portrait rectangle from a cached circle-cropped photo. The colon is centered between the
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
  Used by the wake listener on the same Pi. States: `listening` (teal tick ring, `GROK_STATION_LISTEN_HOLD` s,
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
screen matches Grok's: same tick ring, with their portrait (Grok's is the logo). Without a config file the old single
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
`WAKE_DEVICE_NAME`, `WAKE_STATE_URL`, `WAKE_GAIN` (software gain, default 4), `WAKE_CAPTURE_RATE` (default 16000;
set it to the device's native capture rate, see `/proc/asound/cardN/stream0`. The EMEET mic is 16 kHz only, so it
is used as is and upsampled ×1.5 to 24 kHz for the voice session. Use 48000 for devices whose 16 kHz mode is poor,
such as the Razer USB Sound Card (muffled, the wake word never scores); 48 kHz is decimated to 16 and 24 kHz).
Playback is 48 kHz stereo on the EMEET; `aplay` via `plughw` converts the 24 kHz mono reply. Offline check: `grok_wake.py --test clip.wav` (16 kHz mono) runs the clip through
the same detector and prints which word and persona it triggers plus every word's peak score (no session is
opened). `grok_wake.py --bench [seconds]` times the loaded models on noise. On a Pi 5 the listener used about 12%
of one core with Hey Jarvis alone; `--bench` measures 10.6% of one core for all eight models (seven custom words plus the Hey Jarvis fallback) vs 9.4% for Hey Jarvis alone (the shared audio front end dominates). The spinning ring renders one revolution once (about 66% of a core for 1.2 s), then reuses those 18 frames at about 6% of a core; a colon blink costs about 2 ms.

## Voice (proof of concept)

One question and answer per wake: "Hey Grok" (or a teammate's wake word) → chime + **listening** → speak → **thinking** (Grok started a
response) → **speaking** (reply plays on the speakerphone, text on screen) → idle. Mic audio keeps streaming until a
response actually starts, so a short noise or the tail of the wake word (which can trip the server VAD without
producing a reply) doesn't swallow the real question. The first `VOICE_SKIP_MS` (default 300) of post-wake audio
is dropped for the same reason. Nothing heard (or only noise) for 10 s → "Didn't catch that." and idle.

```
Pi: grok_wake.py ──LAN wss, client cert + device token──▶ tls (Caddy) ──▶ relay/ ──▶ wss://api.x.ai/v1/realtime
     mic PCM16 24 kHz ▲  reply PCM16 24 kHz + JSON events ▼   (Docker, any LAN host)   grok-voice-3.0, server_vad
```

- **The xAI API key lives only on the relay host**, never on the Pi. The Pi holds a random device token.
- `relay/relay.py` (aiohttp): `GET /ws` (`Authorization: Bearer $RELAY_TOKEN`) opens one xAI realtime session,
  forwards mic audio up and reply audio, transcripts and turn events down, then closes after the reply, after
  10 s with no speech in progress and no response (counted from the last end of speech), or at 60 s (cost guard;
  realtime voice bills by the minute). One session at a time. The instructions get the current local date and
  time (`TZ_NAME`, default America/Chicago); the optional `keyterms` secret (comma-separated) biases
  transcription toward names the station should recognize.
  `GET /health` returns `{"ok":true}`; with the token it adds key presence, today's usage and the caps. Logs carry
  event types and durations only, no transcripts or audio.
- **Guards:** requests with an `Origin` header (any browser) get 403; one session at a time (409); daily caps
  `MAX_SESSIONS_DAY` (40) and `MAX_MINUTES_DAY` (20), counted per America/Chicago day in `/state/usage.json` so
  restarts don't reset them, return 429; 5 bad tokens in 10 min lock the source out for 15 min (429). Behind the
  `tls` front the source is the **client certificate** (Caddy sets `X-Client-Cert-Fp`; the relay trusts it only
  from the `tls` container), so bad tries with one certificate can't lock out the station; direct loopback callers
  are keyed by address. Upstream audio is capped at `MAX_UP_SECONDS` (65) per session. 80% / 100% of a cap and
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

Records 10-40 clips (default 25) of one phrase on the speakerphone, uploads them to the training host through the
relay, and trains a model from real + synthetic speech. Architecture: `docs/enroll.md`; host scripts: `train/`.
Never started by voice. Start it from the LAN with the `/show` token, or on the Pi:

```sh
curl -H "Authorization: Bearer $TOKEN" -d '{"phrase":"hey_finn","count":25}' http://grok-station.local:8787/enroll
/opt/grok-station/wake/venv/bin/python /opt/grok-station/wake/grok_wake.py --enroll hey_finn 25   # on the Pi
```

Phrases: any word in `wakewords.json` plus its `enroll` list (persona per phrase). The screen shows the phrase, "Clip N
of M", a progress bar and the status (get ready → chime → recording; too quiet or clipped clips are retried). Clips
are trimmed to the speech, kept locally in `/opt/grok-station/wake/enroll/<phrase>/<session>/`, and posted to the relay
(`POST /enroll/<phrase>/<session>/<NN>.wav`, then `.../done`; mTLS + relay token; 16 kHz mono WAV, ≤ 5 s, ≤ 40 per
session). The relay writes them to `$GROK_STATION_ENROLL` on the training host, where `train/enroll_watch.sh`
(launchd, every 60 s) starts `enroll_train.sh` under `nice 10` + `caffeinate`: synthetic set (generated once for a new
phrase), 80 % of the real clips augmented ×200 into the positives, training, then the gate: false wakes ≤ 1.0/h on the
validation set and recall ≥ 0.80 on the held-out real clips (clean + 15/10/5 dB background). A passing model and its
threshold/patience are published to `$GROK_STATION_WAKEWORDS`; the relay serves them (`GET /wakewords`,
`GET /wakeword/<name>.onnx`). The Pi syncs them at startup, hourly, and every 5 min for 3 h after an enrollment
(`/opt/grok-station/wake/models/enrolled/`, sha256 checked) and reloads the detector between sessions; an enrolled
model overrides the repo entry for that phrase. The watcher is also the job's watchdog (dead job or 30 min without
log/file activity → killed, restarted once, then marked FAILED in `enroll/enroll.log`).

## Credits

Inter and Inter Tight by The Inter Project Authors, SIL Open Font License 1.1 (`assets/fonts/OFL.txt`).

## Logo

Grok mark extracted from Wikimedia Commons `File:Grok-feb-2025-logo.svg` (X.AI Corp., PD-textlogo; trademark of xAI),
rendered white-on-transparent at 300 px. Source in `splash/grok-src.svg`, mark-only in `splash/grok-mark.svg`.
