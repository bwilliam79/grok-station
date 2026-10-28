# Teammate handoff

A paired bot can answer on the station. Handoff is on unless `HANDOFF_ENABLED=0`. A bot is reachable only when it has both a webhook and a pinned verify key. There is no separate relay host and no compose file. See [bots.md](bots.md) for pairing, the wire format, and the routine.

## Flow

1. The station asks a bot in one of three ways: the wake word or a direct persona session, a spoken name from Grok ("ask <bot> …"), or the model's `check_notes` tool. The relay offers `check_notes` only to a persona that already has a route.
2. Limits still apply (one outstanding question per bot, plus the daily caps in `relay/handoff.py`). The question is cleaned (NFKC; control, zero-width and bidi characters dropped; length cap) and stored on a pending file with `pii_mode`, `tier_max` 0, `reply_by`, and an accept-until time. `pii_mode` is not sent to the bot.
3. One POST goes to that bot's webhook and is never retried. The body is `v`, `request_id`, `persona`, `question`, and `reply_by` (about 60 seconds ahead, ISO UTC). An admin test adds `test: true`. Nothing else is on the wire.
4. The bot answers with `POST /voice/reply` on the admin HTTPS port: `{"v":1,"bot","sealed"}`. The relay unseals, checks the pinned signature before parsing, then checks the schema, the open pending request, and the lint. The text is written for the waiting ask. Duplicate and expired requests are rejected.
5. The station waits `REPLY_WAIT` seconds (default 30). The pending file stays valid for two minutes so a skewed clock does not fail a reply the station is still waiting for. On timeout the station says `I couldn't reach <Name>.`

The screen shows the thinking ring during that wait. The same thinking state plays the waiting tone (a soft chime, after a short delay, until speech starts). The spoken asking clip is separate and is described below. It stops the tone while it plays; the tone starts again only if the ring is still thinking.

## Asking line

The station speaks `Asking <Name>…` once per utterance, on every handoff: a direct wake, a spoken name from Grok, `talk_to`, and `check_notes`. It does not speak that line for Grok's own answers, for a bot with no webhook, or when a cap would refuse the ask (nothing is waiting).

The clip is one xAI TTS render of that fixed line, stored as `voice/ack/<slug>-<sha256 of name|voice>.pcm` under the state directory (`ACK_DIR`, mode 0640). The installer creates the directory. The relay unit already has the state directory on `ReadWritePaths`, so no new path is opened. A rename or a voice change deletes the old file; the next persona reload renders the new line when an xAI key is set. A missing file is rendered on first use. If TTS fails, nothing is cached and the handoff still runs.

A woken webhook persona sends the cached clip when speech stops, before model audio and before the webhook. A Grok route sends it at the start of the handoff, before the webhook, unless this utterance already played it. The Pi plays it on the same one-clip-at-a-time player as a `handoff_still` message, so the two cannot overlap, and logs `ack played X.XXs after end of speech`. The clip is not model audio, so the screen does not flip to speaking. The wait no longer makes a second live TTS of the same line. `REPLY_WAIT` is unchanged: it starts when the webhook POST succeeds, not when the ack plays. A direct wake cannot see an empty transcript at the moment speech stops, so a cough can play the clip. Grok does not play it until the turn is actually a route.

## Reply lint

Spoken replies are linted in `relay/handoff.py`. An exception inside the lint fails closed. Logs are verdict and rule code only. Reply text is never logged.

Normalization before the lists: NFKC, lowercase, spelled-out digits, then spaces, dashes and dots between digits are removed.

**Always blocked:** a password, passcode, pin, token, API key or secret that has a value; key prefixes `sk-`, `xai-`, `ghp_`, `github_pat_`, `AKIA`, `Bearer` plus a value, and a PEM private key; a high-entropy token of 32 or more characters; an SSN (including one spelled out); a card number of 13–19 digits that passes Luhn (including one spelled out).

**Blocked only when `pii_mode` is true:** household names from the private registry, email addresses, phone numbers, street addresses, IP addresses, MAC addresses, and private host names (`.lan`, `.local`, `.internal`, `home.arpa`, `localdomain`, `localhost`, plus the registry's private host names).

A bare link, a short digit run, money, a VIN, medical wording, or the word "password" with no value all pass. With `pii_mode` false an email may be spoken. An SSN is still not.

`pii_mode` lives in the bots directory `policy.json` (mode 600), `{"pii_mode": true}` and nothing else. Missing or invalid means true.

## T1

Off unless `T1_ENABLED=1`. The relay does not authorize a write. It can show a proposed note and listen for a confirm at the station (`relay/t1.py`). Bots still may not send anything but a tier-0 answer on `/voice/reply`. Display cards are not replies: a bot posts them to `POST /voice/show` with the same sealed signature. See [cards.md](cards.md).

## Validation on the Pi

What changed: a handoff says "Asking <name>…" from a clip cached for that bot's name and voice. A direct wake plays it once the words show the turn goes to the bot, so a goodbye never says it. A question Grok routes (spoken name, `talk_to`, or `check_notes`) plays it when the handoff starts. The wait for the answer is the same length as before. No new listening port.

How to test, after an update (the installer creates `voice/ack` on a re-run):

1. Restart `grok-station-relay` and `grok-station-wake`, or reboot.
2. Ask a paired bot directly, for example "Hey <bot>, what time is it".
3. In `journalctl -u grok-station-wake -n 80 --no-pager`, expect `ack played 0.XXs after end of speech` (a cached clip is about 0.3 s or less) and then `relay: handoff wait`.
4. Ask Grok to hand off, for example "Hey Grok, ask <bot> what time it is". The same ack line should show after the route, still before the reply.
5. Rename the bot, or change its voice, and ask again. The spoken name and voice should match the new settings. The previous `*.pcm` under `/var/lib/grok-station/voice/ack/` for that bot should be gone.

Expected result: one "Asking <name>", with no second copy and no overlap with the reply. The round trip stays about the same length. `ss -tlnp` shows no new listener; the relay is still loopback port 8790 only.
