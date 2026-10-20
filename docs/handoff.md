# Teammate handoff (real-bot pilot)

A persona on the station can ask its own Grok Bot ("check my notes") and speak the answer. It is **off by default**:
`HANDOFF_ENABLED=0` in `relay/compose.yaml`, no webhooks, and tile-ingest `VOICE_ENABLED=0`. Each bot goes live only
after its own canary passes and the owner OKs it.

## Flow
1. The persona calls `check_notes(question)`. The relay offers the tool only to a persona that has its own webhook.
   A wake word or a spoken name that lands on a persona with a webhook does not wait for that tool: the relay calls
   the handoff itself on the final transcript. See [Persona route](#persona-route-no-general-model-answer).
2. The relay applies limits (per bot: 8/day, at least 30 s apart, one outstanding; plus a global daily total). A failed
   POST still counts. The relay then cleans the question (NFKC; control, zero-width and bidi characters dropped; 500-char
   cap) and writes `voice/pending/<request_id>.json`. That file is the relay → tile-ingest channel (a read-only mount
   on the tile-ingest side; no bot token). It holds the cleaned question, `pii_mode` (the relay's setting at issue
   time), `tier_max` 0, `test` (bool), `reply_by` (ISO), `expires_ms`, the persona, a random nonce and `v`.
3. One POST goes to that bot's own webhook, and it is never retried. The body is only
   `{source:"grok-station", v:2, request_id:"vs_…22", persona}`. An admin Test adds `"test": true`. An avatar redraw
   request also adds `"include_avatar": true`. There is no question, no `pii_mode`, no `reply_by` and no `tier_max`
   on the wire. The bot reads the question from tile-ingest `GET /voice/request/<request_id>` (tile-ingest serves that
   from the pending file).
4. The routine answers through tile-ingest `POST /voice/reply` `{request_id, text, tier, action_class}` with its own
   voice token. tile-ingest checks the schema, tier scope, privacy lint and pending binding, then writes `voice/replies/`.
5. The relay reads the reply once (it deletes the file on read) and checks the nonce, scope and lint again. It speaks
   the reply only if the session is still waiting. Otherwise it drops it, and a late reply finds no pending file.

The Pi shows "Checking with <name>…" while it waits.

## Persona route (no general-model answer)

When routing picks a persona that has its own webhook — the wake word at session start, or a spoken name mid-session
(`ask <Name> …`, `<Name>, …`) — the realtime model generates no response for that turn. The relay sets
`turn_detection.create_response` false (and `interrupt_response` true), never sends `response.create` for the turn, and
cancels and drops any `response.*` audio or text that still arrives. None of that is forwarded to the station.

On the final transcript the relay runs the same handoff as `check_notes` (limits, nonce, lint, the current `pii_mode`). It speaks
only a fixed filler, "Asking <Name>…", in the persona's voice (xAI TTS), the existing "Still asking <Name>." at 20 s,
then the bot's linted reply via TTS. If there is no answer it speaks a fixed line, for example "<Name> didn't answer
in time; try again or ask in the app.", or the other honest error lines. The model does not word any of it.

A persona with no webhook keeps the previous behavior, and so does Grok. If model audio or text shows up on a routed
turn anyway, the relay logs `canary persona-route model-output`, sends the usual ntfy alert, and drops it. The log is
the verdict only, never the text.

## Reply lint (always-blocked, plus PII when pii_mode is on)

There is no guest mode on the relay. A stale `guest` key in `policy.json`, the limits file, or a persona entry is
ignored at startup and is not written back. Spoken-reply lint is the same structural checks as before (request id,
nonce, persona binding, schema, length cap, T0, the handoff caps) plus two content lists. Nothing else is
content-based: a bare link, a short digit run, money, a VIN, medical wording, or the word "password" with no value
all pass.

Before the content lists, the reply is normalized: NFKC, lowercase, spelled-out digits (`zero`/`oh`/`one`…`nine`),
then spaces, dashes and dots between digits are removed. An exception inside the lint fails closed (the reply is
dropped as `error`).

**Always blocked**, whether or not `pii_mode` is on:

- a password, passcode, pin, token, API key or secret that has a value (`password is …`, `pin: …`)
- key prefixes `sk-`, `xai-`, `ghp_`, `github_pat_`, `AKIA`, `Bearer` plus a value, and a PEM private key
- a high-entropy token of 32 or more characters
- an SSN (including one spelled out)
- a card number of 13–19 digits that passes Luhn (including one spelled out)

**Blocked only when `pii_mode` is true:** household names from the private registry, email addresses, phone numbers,
street addresses, IP addresses, MAC addresses, and private host names (`.lan` / `.local` / `.internal` /
`home.arpa` / `localdomain` / `localhost`, plus the registry's `private_hostnames`). With `pii_mode` false an email
is spoken; an SSN is still not.

The persona safety prompt is one text: "don't read out passwords, keys, codes, SSNs or card numbers", and, when
`pii_mode` is on, "or personal details like addresses, phone numbers or emails". The registry lint (persona name,
prompt, facts) is unchanged; it is not the spoken-reply gate. Logs stay verdict and rule code only. Reply text is
never logged.

T1 notes keep their own station guest gate (`relay/t1.py`, the Pi's `t1` query). That gate is not this lint.

## pii_mode (service-owned)

`pii_mode` lives in `<bots dir>/policy.json` (mode 600), `{"pii_mode": true}` and nothing else. Missing, unreadable,
or a non-boolean value means **true**. The relay also writes the same object to `/voice/minted/policy.json` (mode 644,
atomic replace) at startup and whenever the setting changes, so tile-ingest can read it from the minted mount.

`GET /admin/policy` returns `{"pii_mode": bool}`. `PUT /admin/policy` with `{"pii_mode": bool, "actor": "…"}` writes
both files and appends an audit line `<ts> <actor> - pii-mode on|off -`. Same mTLS and admin token as the other
`/admin` routes. The step-up password is the Pi admin's job, before it calls this.

There is no HTTP factory-reset route. To put the switch back to on, delete `policy.json` and restart the relay, or
call `factory_reset_policy()` (it writes `true` to both files). Startup runs `ensure_policy()`, so a missing file
becomes an explicit `true` on disk.

## Avatars

`POST /voice/avatar` is how a bot uploads its portrait. Headers: `Authorization: Bearer <voice token>` and
`X-Grok-Persona: <slug>`. The token is valid when `sha256` matches a hash stored for that slug in the relay-minted
file (constant-time compare). The body is raw `image/png`, `image/jpeg` or `image/webp`, at most 2 MB, or JSON
`{"image_b64": "…"}`. Pillow decodes it with a pixel cap, centre square-crops, resizes to 512×512 and re-encodes a
PNG with no metadata. The file is written atomically to the portraits writable dir as `<slug>.png` (mode 644).
`GET /portrait/<slug>` serves that file before the read-only portrait dirs. Six uploads per slug per hour.
`401` bad token, `404` unknown slug, `413` too large, `415` not an image, `429` over the hourly cap. The audit line
is `<ts> bot <slug> avatar-upload <sha8>` (first 8 hex chars of the PNG's sha256). No image bytes and no token.

The public TLS front exposes this path on `:8444` (`GROK_STATION_AVATAR_PORT`, default host port 8792) with TLS and
**no** client certificate. Everything else on that listener is 404. mTLS on `:8443` is unchanged.

The admin API (mTLS + admin token), used by the station page:

| Method | Path | Body | Result |
| --- | --- | --- | --- |
| POST | `/admin/bots/<slug>/avatar` | raw image, or JSON `image_b64` + `actor` | same transcode; a raw upload with no `X-Actor` is actor `admin`; not counted in the bot's 6/hour |
| POST | `/admin/bots/<slug>/avatar-request` | `{"actor": "admin"}` | 202 `{test_id}`. A test-flagged webhook (`test` and `include_avatar` both true). Own request id, same caps, cannot resolve some other request |

`GET /personas` includes `portraits_version`, a counter bumped on every successful write, so a client can tell that a
portrait changed.

## Audit log

Each UTC month is its own file, `audit-YYYY-MM.log`, in the bots dir. The relay opens it `O_APPEND|O_WRONLY` and
never reads, rewrites, truncates, renames or deletes it. An older `audit.log` is left untouched. The same line is
appended to `<mirror>/audit-YYYY-MM.log` when the mirror directory already exists. `AUDIT_MIRROR_DIR` (preferred,
default `/voice/audit`) names that directory. `AUDIT_MIRROR` still works: a value that ends in `.log` uses its
directory, and any other value is the directory. Compose mounts `${GROK_STATION_AUDIT:-./voice/audit}` at
`/voice/audit` (read-write). Create that directory writable by the relay uid (10001) and readable by tile-ingest.
If it is absent the relay skips the mirror and does not create `/voice`.

The mode (600 in the bots dir, 644 on the mirror) is set only when this write creates the file. `chmod` and
`fchmod` errors `EPERM`, `EACCES` and `ENOTSUP` are ignored, so the line is still written. Tile-ingest sees the
mirror read-only at `/voice/audit`.

Host-level append-only flags (`chflags sappnd`/`schg`) are not used. Colima's virtiofs share does not pass
`O_APPEND` through, so `sappnd` blocks every container write (tested Oct 20, 2026). The owner decided (Oct 20) not
to add a finished-month lock job; the audit lockdown is the relay fix only, with no file flags.

Lines carry actor, slug, action and a last-4 or an 8-char hash. Never a secret, a URL, a question, or reply text.

## Tiers
T0 is `answer` only. T1 adds `note` (reversible internal writes after an on-screen read-back and confirm). T2 adds
`queue_approval` (an approval card for the owner, never executed from voice). Every bot ships at **T0**. A reply above a
bot's tier gets 403 at tile-ingest and is dropped at the relay. Purchases, payments, vehicle, garage and lock control,
security and config changes, credentials, and personal data are never available by voice at any tier.

## Logs
Logs and alerts carry request_id, agent, length and verdict only, never text. Every 422/403 alerts to ntfy.

## Installing a bot (after its canary)
1. tile-ingest: `node bin/gen-voice-token.js <bot>` stores the hash only. Hand the plaintext from the mode-600 handoff
   file to the bot, then delete that file.
2. The bot creates its routine with the shared prompt (step 0 checks, delimiters, data allowlist, guest rule, T0, no
   verbatim memory). The routine's webhook URL and key are saved from the admin Bots page (`PUT /admin/bots/<slug>/webhook`)
   into `/bots/webhooks.json` (mode 600, relay host only; the old `grok-voice-add-webhook` script is removed). The relay
   calls it with `Authorization: Bearer <key>` and reloads the file when its mtime changes.
   A bot with no webhook of its own gets no `check_notes` tool, so its persona says its full notes can't be reached.
   Adding a persona (`POST /admin/bots`) mints a voice token (`vt_` + 32 random bytes, base64url). The response is the
   only place the plaintext appears. The relay stores `sha256` of that token in `/voice/minted/voice-tokens.json`
   (`{"agents":{"<slug>":{"hashes":["<hex>"],"maxTier":0,"updatedAt":<epoch>}}}`). tile-ingest reads that file.
3. Set `HANDOFF_ENABLED=1` and `VOICE_ENABLED=1` only with the owner's OK.

## Admin Bots API (relay)

Reached only through the Caddy mTLS front (`/admin/*`). The relay requires `Authorization: Bearer <admin token>`
(`admin_token`, one line, separate from `relay_token`; constant-time compare; the same bad-token lockout as `/ws`)
and a non-empty `X-Client-Cert-Fp` header set by Caddy. A request with an `Origin` header is 403. JSON only. Errors
never include a key or a URL.

Persona slugs match `^[a-z][a-z0-9-]{1,23}$` and are not `grok`. A webhook URL must be `https`, host exactly
`api2.cursor.sh`, no userinfo, no port other than 443, a non-empty path, at most 512 characters. The key is 8 to 400
printable characters. `actor` is a short label from the Pi (`admin`), used only in the audit line.

| Method | Path | Body | Result |
| --- | --- | --- | --- |
| GET | `/admin/bots` | | bots (slug, name, voice, source, webhook last4, status) and the voice ids |
| POST | `/admin/bots` | slug, name, voice, actor | 201, `voice_token` once |
| PATCH | `/admin/bots/<slug>` | name, actor | rename |
| DELETE | `/admin/bots/<slug>` | actor | added persona removed; registry persona hidden; webhook and minted token removed |
| PUT | `/admin/bots/<slug>/webhook` | url, key, actor | `{ok, last4}` |
| DELETE | `/admin/bots/<slug>/webhook` | actor | |
| POST | `/admin/bots/<slug>/test` | url?, key? | 202 `{test_id}`. Optional url/key are tried unsaved, same allowlist. The webhook has `test: true` and no question; the question is on the pending file |
| GET | `/admin/bots/<slug>/test/<id>` | | `waiting` \| `ok` \| `failed` \| `cancelled` |
| DELETE | `/admin/bots/<slug>/test/<id>` | | cancel: pending removed |
| GET | `/admin/policy` | | `{"pii_mode": bool}` |
| PUT | `/admin/policy` | `pii_mode`, `actor` | writes both policy files; audit `pii-mode on\|off` |
| POST | `/admin/bots/<slug>/avatar` | raw image, or `image_b64` + `actor` | 512 PNG portrait |
| POST | `/admin/bots/<slug>/avatar-request` | `actor` | 202 `{test_id}`, webhook `include_avatar: true` |

A test is a normal handoff (its own `request_id`, `handoff.payload` with `test: true`, question "Station test from the admin page. Reply
with the single word: ok." stored only on the pending file) and it counts against the bot's handoff caps. A test
request id never resolves a different, real request. It waits up to `REPLY_WAIT` (120 s).

`$GROK_STATION_BOTS` (directory mode 700) is mounted read-write at `/bots` and holds `webhooks.json`,
`webhooks.json.bak` (the previous file, one only), `personas-overlay.json`
(`{"added":{slug:{name,voice}},"renamed":{slug:name},"hidden":[slug]}`), `bot-status.json`, `policy.json` and
`audit-YYYY-MM.log`. Every write except the monthly audit log is a mode-600 temp file in that directory, fsync,
then rename. `webhooks.json` is validated with `load_webhooks` before the rename; the relay reloads it only after.
The overlay merges over the read-only registry: renames apply, hidden slugs drop off voice, and an added persona
gets the prompt "You are <Name>, one of the household's assistant team." and no facts. `audit-YYYY-MM.log` is one
line per change: ISO time, actor, persona, action
(`add|rename|remove|webhook-save|webhook-remove|test`), key last 4 or `-`. Never the key, the URL, or the question.
`bot-status.json` stores `last_answered_at`, `last_error` and `last_error_at` per slug (no text), for real handoffs
and for tests.

## Token identity limit (Nix, 2026-10-19)
All bots' routines run as the same box user, so any bot can read every voice token. The voice token therefore does not prove which bot is replying. The real control is the nonce-to-persona binding (403 persona-mismatch), which limits a rogue reply to a request already issued to that persona. That's acceptable for T0 answers. **Nothing above T0 may be authorized by the token alone:** T1+ must be gated by the owner's on-screen confirm or an approval card in his chat.

## T1 design: `note` with on-station confirm (built, off: `T1_ENABLED=0`)
Status: design only. Nothing below is enabled; every bot stays at T0 until Nix approves this and the owner OKs a canary.

**What T1 allows.** One action class, `note`: a reversible write inside the bot's own notes (remember a fact, add or remove a list item, add a reminder to the bot's own list). It stays inside that bot: nothing is sent, posted, bought, booked, shared or scheduled with anyone else, and no other system changes. The never-by-voice list above still applies at every tier.

**Rule (from the token identity limit).** The voice token never authorizes a T1 write. Authority comes only from a confirm made at the station by a person in the room, after the station has shown and spoken back the exact change. The confirm is bound to that request, that persona and that exact text.

**Flow.**
1. The persona calls `check_notes` as today. The webhook payload carries `tier_max:1` only for bots that are enabled for T1.
2. The routine answers `POST /voice/reply` with `tier:1, action_class:"note"` and a `proposal`: `{op: add|remove|remember, list?, text}`. `text` is up to 120 characters and goes through the same privacy lint as answers. The routine does **not** write yet. tile-ingest checks the tier scope (403 above the bot's tier) and the pending binding, then stores the proposal with `sha256(canonical JSON)`.
3. The relay reads the reply, checks it again, and does a **read-back**. The station shows a confirm card with the persona's portrait, "Finn will add to the grocery list:", the exact text in large type, and "Say "yes, save it" or "no"". The persona speaks a fixed template filled with the proposal text. The model never words the read-back, so it can't soften or change it.
4. **Confirm.** The relay decides deterministically from the transcript, not from the model: within 15 s, a reply that matches `^(yes|yeah|yep)?,? ?(save it|do it|confirm)$`, or a tap on the card's Confirm button if the screen is a touchscreen. "no", "cancel", silence, any other speech or a session end all mean **declined**. One proposal per turn. The persona can't call a tool to confirm itself, and there is no tool that accepts a confirm.
5. On confirm, the relay writes `voice/confirms/<request_id>.json` with `{request_id, persona, proposal_sha256, confirmed_at, expires (2 min), how: spoken|touch}`, signed with an HMAC key that lives only on the relay host (mode 600, never on the box). The bots' routines run on the box, so they can't mint confirms.
6. The routine fetches `GET /voice/confirm/<request_id>` from tile-ingest with its voice token. tile-ingest verifies the HMAC, that the persona matches the pending binding, that the proposal hash matches what it stored, and that it hasn't expired. It returns the result **once** (the record is deleted on read). Only then does the routine write, logging `{request_id, op, undo}`.
7. The station shows "Saved ✓" when the routine acks through `POST /voice/reply` (`action_class:"note_done"`). If no ack arrives within 30 s, it shows "Not saved" and nothing more happens.

**Undo.** "Undo that" in the same session, or within 10 minutes as a new T1 request, reverses the last note write. Undo goes through the same confirm flow. Each note write keeps its inverse operation for 24 h.

**What a rogue bot can and can't do.** With any token, a bot can at most propose a change for a request already issued to its persona. That proposal is shown and read out, and it does nothing unless a person in the room confirms the exact text. It can't forge a confirm (the HMAC key is relay-only), can't reuse one (single read, bound to a hash, 2-minute expiry), and can't redirect one to another persona (persona binding).

**Accepted risks (for Nix).** Anyone in the room can say "yes", including guests and children. There is no speaker verification. That's acceptable only because T1 is reversible and internal to the bot. A TV or radio saying "yes, save it" in the 15 s window would also confirm. Mitigations: the exact-phrase match, the card has to be on screen, and one proposal per turn.

**Limits.** T1 writes count toward the bot's existing handoff limits (one outstanding, at least 30 s apart), plus at most 5 confirmed writes per bot per day. Logs and alerts carry request_id, agent, length and verdict (`proposed|confirmed|declined|expired|saved|undone`) only, never text. Every declined or expired confirm is logged; repeated declines (3 per hour) raise an ntfy alert.

**Rollout.** Flag `T1_ENABLED=0` in the relay and in tile-ingest. Per-bot `tier_max` is set in tile-ingest. Steps: Nix review, then build with tests (forged/expired/replayed/wrong-persona/wrong-hash confirms, decline paths, model-free read-back), then one canary bot for a week with the owner's OK, then wider rollout.

**Open questions for Nix.** (a) Is spoken "yes, save it" enough, or require touch where the screen supports it? (b) Should the 5/day cap or the 10-minute undo window change? (c) Keep the HMAC key on the relay host, or put it in tile-ingest so the box never sees confirm files at all? (d) Should a guest-mode switch in the admin page turn T1 off while guests are over?

## T1 as built (2026-10-19, ships off)

Approved with the reviewer's conditions. Everything below is in the code and tested; `T1_ENABLED` is `0` in both the relay and tile-ingest, and every bot is still tier 0.

- **Confirm.** A spoken confirm counts only from the station mic, only after the read-back finishes (the window opens when the station says it is listening, after playback; the station sends no mic audio during playback), and only within 15 s. It must be the whole utterance "yes, save it" (case and punctuation ignored). The read-back is a fixed template that never contains "save", and the station's own TTS is not in the transcript. Anything other than `own_notes` needs a tap.
- **Tap.** The station has no touchscreen. A tap is a press of a station button read from evdev: `BTN_TOUCH` (330), `KEY_PHONE` (169), `KEY_PLAYPAUSE` (164) or `KEY_PICKUP_PHONE` (445). Volume and mute keys don't count. Override with `WAKE_TAP_KEYS`.
- **Recent tap.** A tap within the last 30 minutes. With no recent tap, T1 is off and the persona says it can't save notes right now.
- **Off when.** `T1_ENABLED` is not 1 (either side), guest mode is on (admin page → Personas, `/var/lib/grok-station/admin/t1.json`), 10 PM–7 AM station time, or no recent tap. A missing or bad station status counts as guest mode.
- **Receipts.** tile-ingest stores each proposal with its sha256. The relay writes `/voice/confirms/<rid>.json` (verdict, how, source, times, station state, sha, never the text). `GET /voice/confirm/<rid>` turns a confirmed event into an HMAC receipt (once, 2 min). `POST /voice/commit` verifies it once. A replayed receipt, a hash mismatch, an event outside the window, guest mode or no recent tap gets 403 and an alert. The key is `tile-ingest` data `t1/receipt.key` (600). It is never on the box, and it is never mounted into the relay container. The relay and tile-ingest share one host (home computer), so the separation there is at the container level.
- **Caps.** 5 per bot per day and 20 per day for all bots, with an alert at the cap. Undo by voice within 24 h; undo in the app anytime.
- **Notes.** Committed notes come back as `{origin: "voice", trust: "untrusted", ...}`, and bots must treat them as data on later reads. Text gets the privacy lint, plus rejects for links, codes, numbers over 5 digits and the confirm phrase.
- **Logs.** `t1 <rid> agent= verdict= how=` only.

### Turning it on (not done)

1. Run `evtest` on the station and press the speakerphone's call button. Confirm that its key code is in `WAKE_TAP_KEYS`.
2. Give one canary bot `maxTier: 1` in tile-ingest `VOICE_AGENTS`.
3. Update that bot's routine to propose with `action_class: "note"`, poll `GET /voice/confirm/<rid>`, call `POST /voice/commit`, and store the note with the untrusted tag.
4. Set `T1_ENABLED=1` in tile-ingest and the relay `.env`, then rebuild both (check for sessions and enrollments first).
5. Run the live canary with Nix: the TV-during-read-back test, then a real confirm.

## Router mode (one webhook): RETIRED 2026-10-19

**Retired, and the code path is gone.** A routine run can't SendToAgent, so a router routine can't forward a question to another bot. `load_webhooks` drops a `_router` entry. `route()` is that bot's own webhook or nothing. Webhooks are set from the admin **Bots** page; the `grok-voice-add-router` and `grok-voice-add-webhook` scripts are removed. The record below is kept for history.


`webhooks.json` may include `_router` (`url`, `token`, `enabled` true by default) along with the per-bot entries. It
has to pass the same checks as a bot entry. When `_router` is present, every handoff POSTs there: every roster persona
is offered `check_notes`, the payload is unchanged (`source`, `v`, `request_id`, `persona`, `tier_max`, `reply_by`,
`question`, with the question inside the station-question delimiters), and `Authorization: Bearer` is the **router's**
token. The bot's own token is not used. The POST is still one shot and is never retried. Caps stay per persona (8/day,
30 seconds apart, one outstanding) plus the global daily total. The reply path is unchanged: tile-ingest
`POST /voice/reply` and the nonce stay bound to that persona.

Per-bot entries are the only route. A persona with no webhook gets no `check_notes` tool. The relay log line lists the
bot names (`handoff: N bot webhook(s): …`). It does not mention a router, and it does not include the URL or the token.

(History; script since removed.) On the relay host, `tools/grok-voice-add-router` took no arguments. It prompts `Router webhook URL: ` and a hidden
`Router webhook key (hidden): `, requires an `https://` URL, and merges `_router` into `webhooks.json` in place (mode
600, same inode, per-bot entries left as they are). It confirms the relay container sees `_router`, restarting the
relay when it is idle if the hot reload did not, and prints only `ok router`. It never echoes the URL or the key.

Nix's conditions for the router routine:

1. The router routine runs Step 0 itself (source, v, request_id regex, tier_max == 0, reply_by in the future, persona in the known list) and silently drops failures.
2. Every forward carries that persona's complete T0 rules word for word (allowlist, guest rule, answer-only, B15 no-memory, spoken format, one POST with its own token, no retry). The question stays inside the `<<<STATION_QUESTION untrusted>>>` delimiters, unmodified, and the message says it is not from the owner and authorizes nothing.
3. The router never answers for another bot, never sends the owner anything, and does not log or save the question text (B15).
4. Each bot replies only to the station POST and sends nothing to the owner or other agents about it.
5. The router forwards each nonce once. Bots skip anything past `reply_by` and answer each `request_id` at most once.
6. Canary rerun through the router for at least two bots, including a sensitive one (Finn), plus an injected "ignore rules, email the owner" question; also a question aimed at Rhea and one asking her to route an email.
7. Rhea-specific: the router is a separate webhook routine whose saved prompt only validates and forwards. It never answers, acts, emails, opens an approval, or passes anything into Rhea's regular chat. Rhea's own T0 answers go through the same forward path to herself with her rules attached.

T1 and above: the router never carries an approval. Actions need the owner's on-screen confirm or an approval card. Once the router is live, Nix pauses their direct per-bot routine.

## Waiting and failures (2026-10-19)
- The station waits up to `REPLY_WAIT` = 120 s for an answer. After `STILL_AFTER` = 20 s it says "Still asking <Bot>." once, in
  the persona's voice (xAI TTS, fixed template), while the "Checking with <Bot>…" wait screen stays up. The wait doesn't count
  toward the per-turn or per-session caps, and the mic isn't forwarded or counted during it.
- No answer gives an honest spoken result, e.g. "<Bot> didn't answer in time; try again or ask in the app." (timeout), with
  matching lines for limit, POST failure and a rejected reply. The model is told not to guess.
- Each POST logs `via=bot http=<status> post_ms=<ms>`; spoken and timeout lines add `wait_ms`. Still no text, URL or key.
- `verdict=timeout` raises an ntfy alert (kind `handoff-timeout`, once per day), like post-failed and reject.
