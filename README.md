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
`pygame.event.wait`. Weather is from [Open-Meteo](https://open-meteo.com/) (no key), refreshed every 15 min,
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
crash can't take down the display. It reads 16 kHz mono from the USB headset mic via `arecord` (device picked by
name, `WAKE_DEVICE_NAME`, default `Razer`), runs [openWakeWord](https://github.com/dscripka/openWakeWord) with ONNX
Runtime, and on a hit (score ≥ 0.5, 2 s cooldown) plays a 200 ms two-tone chime on the headset and POSTs
`{"state":"listening"}` to `http://127.0.0.1:8787/state`. Mic level and peak score are logged every 30 s.

**The wake phrase is "Hey Jarvis"**, a pre-trained openWakeWord model used as a placeholder until a custom
"Hey Grok" model is trained. Wake detection is fully local; audio leaves the Pi only after a wake, and only to
the voice relay below (when configured).

Setup on the Pi (venv, since openWakeWord's tflite dependency has no Trixie arm64 wheel):

```sh
sudo install -d -o grok -g grok /opt/grok-station/wake
python3 -m venv /opt/grok-station/wake/venv
/opt/grok-station/wake/venv/bin/pip install --no-deps openwakeword
/opt/grok-station/wake/venv/bin/pip install onnxruntime numpy tqdm scipy scikit-learn requests
/opt/grok-station/wake/venv/bin/python -c 'import openwakeword.utils as u; u.download_models(["hey_jarvis"])'
sudo install -m755 wake/grok_wake.py /opt/grok-station/wake/
sudo install -m644 systemd/grok-station-wake.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now grok-station-wake
```

Optional overrides in `/etc/grok-station/wake.env`: `WAKE_MODEL`, `WAKE_THRESHOLD`, `WAKE_COOLDOWN`,
`WAKE_DEVICE_NAME`, `WAKE_STATE_URL`, `WAKE_GAIN` (software gain, default 4), `WAKE_CAPTURE_RATE` (default 48000;
audio is captured at the device's native rate and decimated to 16 kHz in software, because some USB headsets,
including the Razer USB Sound Card, deliver muffled audio in their 16 kHz mode and the wake word never scores). Offline check: `grok_wake.py --test clip.wav` (16 kHz mono) prints the peak
score. On a Pi 5 the listener uses about 11% of one core and ~200 MB RSS.

## Voice (proof of concept)

One question and answer per wake: "Hey Jarvis" → chime + **listening** → speak → **thinking** (Grok started a
response) → **speaking** (reply plays on the headset, text on screen) → idle. Mic audio keeps streaming until a
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
  `portrait` (file name), `prompt` (≤ 200 words), `facts` (≤ 10 lines of ≤ 140 chars; links, @, 6+ digit runs, IPs,
  `.lan/.local/.internal` names and hidden characters are rejected), `on_voice`. Unknown fields or a failed check skip
  the entry (logged by slug). The file is re-read when it changes. Every persona prompt gets a fixed safety line from
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
    `personas.json` there: `/show` can't post as Grok or any persona name, slug or alias, and `.jpg` portraits work.
- End-to-end check without talking: `grok_wake.py --ask question.wav [reply.wav]` (24 kHz mono) streams the
  file through the same path, plays the reply, and prints the end-of-speech → first-audio latency.

## Credits

Inter and Inter Tight by The Inter Project Authors, SIL Open Font License 1.1 (`assets/fonts/OFL.txt`).

## Logo

Grok mark extracted from Wikimedia Commons `File:Grok-feb-2025-logo.svg` (X.AI Corp., PD-textlogo; trademark of xAI),
rendered white-on-transparent at 300 px. Source in `splash/grok-src.svg`, mark-only in `splash/grok-mark.svg`.
