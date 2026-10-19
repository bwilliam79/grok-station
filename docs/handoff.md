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

## Token identity limit (Nix, 2026-10-19)
All bots' routines run as the same box user, so any bot can read every voice token. The voice token therefore does not prove which bot is replying. The real control is the nonce-to-persona binding (403 persona-mismatch), which limits a rogue reply to a request already issued to that persona. That's acceptable for T0 answers. **Nothing above T0 may be authorized by the token alone:** T1+ must be gated by the owner's on-screen confirm or an approval card in his chat.

## T1 design: `note` with on-station confirm (proposal for Nix's review, not built)
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
