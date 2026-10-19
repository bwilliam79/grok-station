# Teammate handoff (real-bot pilot)

A persona on the station can ask its own Grok Bot ("check my notes") and speak the answer. It is **off by default**:
`HANDOFF_ENABLED=0` in `relay/compose.yaml`, no webhooks, and tile-ingest `VOICE_ENABLED=0`. Each bot goes live only
after its own canary passes and the owner OKs it.

## Flow
1. The persona calls `check_notes(question)`. The relay offers the tool only to a persona that has a webhook entry.
2. The relay applies limits (per bot: 8/day, at least 30 s apart, one outstanding; plus a global daily total). A failed
   POST still counts. The relay then cleans the question (NFKC; control, zero-width and bidi characters dropped; 300-char
   cap) and writes `voice/pending/<request_id>.json` with a random nonce and an expiry.
3. One POST goes to the bot's routine webhook, and it is never retried:
   `{source:"grok-station", v:1, request_id:"vs_…22", persona, tier_max:0, reply_by, question}`. The question sits
   between `<<<STATION_QUESTION untrusted>>>` and `<<<END_STATION_QUESTION>>>` as data.
4. The routine answers through tile-ingest `POST /voice/reply` `{request_id, text, tier, action_class}` with its own
   voice token. tile-ingest checks the schema, tier scope, privacy lint and pending binding, then writes `voice/replies/`.
5. The relay reads the reply once (it deletes the file on read) and checks the nonce, scope and lint again. It speaks
   the reply only if the session is still waiting. Otherwise it drops it, and a late reply finds no pending file.

The Pi shows "Checking with <name>…" while it waits.

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
   verbatim memory). The owner copies the routine's webhook URL and key from its panel and runs
   `grok-voice-add-webhook <bot>` on the relay host, which prompts for both (the key is hidden) and merges them into
   `webhooks.json` (mode 600). The relay calls it with `Authorization: Bearer <key>` and reloads the file on change.
   A bot without a webhook gets no `check_notes` tool, so its persona says its full notes can't be reached.
3. Set `HANDOFF_ENABLED=1` and `VOICE_ENABLED=1` only with the owner's OK.
