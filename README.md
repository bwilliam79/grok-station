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
| `GROK_STATION_LAT` / `GROK_STATION_LON` | `36.19` / `-94.13` | Weather location |
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
  200 ok · 400 missing text/bad JSON · 401 bad token · 403 non-private client IP · 413 body > 8 KB.
- `GET /health`: 200.
- `POST /state` (**localhost only**, no token): `{"state": "listening"|"idle"}`. Used by the wake listener on the
  same Pi. `listening` shows the listening screen (teal tick ring, "Listening") for `GROK_STATION_LISTEN_HOLD`
  seconds (default 8), then idle. 403 from any other address.

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
"Hey Grok" model is trained. No audio leaves the Pi; there is no speech-to-text or cloud step yet.

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

## Credits

Inter and Inter Tight by The Inter Project Authors, SIL Open Font License 1.1 (`assets/fonts/OFL.txt`).

## Logo

Grok mark extracted from Wikimedia Commons `File:Grok-feb-2025-logo.svg` (X.AI Corp., PD-textlogo; trademark of xAI),
rendered white-on-transparent at 300 px. Source in `splash/grok-src.svg`, mark-only in `splash/grok-mark.svg`.
