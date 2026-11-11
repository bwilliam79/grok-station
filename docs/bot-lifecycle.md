# Bot Update and Set up lifecycle (authoritative, 0.13.37)

This is the spec for how the station updates a bot's instructions and sets up a new bot through the main bot. It
covers the station admin page (`admin/grok_admin.py`, `admin/web/app.js`), the relay (`relay/relay.py`,
`relay/botreply.py`, `relay/handoff.py`) and the bot side (the routine template `admin/routine-home.txt`, `scripts/gs-update.sh`, `admin/setup-params.txt`;
0.13.37 removed `admin/routine-cloud.txt`). Every rule names the function that
enforces it. If code and this file disagree, the code is wrong or this file is stale: fix one in the same change.
`tests/test_lifecycle_scenarios.py` checks the rules marked [S] on a tree installed by `tools/install-files.sh`, and
`tools/release-check.sh` runs it before every push to `main` (the clone's pre-push hook, `tools/install-release-hook`).

## Words

- **Home bot**: since 0.13.37 the only kind. The bot's key lives on its always-on home computer, which seals and
  posts every reply. There is no key location to store or switch: `key_mode` is not stored, listed or forwarded,
  and any value but "home" is refused (`relay.py:key_mode_refused`, `grok_admin.py:KEY_HOME_ONLY`). An overlay
  written by an older release loses its `key_mode` on the next save.
- **Release**: `VERSION` on the station. Every release changes the first line of each routine template
  ("Grok Station instructions, release X.Y.Z.").
- **Main bot**: the relay overlay's `list_source` (`relay.py:list_source_slug`). The first bot added becomes it
  (`relay.py:admin_bots_post`); it relays Set up to other bots.
- **Signed update status**: the line `X.Y.Z instr=<16 hex> helper=<ver|-> computer=<16 hex>` a bot sends, sealed,
  after it adopts new instructions (`botreply.UPDATE_STATUS`). The relay keeps the last one per bot
  (`relay.py:note_update`, `bot_updates`) and drops it on delete or a new pairing (`relay.py:forget_bot_status`).
- **Pairing stamp**: the release that was current when the relay pinned a bot's key (`relay.py:note_paired`, from
  `admin_key_put` and `setup_pair_decide` accept), kept in `state/bots/paired.json` (`relay.py:PAIRED_FILE`). It is
  read only while the bot has no signed status: `note_update` drops it, and so do a key revoke, revoke-all, a
  delete and a new pairing (`relay.py:forget_paired`, called by `forget_bot_status`).
- **instr hash**: `gs_routine.instr_hash` of the exact instructions text. The station's expected text for a bot is
  `grok_admin.py:render_bot_routine` (this release's home template, this bot's slug and the update pin from
  `admin/web/deploy-pins.js`).
- **Content fingerprint table**: `admin/bot-releases.txt` (`tools/gen-update-manifest.py`, `admin/bot_content.py`).
  One line per release: update commit, `gs-update.sh` sha256, and a fingerprint of what a home bot runs with the
  release line taken out. Lines from before 0.13.37 also carry a cloud fingerprint; it is read past and not used.
- **Setup literals**: `admin/setup-params.txt` (`tools/gen-setup-params.py`): RELEASE, the install commit and
  `install-home.sh` sha256, ROUTINE_COMMIT, HASH_ROUTINE_HOME, UPDATE_COMMIT, HASH_UPDATE (0.13.37 dropped
  HASH_ROUTINE_CLOUD). A bot
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
| Set up before 0.11.8: no signed status, no setup record, nothing copied or sent | copy | Copy instructions | nothing until copied | n/a |
| Home bot whose helper is out of date | needs, even when otherwise done | Update (reinstalls the helper) | update_bot; helper ask via `admin_helper_update` | pinned `install-home.sh` sha256 |
| Station can't render the instructions | error | the render error | nothing | n/a |
| Bot set up with its key on a bot computer before 0.13.37 | as above (treated as home) | Update | update_bot | `gs-update.sh` refuses the cloud routine (CLOUD_GONE): use Set up or Replace key on the home computer |

Home bots on one computer form one group sharing a helper; the group's updater is the chosen bot, else one that has
answered (`finish_rows`, `api_post_finish_update` /updater). Home instructions carry no station address,
fingerprint or public key; the helper holds those from `gs-seal-setup init`.

Page side: the page ends a waiting row on its own clock at the server's `wait_left`, even when it can't re-read the
station (`app.js:finishExpire`, `rowUpdating`) [S].

## Set up (no-paste, typed setup_relay)

Flow: Set up on a row (`app.js`, `POST /api/bots/setup-send`) to `grok_admin.py:Handler.bots_setup_send`, which
builds `_setup_ctx` and calls `setup_send`. The poster asks `relay.py:admin_setup_relay` with
`{slug: main, target, mode, path}`; mode is always "home" (`bots_setup_send` sets it, `handoff.setup_field_ok`
refuses anything else, 0.13.37) [S]. The relay builds the payload from station state only
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
| review | "Action needed · {bot}'s key is waiting for you to accept", an **Accept key** button that focuses Accept in a panel headed "Accept the key from {bot}" | Accept needs the admin password (`bots_setup_pair`); a key that differs from the pinned one asks "Replace {bot}'s key?" first | fingerprint shown; a changed key shows old and new |
| accepted | "Paired", paste webhook URL and key | the paste saves the webhook and runs the Test | n/a |
| accepted, Test running | "Testing · waiting for {bot}'s reply, up to 3:00" | nothing | Test waits TEST_LIMIT_S (180 s, the same as REPLY_WAIT) |
| accepted, Test passed | "Connected" | `bots_setup_connected` needs the passing `test_id` (the relay's Test state "ok"); otherwise 409 | n/a |
| rejected | "Key not accepted" | Set up sends a new request | n/a |
| needs_details | "{bot} finished · add webhook details on this page" | manual paste | n/a |
| failed / declined | "{main} couldn't pass it on." | Send again or enter details yourself | (0.13.34 incident: main bot on 0.13.33 declined 0.13.34 pins) |
| failed / standing_absent | "{bot} isn't ready for first-time setup" | ask the main bot in chat | target lacks the setup steps |
| connected | "Connected" | n/a | n/a |

**Accept records the setup (0.13.39).** A successful Accept (the relay returned 200) writes the same pairing record as pinning a key by hand (`audit` action `pair`, then `note_setup`). The release is the one frozen into that setup request when the relay response includes it (`setup_pair_decide` reads the pending file's `frozen.release`). The admin setup row does not store a release, so a response without one records `version()`, the same file the request was frozen from unless the station was updated while the request was open. Reject writes nothing. With a webhook and no signed status yet, that record makes the Finish update row `done`, not `copy`. Remove and Dismiss still clear `finish["setup"][slug]` through `forget_bot_admin` / `forget_finish`. `setup_forget` clears the Set up row, and `forget_bot_traces` clears the relay's setup-relay row, pending requests, and pairing stamp. A removed bot has no pairing record, so it comes back with nothing carried over [S].

A new Set up press replaces the pending request for that target (`relay.py:_retire_setup_pending`), so a reply to
the old one is refused. Since 0.13.37 the page asks "Replace the pending request?" first while a request is open
(`app.js:setupSend`, `setupPendingWhat`), and a key posted on the old request can no longer be accepted or posted
(`setup_note_review` refuses a row whose rid or expiry no longer matches) [S].

**Forget (0.13.37).** Remove (`grok_admin.py:bots_remove`, `relay.py:admin_bots_delete`) and Dismiss
(`bots_dismiss`, `admin_imported_delete` with `forget`) run the same per-slug forget: `relay.py:forget_bot_traces`
(setup-relay row, every pending request for the slug, a running Test, the bot-status entry, signed update status,
pairing stamp, ack clips) and `grok_admin.py:forget_bot_admin` (Set up row and hint, Finish rows). Dismiss forgets
the slug the name would Set up as only when that slug is not a bot: `known_bot_slugs_or_none` (registry including
hidden bots, plus added bots) must be readable and must not list it, else nothing is forgotten (fail closed,
GS1337-02); the admin clears its own state only when the relay answers `forgot: true`. So a rescan after either shows a net-new Not set
up row, and Set up sends fresh home-mode details [S].

**Stale cloud requests (0.13.37 upgrade).** A setup_relay pending frozen with a mode other than home (0.13.36 froze
"cloud" for a bot with no stored key location) is not actionable: the relay drops all of them at start
(`relay.py:retire_cloud_setup_requests`, which also expires the row and drops its posted key, so the row shows "No
reply from {main}." and Send again), and `_pending_doc` refuses one found later. An ack, a key post or Accept for it
is refused [S].

**Key migration (0.13.37 upgrade, GS1337-01).** At its first 0.13.37 start, before the stale-request retire,
the relay runs `relay.py:run_key_migration` once (it is skipped once `bots/key-migration.json` exists, and waits a
start when the overlay or the keys file can't be read). For each pinned key it reads the overlay's raw `key_mode`
(kept by `save_overlay` until this file exists) and `home_evidence`:

| Pin | Verdict | Effect |
|---|---|---|
| `key_mode` cloud | cloud | marked "needs Replace key" |
| `key_mode` home | home | trusted |
| no `key_mode`, home evidence | home-evidence | trusted |
| no `key_mode`, no evidence | no-evidence | marked "needs Replace key" |

Home evidence (station fields only the home helper fills): `helpers.json` signed helper version
(`helper-signed`), `helpers.json` `X-GS-Helper` header version on a seal-verified post (`helper-header`),
`updates.json` signed update status with helper != "-" (`status-helper`) or computer != "-" (`status-computer`),
and a `paired.json` stamp of 0.13.37 or later (`paired-0.13.37`). A stamp from before 0.13.37 is not evidence.
A marked key stays in `bot_keys.json` (nothing is deleted) but `trusted_keys` leaves it out of every signature
check (`bot_paired`, `icon_view`, avatars, `botreply.ingest`, the Accept view), so replies, avatars and statuses
from it are refused. The bot list carries `key_move` (cloud | no-evidence) and the row asks for Set up. The mark
holds only while the pinned fingerprint equals the marked one. `clear_key_move` clears it on a different key from
Configure, or on any key accepted through Set up for no-evidence (a home re-setup keeps an existing key). One audit
line per slug: `<slug> key-migration <verdict> fp=<16 hex> mode=<home|cloud|-> evidence=<list|->`, and
`key-migration cleared was=... new=... via=setup|configure`. `tools/key-migration-preview.py` prints the verdicts
from a temporary copy of the state: an allowlist of the six files the migration reads (`bot_keys.json`,
`personas-overlay.json`, `helpers.json`, `updates.json`, `paired.json`, `personas/personas.json`), never `webhooks.json`,
minted tokens or secrets, in a 0700 directory removed on exit and on signals (GS1337-04) [S].

**Restoring `bot_keys.json` after the check (GS1337-07).** The migration judges keys once. After a good first 0.13.37 start, a restored `bot_keys.json` that holds a key the migration never saw leaves that key trusted and unmarked. Restore only a backup taken after the first 0.13.37 start. Revoke any bot whose key you don't recognize and set it up again. A restore before the check has completed is still judged in full at the next start.

**The key check fails closed (GS1337-05).** `key_move_state` reports damage when `key-migration.json` exists but
can't be read, has the wrong shape or a bad entry, or vanished after this relay read it; `key_migration_start` (the
relay start) also reports it when the migration raised or could not decide. While damaged, `trusted_keys` returns no
keys (like a corrupt `bot_keys.json`), the bot list shows `key_move: damaged`, marks can't be cleared, the relay logs
"KEY CHECK FAILED CLOSED" and the audit log gets one `key-migration damaged reason=... all-keys-distrusted` line.
A readable file again restores the marks as written; deleting it reruns the migration at the next start [S].
The migration reads `bot_keys.json` strictly (GS1337-06, `_strict_pinned_keys`; `botreply.load_keys` reads a damaged
file as no keys, which is right for replies but would finish the migration with nothing marked). A keys file that
exists but can't be read, isn't JSON or has the wrong shape, or is missing while the station has bots
(`_station_has_bots`: overlay key_mode, registry or added bots, or paired, helper or update records), makes it return
None: nothing is written, `key_migration_start` reports damage for this run, and the next start judges every key once
the file reads. A missing file on a fresh station with no bots counts as done. The preview prints "could not decide"
and exits 1 instead of an empty would_mark.

**Stale pages (GS1337-03).** `PUT /admin/bots/<slug>/key` and Accept (`setup-pair` decision accept) require
`expect_old_fp`, the 16-hex fingerprint of the key the page showed as pinned, or `-` for none. When it no longer
names the pinned key the relay answers 409 `KEY_CHANGED` and pins nothing [S].

## Team list from a voice session

Saying "update my team list" (or the `get_bot_list` tool) starts `relay.py:voice_ask_bot_list`. That runs
`ask_bot_list` as its own task, tracked in `_run_tasks` the same way an admin Test is, and awaited with
`asyncio.shield`. The session still speaks the result line when it is open (`voice_list_line`). The paid
voice session is not held open for the reply: `MAX_SESSION` (60 s) ends the session ("max turn") and that
cancel does not reach the list task, so `ask_bot_list`'s `finally` does not remove the pending file early.
The pending file stays until the list arrives or `REPLY_WAIT` (180 s) ends. A list the main bot posts after
the session has ended, and inside that wait, is accepted and applied (`merge_imported`), not refused with
403 reason=request. The one-in-flight lock is unchanged (`Limits.take_list`, `Limits.release_list`). The
admin rescan and import routes still await `ask_bot_list` on their own request.

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
9. **Every Set up is home.** The admin poster, the relay and the frozen payload carry mode home only; a bot with
   no stored key location gets home details, never cloud (before 0.13.37 the missing value fell back to cloud). [S]
10. **Connected means the Test passed.** The row is Connected only after `bots_setup_connected` sees the relay's
   Test for that slug in state "ok". [S for the gate via `tests/test_gs1337_connected_after_test.py`]
11. **The release gates fail closed.** A missing, unreadable or unexpected input to the main bot gate is a refusal,
   not a pass (`_setup_ctx`, `main_release_ok`). The install sim behind the scenarios refuses to run as root and
   stops if a host directory can't be bound read-only (`tests/lifecycle_sim.py:install`).
12. **A key that may be on a bot computer is not trusted.** After the key migration, a pin marked cloud or
   no-evidence verifies nothing until it is replaced or set up again; no pin is deleted by the migration. [S]

## Release order

Press Update on the main bot first and wait until its row is done, then Update the rest, then Set up. The page
enforces the order through invariant 1.

## Checking a release

`sh tools/release-check.sh` on the exact commit you will push: ritual checks (`tools/check-version.py`,
`tools/check-deploy-pins.py` with no pending re-pin), the installed-tree import checks, and the scenarios in
`tests/test_lifecycle_scenarios.py`. It fails when a scenario could not run. No push to `main` unless it passes.
`sh tools/install-release-hook` chains `tools/release-check-pre-push` into the clone's local pre-push hook, which
the global hooks dispatcher runs after pii-guard and the leak scan. It gates only pre-push lines whose remote ref
is `refs/heads/main`: the pushed commit must be the checked-out commit and `tools/release-check.sh` must end with
`release-check: PASS`, and a delete of `main` is refused. Other refs (pull request branches, new branches, deletes
of other branches, tags) pass; in a mixed push the `main` line decides. A last line with no newline is read, a
trailing CR is dropped, and a line that is not four fields is refused. The installed stub reads the refs first:
a push with no `main` line passes even from a checkout whose tree has no `tools/release-check-pre-push`, and a
`main` push from such a checkout is refused (`tests/test_gs1335_release_hook.py`). After `install-release-hook`
changes, rerun it once from a checkout of the new `main`; it replaces its own earlier stub. The dispatcher must pass
the local hook's exit status back to git for the block to hold.
