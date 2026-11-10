# Bot Update and Set up lifecycle (authoritative, 0.13.35)

This is the spec for how the station updates a bot's instructions and sets up a new bot through the main bot. It
covers the station admin page (`admin/grok_admin.py`, `admin/web/app.js`), the relay (`relay/relay.py`,
`relay/botreply.py`, `relay/handoff.py`) and the bot side (the routine templates `admin/routine-home.txt` and
`admin/routine-cloud.txt`, `scripts/gs-update.sh`, `admin/setup-params.txt`). Every rule names the function that
enforces it. If code and this file disagree, the code is wrong or this file is stale: fix one in the same change.
`tests/test_lifecycle_scenarios.py` checks the rules marked [S] on a tree installed by `tools/install-files.sh`, and
`tools/release-check.sh` runs it before every push (the clone's pre-push hook, `tools/install-release-hook`).

## Words

- **Release**: `VERSION` on the station. Every release changes the first line of each routine template
  ("Grok Station instructions, release X.Y.Z.").
- **Main bot**: the relay overlay's `list_source` (`relay.py:list_source_slug`). The first bot added becomes it
  (`relay.py:admin_bots_post`); it relays Set up to other bots.
- **Signed update status**: the line `X.Y.Z instr=<16 hex> helper=<ver|-> computer=<16 hex>` a bot sends, sealed,
  after it adopts new instructions (`botreply.UPDATE_STATUS`). The relay keeps the last one per bot
  (`relay.py:note_update`, `bot_updates`) and drops it on delete or a new pairing (`relay.py:forget_bot_status`).
- **Pairing stamp**: the release that was current when the relay pinned a bot's key (`relay.py:note_paired`, from
  `admin_key_put` and `setup_pair_decide` accept), kept in `state/bots/paired.json` (`relay.py:PAIRED_FILE`). It is
  read only while the bot has no signed status: `note_update` drops it, and so do a key revoke, revoke-all, a key
  mode switch, a delete and a new pairing (`relay.py:forget_paired`, called by `forget_bot_status`).
- **instr hash**: `gs_routine.instr_hash` of the exact instructions text. The station's expected text for a bot is
  `grok_admin.py:render_bot_routine` (this release's template, this bot's slug and mode, the update pin from
  `admin/web/deploy-pins.js`, and for cloud bots the station address, TLS fingerprint and station public key).
- **Content fingerprint table**: `admin/bot-releases.txt` (`tools/gen-update-manifest.py`, `admin/bot_content.py`).
  One line per release: update commit, `gs-update.sh` sha256, and a fingerprint of what a home and a cloud bot run
  with the release line taken out.
- **Setup literals**: `admin/setup-params.txt` (`tools/gen-setup-params.py`): RELEASE, the install commit and
  `install-home.sh` sha256, ROUTINE_COMMIT, HASH_ROUTINE_HOME, HASH_ROUTINE_CLOUD, UPDATE_COMMIT, HASH_UPDATE. A bot
  saves this text as `gs-setup-params.txt` when it sets up or updates; those are the literals it holds.

## Update

Flow: Bots page Update (`app.js:rowUpdatePoll`, `POST /api/finish-update/send`) to
`grok_admin.py:Handler.api_post_finish_update` to `relay.py:admin_bot_update`, which posts an `update_bot` webhook
with only `release`, `commit` (the pinned update commit) and `owner`. The bot runs the pinned `gs-update.sh` from
its own instructions (`--release`, `--commit` from the webhook, nothing else). `gs-update.sh` checks formats, no
downgrade, and every file against the release manifest sha256 (`scripts/update-manifest.txt`), renders its new
instructions with the embedded `gs_routine.py`, then posts the signed status. `relay.py:note_update` records it.

Row state is `grok_admin.py:finish_rows`, computed on every read from the relay's bot list and
`finish-update.json`; nothing about it is cached in the page.

| Bot state | Row state (`finish_rows`) | Bots page shows | Station sends on Update | Bot checks |
|---|---|---|---|---|
| Fresh: listed, not paired or no webhook | not listed | Configure / Set up, no Update | nothing (row skipped) | n/a |
| Paired with this release's Add bot instructions, no status yet | done | Connected, no Update | nothing | n/a |
| On this release, status instr equals the expected hash [S] | done | no Update | nothing | n/a |
| On this release, instr differs only by the placeholder owner | needs | Update | update_bot | release newer, manifest sha256 |
| On this release, instr differs otherwise | different | "The bot's instructions don't match this release." | Update allowed | same |
| Older release, same content (not the main bot) [S] | done (`same_content`) | no Update | nothing | n/a |
| Older release, same content, paired with no status (not the main bot) | done, unconfirmed | no Update | nothing | n/a |
| Older release, different content [S] | needs | Update, Bots-menu dot | update_bot with this release | release newer, sha256 of every file |
| **Main bot on any other release, even with the same content** [S] | needs | Update on its row, callout "Update {main} first", dot | update_bot | same |
| Update sent, inside FINISH_WAIT_S (600 s) [S] | waiting | "Updating…", Update disabled; a second press is 409 "Still waiting for {name} to reply." | nothing new | n/a |
| Update sent, no reply after 600 s [S] | noreply | "Update failed: No reply within 10 minutes.", Update again | update_bot again (relay allows one ask per HELPER_ASK_GAP_S, 600 s) | same |
| Update could not be delivered (bot's computer off, webhook error) [S] | keeps its state, reason kept in `finish-update.json` "failed" | "Couldn't reach the bot." (502) or "Asked a few minutes ago." (429) | nothing (relay frees the ask slot) | n/a |
| Reply was not a valid status line | refused (`update_refused_at`) | "The bot refused the update." | Update allowed | n/a |
| Set up before 0.11.8 (no update_bot in its instructions) | copy | Copy instructions | nothing until copied | n/a |
| Home bot whose helper is out of date | needs, even when otherwise done | Update (reinstalls the helper) | update_bot; helper ask via `admin_helper_update` | pinned `install-home.sh` sha256 |
| Station can't render the instructions | error | the render error | nothing | n/a |

Home and cloud: home bots on one computer form one group sharing a helper; the group's updater is the chosen bot,
else one that has answered (`finish_rows`, `api_post_finish_update` /updater). Cloud bots render the station
address, fingerprint and public key into their instructions; home bots do not.

Page side: the page ends a waiting row on its own clock at the server's `wait_left`, even when it can't re-read the
station (`app.js:finishExpire`, `rowUpdating`) [S].

## Set up (no-paste, typed setup_relay)

Flow: Set up on a row (`app.js`, `POST /api/bots/setup-send`) to `grok_admin.py:Handler.bots_setup_send`, which
builds `_setup_ctx` and calls `setup_send`. The poster asks `relay.py:admin_setup_relay` with
`{slug: main, target, mode, path}`. The relay builds the payload from station state only
(`setup_station_values`, `station_release`, `setup_install_pins`, `setup_routine_pins`, `setup_update_pins`),
freezes it into a pending file, then posts exactly those bytes to the main bot's webhook. The main bot's routine
(step 1 of the Station setup relay section) checks every field, including that routine_commit/routine_sha256 and
update_commit/update_sha256 equal the ROUTINE_COMMIT, HASH_ROUTINE_*, UPDATE_COMMIT and HASH_UPDATE it holds ("An
older commit is declined"), then forwards to the target and seals a `setup_relay_ack`
(`relayed | failed | needs_details`, reason `target_unreachable | declined | unknown | standing_absent`). The target
checks the same literals against its own Station setup parameters (`admin/setup-params.txt` step 1: release equal,
pins equal when held), and on first time asks its owner to confirm the station address and fingerprint.

Admin row status: `not_set_up | sent | failed | connected` (`grok_admin.py:_SETUP_STATUSES`). Relay display state:
`relay.py:setup_state_public` (sent, relayed, awaiting_confirm, needs_details, review, accepted, rejected, failed,
expired), merged onto a Sent row by `grok_admin.py:setup_public`.

| Setup state | Page shows (`app.js:setupRowStatus`) | Gate / what is sent | Pins and literals checked |
|---|---|---|---|
| Main bot not set | "Add your main bot first" | setup_send poster 400 not configured | n/a |
| Main bot behind (not exactly this release) [S] | "Update {main} first" callout; Set up toast "Update the main bot first." | admin 409 (`setup_send`, from `_setup_ctx` outdated); relay 409 before freezing (`admin_setup_relay`, `main_release_ok`) | nothing sent |
| Main bot with no signed status [S] | as its Update row says (done only if paired with this release's Add bot instructions) | relay proceeds only if its pairing stamp is this release; no stamp, an older stamp, or an unreadable status file is 409 "Update the main bot first." | nothing sent on 409 |
| Admin finish check fails, or the main bot has no row | Set up toast "Update the main bot first." | admin 409 (`_setup_ctx` fails closed: outdated) | nothing sent |
| Main bot current, target known [S] | "Setup request sent to {main}" | one setup_relay post; path=first carries install, routine and update pins | main: payload pins equal its held literals; target: release and pins equal |
| sent, past SETUP_RELAY_TTL_S (900 s), no ack | "No reply from {main}." | Send again allowed | n/a |
| relayed (held path) | "{main} passed it to {bot} · waiting for {bot} to finish" | nothing | target installs from held literals |
| awaiting_confirm (relayed, first path) | "… waiting for {bot} to confirm" | nothing | target's owner compares address and fingerprint |
| review | "Key posted · compare the fingerprint, then Accept" | Accept needs the admin password (`bots_setup_pair`) | fingerprint shown |
| accepted | "Paired", paste webhook URL and key | webhook paste marks Connected | n/a |
| rejected | "Key not accepted" | Set up sends a new request | n/a |
| needs_details | "{bot} finished · add webhook details on this page" | manual paste | n/a |
| failed / declined | "{main} couldn't pass it on." | Send again or enter details yourself | (0.13.34 incident: main bot on 0.13.33 declined 0.13.34 pins) |
| failed / standing_absent | "{bot} isn't ready for first-time setup" | ask the main bot in chat | target lacks the setup steps |
| connected | "Connected" | n/a | n/a |

A new Set up press replaces the pending request for that target (`relay.py:_retire_setup_pending`), so a reply to
the old one is refused.

## Invariants

1. **The main bot is on exactly this release before any setup_relay.** `finish_rows(main=...)` skips both
   same-content shortcuts for the main bot, so it is done only when its signed status names this release with this
   release's instr hash (or it was paired with this release's Add bot instructions and has no status yet, because
   those hold this release's literals). Every caller passes the main bot from the same relay read: `finish_view`
   for `/api/finish-update` (rows, dot, callout) and Update, and `_setup_ctx` for the setup gate, so the page and
   the gate agree. `_setup_ctx` fails closed: if `finish_rows` raises, or the view has no row for the main bot,
   the main bot counts as outdated. The relay checks again in `admin_setup_relay` (`relay.py:main_release_ok`) and
   sends only when the main bot's signed status names this release, or it has no signed status and its pairing
   stamp is this release. Everything else (an older or other status, no stamp, an older stamp, an unreadable status
   file, no station release) is 409 "Update the main bot first." before anything is frozen or posted. Reason:
   setup pins (ROUTINE_COMMIT, UPDATE_COMMIT) change every release, and the main bot checks them against the
   literals it holds. The relay checks the release only; the admin also checks the instr hash. [S]
2. **What Set up sends equals what the receiving bots hold.** The relay reads the pins from the installed
   `admin/setup-params.txt`; the update pin there equals `deploy-pins.js` `commit` and the `gs-update.sh` sha256,
   which `render_bot_routine` writes into every bot's instructions (`tools/gen-deploy-pins.py`,
   `tools/check-deploy-pins.py`). [S]
3. **Same content means only the release line differs.** `same_content` rebuilds the text from this template with
   the older header and that release's own update pin, and compares the signed hash. It never calls a newer or
   unknown release the same. It never applies to the main bot (invariant 1).
4. **Waits end on time on both sides.** FINISH_WAIT_S (admin), UPDATE_REPLY_BY_S (bot reply_by) and HELPER_ASK_GAP_S
   (relay ask gap) are all 600 s (`tests/test_finish_update.py`, `tests/test_gs1334_update_timeout.py`). The server
   turns waiting into noreply on read; the page does the same on its own clock. [S]
5. **Every file a module imports is installed.** Every local module reachable from `admin/grok_admin.py` is on an
   install line in `tools/install-files.sh`; the relay modules are in `install.sh` st_relay. The installed admin
   page imports from the installed tree alone (`python3 -I`). [S] (0.13.33 shipped without `gs_render.py`.)
6. **A valid status line is never a secret.** A reply that fully matches `UPDATE_STATUS` for an update_bot request
   skips the SSN and card digit checks; free text still gets them (`botreply._accept_answer`,
   `handoff.lint_reply`). [S] (0.13.32.)
7. **One clock for training stamps.** Clear training (`grok_admin.py:clear_bot_training`) and new sessions
   (`grok_wake.py:session_stamp`) both use UTC, so a recording after Clear is scored on a station behind UTC
   (`fewshot.py:current_training_set`). [S] (0.13.31.)
8. **Nothing from a webhook becomes a command.** Update carries a release and a commit only; setup_relay carries
   typed fields only; both are frozen before posting (`admin_bot_update`, `admin_setup_relay`,
   `handoff.setup_relay_payload`).
9. **The release gates fail closed.** A missing, unreadable or unexpected input to the main bot gate is a refusal,
   not a pass (`_setup_ctx`, `main_release_ok`). The install sim behind the scenarios refuses to run as root and
   stops if a host directory can't be bound read-only (`tests/lifecycle_sim.py:install`).

## Release order

Press Update on the main bot first and wait until its row is done, then Update the rest, then Set up. The page
enforces the order through invariant 1.

## Checking a release

`sh tools/release-check.sh` on the exact commit you will push: ritual checks (`tools/check-version.py`,
`tools/check-deploy-pins.py` with no pending re-pin), the installed-tree import checks, and the scenarios in
`tests/test_lifecycle_scenarios.py`. It fails when a scenario could not run. No push unless it passes.
`sh tools/install-release-hook` chains `tools/release-check-pre-push` into the clone's local pre-push hook, which
the global hooks dispatcher runs after pii-guard and the leak scan. The hook blocks a push unless every pushed
commit is the checked-out commit and `tools/release-check.sh` ends with `release-check: PASS`. The dispatcher must pass
the local hook's exit status back to git for the block to hold.
