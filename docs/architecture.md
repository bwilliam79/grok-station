# Architecture

A Raspberry Pi voice station. Everything runs on the Pi: the display, the "Hey Grok" wake listener, the admin page (HTTPS) and the voice relay (loopback only). Connecting a bot is in [bots.md](bots.md).

The short getting-started guide is the [README](../README.md). This page is what the Pi runs and what the installer puts on it.

## Requirements

- Raspberry Pi 4 with 2 GB RAM or more. A Pi 5 is recommended. A Pi 3B works as a small-hardware test.
- 64-bit Raspberry Pi OS Lite (Trixie). The installer stops on anything that isn't 64-bit (aarch64).
- A microSD card of 16 GB or more.
- A USB microphone and a speaker.
- A display attached to the Pi.
- An xAI API key.

## What the installer does

- Installs the system packages it needs with `apt-get`.
- Creates the service users and generates local secrets in `/etc/grok-station/secrets`. Secrets are never printed.
- Installs the display app, the voice relay and the Hey Grok wake listener (the relay and the wake listener each in their own Python venv) under `/opt/grok-station`.
- Sets quiet boot options in `cmdline.txt`, including `vt.global_cursor_default=0`, which hides the blinking console cursor. It adds `disable_splash=1` to `config.txt`, sets up the boot splash, and turns off the login prompt on the screen (tty1).
- Installs the `grok-station` command and creates a self-signed certificate for the admin page.
- Enables four systemd services: `grok-station`, `grok-station-wake`, `grok-station-relay` and `grok-station-admin`. On a fresh install they are not started; they start at the reboot.
- Runs a static health check: unit files enabled, config and tokens present, venvs and Python imports working. If something fails, the installer prints its last log lines and exits with an error.

Install commands, the reboot prompt, and updates are in [operations.md](operations.md).

## Services

| Service | What it runs |
|---|---|
| `grok-station` | The display |
| `grok-station-wake` | The Hey Grok wake listener |
| `grok-station-relay` | The voice relay (127.0.0.1 only) |
| `grok-station-admin` | The admin page (HTTPS on 443) |
| `grok-station-update` | One-off update started from the admin page (`journalctl -u grok-station-update`) |

```bash
systemctl status grok-station grok-station-wake grok-station-relay grok-station-admin
journalctl -u grok-station-admin -n 50 --no-pager      # swap in any service name
journalctl -u grok-station-wake -f                    # follow live
sudo systemctl restart grok-station-relay              # restart one service
```

## Where a bot key lives

**Bots** on the admin page adds a bot in steps (name and ID, key, webhook, then a summary). Add your main bot first. Voice is optional: leave it on **Station default** and change it later on the bot's row. The admin password is asked once, on the summary, and not again during the window set on **System** (every time, after 1–120 minutes, or not again this sign-in; default 10 minutes). Signing out, 30 minutes idle, or 12 hours ends it. The bot needs Grok Bot running on a home machine on the station's LAN to post replies. The key lives on the cloud computer (default: seal there, post from the home machine) or, on a Mac, on the home machine (gs-seal-reply, one command, about 3 seconds faster; opt-in in Add bot since 0.8.2; cloud stays the default). A key exists on only one machine. The station display does not keep a separate relay icon. The Grok icon in the top-right cluster covers the relay and the Grok service: teal when both are fine, amber with an exclamation badge when the relay or the service is down (the admin home says which), amber with a slash when there is no internet, and dimmed when the network itself is down. Until a bot is paired, the relay is not treated as a fault.

A handoff says "Asking <name>…" from a clip cached for that bot's name and voice: at the end of speech on a direct wake, and when the handoff starts if Grok routes the question. The wait for the answer is unchanged. See [handoff.md](handoff.md).

Say "set a pasta timer for ten minutes" to start one. Up to eight can run at once. They show as countdown rings, and a finished one pulses amber until you say "stop" or "stop the pasta timer". See [timers.md](timers.md).

The **main bot** (starred on **Bots**) is where the bot list comes from. The first bot you add becomes the main bot; to change it, open a bot with **Configure** and pick **Set as Main Bot**. Say "Hey Grok, import my team" (also "import the rest of my team", "import the rest of my bots", and "update my team list") and the station asks the main bot for names. A name that is already a bot is not added again. Each name that is not a bot yet is a row on Bots, state **Not set up**, with **Set up** (Add bot, name filled in) and **Remove**. **Copy team setup message**, on that list and on the main bot's Configure, copies one message for the main bot to send each teammate. The station sends nothing. You paste the webhook URL, webhook key, webhook header and public keys into the station yourself. The list never changes a bot that is already configured, and the station does not run anything from it. See [bots.md](bots.md).
