# Display cards

A paired bot can put a recipe, a sports score, a calendar, or a home list on the station display. The card is data. The station draws it. It never runs it, and it never fetches a URL that is not on the logo allowlist.

`/voice/reply` is unchanged: a tier-0 answer only. Cards use `POST /voice/show` on the same admin HTTPS port. That port forwards the sealed body to the loopback relay, which checks the signature the same way it checks a reply, then posts the cleaned card to the station's existing `POST /show` (bearer token, localhost only for card ops). There is no new listening port.

## Envelope

Outer JSON, at most 8 KiB, same as a reply:

```json
{"v":1,"bot":"jules","sealed":"<base64>"}
```

`sealed` is a NaCl sealed box to the station public key. Inside is an Ed25519 signature over the show JSON below. The signed JSON has no `bot` field. The relay stamps the bot from the outer slug after the signature verifies. A bot can replace or clear only its own card id.

Put:

```json
{"v":1,"op":"put","id":"soup","priority":10,"ttl_s":3600,"kind":"show","nonce":"<b64url>","exp_ms":1700000000000,"card":{}}
```

Clear:

```json
{"v":1,"op":"clear","id":"soup","kind":"show","nonce":"<b64url>","exp_ms":1700000000000}
```

`kind` is the string `show`. A spoken reply is `kind` `reply` and is not accepted here, and a show is not accepted as a reply. `nonce` is URL-safe base64 (no padding) of at least 16 random bytes. `exp_ms` is a unix time in milliseconds. The relay rejects it when it is already past, or more than 120 seconds ahead. The same sealed blob posted again is rejected (`409`). The pair `(bot, nonce)` is remembered until it expires, in a small file on the relay.

After the card text is normalized, every text field is checked with the same secret lint as a spoken reply (API keys, tokens, SSNs, card numbers). A hit is `422` and the card is not shown. When `pii_mode` is on, calendar event titles and home item values are also checked with the reply PII lint. Other fields are not.

`id` matches `^[a-z0-9][a-z0-9_-]{0,31}$`. `priority` is an integer 0..100 (higher shows first). `ttl_s` is an integer from 1 to the station cap (default 86400, never above that, and a configured cap below 60 is ignored). Unknown fields are rejected.

`tools/grok-station-reply show` reads the show JSON on stdin, adds `kind`, a fresh `nonce`, and `exp_ms` (about 60 seconds ahead), seals it, and posts `/voice/show`. You do not put those three fields in the JSON you pipe. `seal`, `post`, and `send` are unchanged, except a reply now carries `"kind":"reply"`.

## Card v1

The card itself is at most 4096 bytes of JSON. `v` must be the integer 1. Any other `type` is rejected. Text is NFKC-normalized; markup and control or bidi characters are removed; the result is length-capped. Drawing uses that text as pixels, not as HTML.

| Type | Fields |
|---|---|
| `recipe` | `title` (60), `servings` 1..48, `ingredients` 1..12 of 48, `steps` 1..12 of 160, `step` 1..len(steps), optional `dish` (16, title case, for example `Orzo`; a malformed `dish` is dropped and the recipe still shows) |
| `sports` | `league` (24), `home` and `away` teams, `home_score` and `away_score` 0..999, `status` `live`\|`final`\|`scheduled`\|`delayed`, `period` (16), `schedule` 0..4 of `{label` 48, `when` 24}` |
| `calendar` | `date` `YYYY-MM-DD`, `events` 0..8 of `{time` 16, `title` 48}`. The display shows five rows. The first is next (teal), then a summary line |
| `home` | `title` (24), `items` 1..6 of `{label` 32, `value` 24, optional `trend` `up`\|`down}` |

A team is `{name` 24, `abbr` `^[A-Z0-9]{2,4}$`, either `logo_url` or `logo_id` or neither, and optional `logo_badge` true}`. On the sports card the logo is centered above the team name. If `logo_url` is missing, the name stands alone: no empty gap and no broken image. If a logo was asked for and is not ready, the station sets `logo_badge` and draws the abbreviation instead. A bot sends `logo_url` (https, at most 200 characters, a public host shape). The station stores a `logo_id` only after a successful cache write. `logo_badge` may only be the boolean true. Extra keys fail.

## Rotation and expiry

The deck keeps at most 24 cards, 8 per bot. A new id over either cap is rejected. Replacing the same `(bot, id)` keeps its sequence, so it stays in the same place in the order, and the screen repaints from the top of that order. Order is higher priority, then older card. The visible slot advances every `GROK_STATION_CARD_ROTATE` seconds (5..120, default 20), set on the Display page.

A card is live while `now < expires`. `ttl_s` is only that upper bound. Recipe, sports, and calendar are conversation cards: a new one replaces whatever conversation card was already up, even from another bot or another id, so turns do not pile up. Since 0.12.16 a sports or calendar card does not replace a recipe: the recipe stays the current card, and the new card shows as a chip on it until its ttl ends, it is dismissed ("dismiss" or "clear that"), or the recipe ends (see display.md "Bottom zone"). A recipe still replaces everything. Home cards are not conversation cards; they stay on the idle clock, up to three at a time, and still rotate. After the conversation goes idle, a sports or calendar card returns to the main display about 60 seconds later (`CONVO_IDLE_S`), and a recipe about 30 minutes after the last step command, or after idle when there has not been one (`RECIPE_IDLE_S`). Both are still capped by `ttl_s`. While the conversation is active only `ttl_s` applies. Setup, a voice session, and the night clock all win over cards. The recipe screen says “next step”. While that card is up, the station handles a short command itself, before the words go to a bot or to Grok: “next” or “next step”, “back”, “go back”, or “previous step”, “repeat”, “repeat that step”, or “what’s this step”, “start over” or “first step”, and “done” or “close recipe”. The match is the whole utterance (a leading “hey &lt;name&gt;” and a trailing “please” are fine). A question that only contains one of those words still goes to the bot. The step changes on the station, and the new step is read aloud in the card owner’s voice, or the station voice if that bot has none. “Next” on the last step says that’s the last step. “Previous step” on the first step says that’s the first step and the hint row does not change. “Done” or “close recipe” clears the card and leaves any kitchen timers running on the idle screen. The follow-up window stays open, and the command is not sent to the bot. A recipe’s idle clock is 30 minutes from the last step command. Put the next calendar event first. The sports screen keeps the next game in its own strip.

## Logos

Logos are loaded at runtime only, from HTTPS hosts listed on the Display page (at most 8). The list is stored in `logo-hosts.json` in the bots directory and copied to `GROK_STATION_LOGO_HOSTS` for the display. An empty list, or any illegal name, means no fetch. Hosts must be public DNS names. Addresses, `localhost`, and names ending in `.local`, `.lan`, `.internal`, `.localhost`, `.home.arpa`, or `.localdomain` are rejected.

When the station accepts a sports card it fetches logos itself and serves the files from its cache. The fetch is HTTPS only, host `a.espncdn.com` only, and only the `/i/teamlogos/<league>/<size>/<abbr>.png` path (the `/combiner/` resize URL is not that path). The host must also be on the allowlist, which it is by default until a list is saved. Both teams share one deadline of about a second (`LOGO_WAIT_S`). The card is shown when both logos are cached or when that deadline passes. A side that is not ready draws the abbreviation badge, never a broken image and never an empty gap. A side with no URL draws the name only. PNG or JPEG only (sniffed, not trusted from the header alone), at most 64 KiB and 512 px on a side, with no redirects. The station resolves the host once, keeps an address only when every answer is public, and connects to that address. The TLS certificate is checked against the hostname. If the peer is no longer that address, the download is dropped. SVG is rejected. The cache holds 32 files or 1 MiB, whichever comes first, under the station cache directory, mode 0644. A file older than 7 days is dropped and fetched again. A fresh cache hit does not fetch. Real team logos are never committed to this repo.

## Validation on the Pi

What changed: paired bots can put and clear display cards through the existing sealed path (`POST /voice/show`) and the station's existing `/show` listener. The Display page sets rotation seconds and the logo-host allowlist. Expired cards leave on their own.

How to test: install this tree, open **Display**, set rotation to 10 seconds, and leave logo hosts empty (or add only `cdn.example.com`). Pair a bot. From the bot machine, pipe a small recipe JSON (the put shape above, without nonce or exp) into `grok-station-reply show --bot jules --station-pub … --station https://…`. Then clear it with `{"v":1,"op":"clear","id":"soup"}`. Posting the same sealed bytes a second time is rejected.

Expected result: the recipe replaces the idle clock, the step line is plain text, and a sports card with a logo on any other host shows a monogram instead of a picture. After the ttl, or after clear, the clock comes back. Another bot's clear for the same id does nothing. The night clock and a voice session still cover the card.

## Never expose the admin port
The admin HTTPS port also carries `/voice/reply` and `/voice/show`. Never route it through cloudflared, any tunnel, or a router port forward. Only a computer on the home network posts to it.


## Bot updates

0.13.10. A short status line a bot puts on the station's own feed (not Team TV). It rides the same signed `POST /voice/show` envelope as a card (same seal, signature, `kind` `show`, nonce, `exp_ms`, replay set, rate limit and secret lint), so there is no new port, path or key.

Put (signed JSON, before `tools/grok-station-reply show` adds `kind`, `nonce` and `exp_ms`):

```json
{"v":1,"op":"update","id":"table","update":{"type":"done","line":"Booked the table for 7","time":"2026-10-30T18:00:00Z"}}
```

Clear: `{"v":1,"op":"update_clear","id":"table"}`.

| Field | Rule |
|---|---|
| `type` | `working_on`, `done`, `needs_you` or `heads_up` |
| `line` | 1 to 80 characters after cleanup; longer is cut at a word and ends with "…" |
| `time` | ISO 8601 with a zone |
| `expires_at` | optional, ISO 8601; Heads-up leaves then (2 h if missing). Ignored for Needs you |
| `feed_rule` | optional, up to 60 characters: the rule the bot keeps for its own feed, shown under its Updates switch. Empty means All updates |

Unknown fields are rejected. The relay refuses an update with `403` while the bot's Updates switch is off, and every bot starts off. Turning a bot off removes its updates from the station at once. The line and the rule get the secret lint, and the reply PII lint when privacy mode is on.

On the station: Needs you stays until the bot clears it or sends a new update with the same id. One Working on per bot (the next one replaces it). Done looks faded at 30 minutes and leaves at 2 hours; Working on and Heads-up without `expires_at` leave at 2 hours. The feed holds at most 8 per bot and 48 in total, in memory only. Updates are never spoken and never make a sound.
