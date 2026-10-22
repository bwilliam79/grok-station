# Teammate handoff

A paired bot can answer on the station. Handoff is on unless `HANDOFF_ENABLED=0`. A bot is reachable only when it has both a webhook and a pinned verify key. There is no separate relay host and no compose file. See [bots.md](bots.md) for pairing, the wire format, and the routine.

## Flow

1. The station asks a bot in one of three ways: the wake word or a direct persona session, a spoken name from Grok ("ask <bot> …"), or the model's `check_notes` tool. The relay offers `check_notes` only to a persona that already has a route.
2. Limits still apply (one outstanding question per bot, plus the daily caps in `relay/handoff.py`). The question is cleaned (NFKC; control, zero-width and bidi characters dropped; length cap) and stored on a pending file with `pii_mode`, `tier_max` 0, `reply_by`, and an accept-until time. `pii_mode` is not sent to the bot.
3. One POST goes to that bot's webhook and is never retried. The body is `v`, `request_id`, `persona`, `question`, and `reply_by` (about 60 seconds ahead, ISO UTC). An admin test adds `test: true`. Nothing else is on the wire.
4. The bot answers with `POST /voice/reply` on the admin HTTPS port: `{"v":1,"bot","sealed"}`. The relay unseals, checks the pinned signature before parsing, then checks the schema, the open pending request, and the lint. The text is written for the waiting ask. Duplicate and expired requests are rejected.
5. The station waits `REPLY_WAIT` seconds (default 30). The pending file stays valid for two minutes so a skewed clock does not fail a reply the station is still waiting for. On timeout the station says `I couldn't reach <Name>.`

The screen always shows the thinking ring. The spoken cue "Asking <Name>…" is only for a name route from Grok. A direct wake and `check_notes` do not speak that cue.

## Reply lint

Spoken replies are linted in `relay/handoff.py`. An exception inside the lint fails closed. Logs are verdict and rule code only. Reply text is never logged.

Normalization before the lists: NFKC, lowercase, spelled-out digits, then spaces, dashes and dots between digits are removed.

**Always blocked:** a password, passcode, pin, token, API key or secret that has a value; key prefixes `sk-`, `xai-`, `ghp_`, `github_pat_`, `AKIA`, `Bearer` plus a value, and a PEM private key; a high-entropy token of 32 or more characters; an SSN (including one spelled out); a card number of 13–19 digits that passes Luhn (including one spelled out).

**Blocked only when `pii_mode` is true:** household names from the private registry, email addresses, phone numbers, street addresses, IP addresses, MAC addresses, and private host names (`.lan`, `.local`, `.internal`, `home.arpa`, `localdomain`, `localhost`, plus the registry's private host names).

A bare link, a short digit run, money, a VIN, medical wording, or the word "password" with no value all pass. With `pii_mode` false an email may be spoken. An SSN is still not.

`pii_mode` lives in the bots directory `policy.json` (mode 600), `{"pii_mode": true}` and nothing else. Missing or invalid means true.

## T1

Off unless `T1_ENABLED=1`. The relay does not authorize a write. It can show a proposed note and listen for a confirm at the station (`relay/t1.py`). Bots still may not send anything but a tier-0 answer on `/voice/reply`.
