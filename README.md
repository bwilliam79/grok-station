# grok-station

Raspberry Pi 5 (Pi OS Lite 64-bit, Trixie, no desktop/audio/mic) kiosk:

1. **Boot splash** — Plymouth `script` theme: centered Grok mark on black with a slow opacity pulse.
2. **Siri proof of concept** — one Python process (`grok-station.service`) that runs a tiny stdlib HTTP
   listener on `:8787` and draws fullscreen text by rendering with pygame off-screen and writing straight to the `/dev/fb0` framebuffer (no X/Wayland/browser; SDL KMS/DRM frames never reached the screen on the Pi 5).

## Layout

| Path | Installed to |
| --- | --- |
| `splash/grok.plymouth`, `grok.script`, `grok.png` | `/usr/share/plymouth/themes/grok/` |
| `app/grok_station.py` | `/opt/grok-station/grok_station.py` |
| `systemd/grok-station.service` | `/etc/systemd/system/` |
| (token, not in repo) | `/etc/grok-station/env` (root:root 600): `GROK_STATION_TOKEN=...` |

`install.sh` (run as root on the Pi) installs packages (plymouth, python3-pygame, Mesa EGL/GBM), copies files,
appends `quiet splash loglevel=3 vt.global_cursor_default=0 logo.nologo consoleblank=0 plymouth.ignore-serial-consoles`
to `/boot/firmware/cmdline.txt` (single line), adds `disable_splash=1` to `config.txt`, runs
`plymouth-set-default-theme -R grok` (rebuilds initramfs), masks `getty@tty1` (kiosk; use SSH), and enables the service.

The service starts before `plymouth-quit`, runs `plymouth quit --retain-splash`, then takes the display.
It runs as `grok` (groups video/render/input).

## API

- `POST /show` — header `Authorization: Bearer <token>`, JSON `{"text": "..."}` (max 500 chars).
  200 ok · 400 missing text/bad JSON · 401 bad token · 403 non-private client IP · 413 body > 8 KB.
- `GET /health` — 200.

Each render is also saved to `/run/grok-station/last.png` for remote verification.

```sh
curl -H "Authorization: Bearer $TOKEN" -d '{"text":"hello"}' http://grok-station.local:8787/show
```

## Apple Shortcut ("Show on Grok Station")

1. Dictate Text (or Ask for Input → Text).
2. Get Contents of URL: `http://grok-station.local:8787/show` (use the Pi's LAN IP if `.local` fails),
   Method POST, Header `Authorization` = `Bearer <token>`, Request Body JSON, key `text` = Dictated Text.
3. Name it "Show on Grok Station" → "Hey Siri, show on Grok Station".

## Logo

Grok mark extracted from Wikimedia Commons `File:Grok-feb-2025-logo.svg` (X.AI Corp., PD-textlogo; trademark of xAI),
rendered white-on-transparent at 300 px. Source in `splash/grok-src.svg`, mark-only in `splash/grok-mark.svg`.
