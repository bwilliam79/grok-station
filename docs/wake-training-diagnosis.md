# Wake-word training diagnosis (0.13.31)

Base: `7fe3a2dbc0f379a13641e612c2641c6763e927f1`. The station logs are not available here. This is from the code, the tests, and two reproductions that call the same functions the station calls. openWakeWord is not installed in this environment, so the gate reproduction uses the synthetic embedding streams `tests/test_fewshot.py` already uses. Those streams are what `calibrate` scores. The clip audio is reduced to them before the gate.

The bot name from the report is not repeated here.

## What the owner hits

Recording and the pass/fail decision run on the station. `relay/train_flow.py` only decides the spoken steps (5 clips, the silent-countdown lines, the stop lines). It does not train a model. `wake/grok_wake.py` `enroll` records the clips and then calls `build_fewshot`, which calls `wake/fewshot.py` `calibrate`.

The older computer trainer (`train/enroll_train.sh`, `train/enroll_eval.py`) is not on this path. `enroll` never writes a `READY` file. `train/enroll_watch.sh` only starts `enroll_train.sh` for a session that has one. The recall and false-accepts-per-hour gate in `enroll_eval.py` is that older path. The live gate is the one in `calibrate`.

## Path, with file and line

### Clips recorded and stored

`enroll` (`wake/grok_wake.py` 2123) opens `ENROLL_DIR/<phrase>/<session>/` (231, default `/var/lib/grok-station/enroll/inbox`). The session id is `time.strftime("%Y%m%d-%H%M%S")` with no timezone argument (2130), so it is the station's local clock.

A tap on Start recording reads about 3 seconds first and stores `noise.wav` (2158-2160). A voice start (`voice: True`) skips that read (2154-2155). Each kept clip is `NN.wav`, mode 0600 (2217-2218). A quiet or clipped take is not written. The same index is tried again, up to `3 * n` tries (2186-2214). One silent take starts a 3 second countdown. Silence through that countdown deletes the session and stops (2229-2234).

A finished session gets `session.json` (2246-2248). It is kept only when at least 5 clips were saved (`ENROLL_COUNT` is 5, 931 and 2245). `finished_sessions` (1445-1481) skips a directory without that marker, with fewer than 5 `NN.wav` files, or whose clip bytes duplicate an earlier session. `score_sources` (1520-1540) then drops sessions at or before `cleared.json` and keeps at most the newest 20 clips (`fewshot.SCORE_WINDOW`).

Minimums: 5 saved clips for a session to count, and `calibrate` refuses a set shorter than 3 with "Too few recordings were kept" (`fewshot.py` 247-248).

### The trainer

`enroll` 2253-2257 sets the stage to training and calls `build_fewshot`. `_build_fewshot` (1683-1718) reads the current clip paths and room-noise paths, pads each clip, embeds them with the listener's openWakeWord feature models, and appends the shipped negative bank (`wake/models/fewshot_negatives.npz`, 210 sentences). Other recorded words are not plain negatives. They go through `pair_check`. Hey Grok is a separate score after calibration (1720-1725). `calibrate` (236-282) picks templates and a threshold.

### How the numbers are produced

Live path, `calibrate`:

- Positive set: the same kept clips, leave-one-out. Clip j is scored against every template except j (249-251). There is no separate held-out recording. A clip is never a template for its own score.
- Negative set: the room-noise embedding when one was saved and is longer than 1 second (1701), plus each of the 210 bank sentences. The bank file is 6252 embedding frames. At 80 ms a frame that is 500.2 seconds, 0.139 hours. It is a list of short clips, not an hour of audio.
- Recall: `heard / n`, where `heard` is how many leave-one-out peaks are at or above the threshold. The pass line is `reps_needed` (106-108): `ceil(0.80 * n)`, so 4 of 5, or 8 of 10.
- False accepts: `training_counts` (133-154) counts negatives whose peak is at or above that same threshold. The rate is that count divided by the number of negatives, not by duration. Nothing on this path divides by hours.
- The pass test (269) is `heard >= need` and `floor >= neg_max + 0.03` and no colliding word. `floor` is the weakest positive that must be heard. `neg_max` is the loudest negative. False accepts per hour is not read.

The threshold (260) is `clip(max(neg_max + 0.03, (floor + neg_max) / 2), 0.5, 0.97)`.

Older path, not used by `enroll`: `enroll_feats.py` 23-31 holds out `max(3, len/5)` clips with `random.Random(len(clips))`. Train and held-out paths are disjoint. `enroll_eval.py` 3-36 scores recall on those held-out wavs mixed with background, and reads `fa_per_hour` from an evaluate report the file describes as 10.7 hours. Among configs with that rate `<= 1.0`, it publishes only if real recall is `>= 0.80`.

### Thresholds

| Check | Where | Value |
|---|---|---|
| Heard | `fewshot.reps_needed`, `calibrate` 259 and 269 | 80% of the current set |
| Gap | `fewshot.MARGIN` 22, `calibrate` 269 | 0.03 above the loudest negative |
| Threshold clip | `calibrate` 260 | 0.50 to 0.97 |
| Pair of words | `SEP_MARGIN` 25, `pair_check` 200 | 0.05 on 80%, both ways, at least 3 templates |
| Hey Grok | `grok_wake.py` 1723, `THRESH` 111 | 0.5 on the built-in model |
| Older trainer | `enroll_eval.py` 33-36, `quality_message` 1970 | recall `>= 0.80` and `fa_h <= 1.0` |

`docs/design.md` (enrollment session) states the live gate as recall and 1.0 false wake per hour. That matches `enroll_eval.py` and `quality_message`, not `calibrate`. `docs/enroll.md` matches `calibrate`.

### Retry, and whether a failed score sticks

A rejected take is not stored. A finished session that fails the gate is kept. `calibrate` returns before `save` when `ok` is false (`grok_wake.py` 1728-1729), so the model file and its `metrics` are not replaced. The next Train more adds another 5 clips. Scoring uses every finished session since the clear, capped at 20. Old failed clips stay in that window until Clear training.

`enroll` 2262-2264 sets `failed_quality` when the message starts with "Not good enough". `derive_enroll_status` (`admin/grok_admin.py` 2067-2071) copies that stage over the saved row. A later `published` stage replaces it (2070-2071). A queued attempt stamps `recording` before the worker runs (`note_enroll_queued`, 1410-1419) so the page does not stay on the previous failure during the gap.

Clear training deletes the session directories and writes `cleared.json`. The listener stamp is `session_stamp` (`grok_wake.py` 2366-2368 and 2419), which uses `time.gmtime`. The admin stamp is the same UTC form (`grok_admin.py` 1309). `current_training_set` (`fewshot.py` 111-130) keeps a session only when its id is strictly greater than that stamp. The comment on `session_stamp` says the enroll directory uses the same UTC form. `enroll` 2130 does not. It uses local time.

`silence_weak` (2124-2147) sets `enabled` false on a `failed_quality` row, including when an older model is still on disk. Nothing in the publish path sets `enabled` back to true. A later pass clears the status row. It does not by itself turn the override back on.

The listener message stored for the admin is cut at 240 characters (`grok_admin.py` 2047).

### What the page says

`quality_message` (1959-1972) is the sentence for the older scores (`recall_real`, `fa_h`). It always prints both numbers and always says "Record more clips, 20-30, somewhere quiet." It also treats a `false_accepts` count as a per-hour rate (`/h`). The live failure does not go through this unless the listener message does not already start with "Not good enough" (2033-2034, 2069).

The live sentence is `calibrate` 276-277: "It heard X of N recordings. Train more to add 5 more recordings, somewhere quiet." plus the score tail. The tail's false-accept count is taken at the operating threshold.

The bots-row pill is the bare string "Not good enough yet" (`admin/web/app.js` 1509). The dialog (1736-1747) ignores the server's next step. If the message contains "N of M" it always says the word needs `ceil(0.80 * N)` and "Five more recordings should help." Otherwise it says the same five-more line, including when the server said to try a different phrase.

The configure score line reads `word.metrics` (`app.js` `trainScoreHTML`, 1408). Those fields are written only after a pass (`grok_wake.py` 1737-1742). A failure leaves them empty, so the page that stays up says "No tries yet" unless an older success is still stored.

The station screen (`training_notice`, 2286-2292) always says "Train more to add 5 more recordings, somewhere quiet." `docs/design.md` keeps that notice short on purpose. The numbers belong on the admin page.

## Causes

### 1. Clear stamp is UTC, the new session id is local. Confirmed by code, reproduced.

`session_stamp` formats `time.gmtime`. `enroll` formats `time.strftime` with no `struct_time`, which is local time. `current_training_set` compares the strings.

Worked example, the production comparison, with the two formatters the code uses. Clear at 2026-11-10 18:00:00 UTC. A recording one minute later on a clock 8 hours behind UTC:

- cutoff written the way `session_stamp` writes it: `20261110-180000`
- session id written the way `enroll` writes it: `20261110-100100`
- `current_training_set([(session, "new-clip"), ("20261109-120000", "old")], cutoff, 20)` returned `[]`

The new clips are on disk and are not scored. `calibrate` then sees fewer than 3 clips and says too few were kept, or it scores whatever older sessions still sort after the stamp. Clear training cannot help on a station whose local clock is behind UTC until local time catches the UTC stamp (8 hours in this example). On a clock ahead of UTC the new id sorts after the stamp, so this direction does not drop it. This environment's clock is UTC, so local and UTC match here. The two call sites still disagree for any other offset.

This does not explain a first attempt that has never been cleared (`cleared_at` empty keeps every session, then the cap of 20).

### 2. The failure text names the wrong check. Confirmed by code, reproduced.

Worked example, `calibrate` on five copies of one embedding pattern (the shape `tests/test_fewshot.py` uses, noise 0.15) plus one near-copy as a negative (noise 0.05). Leave-one-out peaks `[0.663, 0.781, 0.788, 0.704, 0.841]`. Fourth-best, the weakest that must pass, is 0.704. Loudest negative is 0.709. The gap test needs 0.739. The threshold becomes 0.739. Three peaks clear it, so heard is 3 of 5 (recall 0.60, need 0.80). The negative's 0.709 is under 0.739, so the false-accept count is 0.

The sentence was: "Not good enough yet. It heard 3 of 5 recordings. Train more to add 5 more recordings, somewhere quiet. 60% of this set. False rejects 2. False accepts 0."

The count is 0 because the threshold is `neg_max + 0.03` whenever that value sits inside 0.50 to 0.97. Every negative is then strictly under the threshold. The score line added in 0.13.22 cannot show the check that failed. The dialog then says five more recordings should help. More copies of a phrase that already sits on top of another sound do not open the 0.03 gap. The matching next step is a longer or more distinct phrase, or a quieter room (the room clip is one of the negatives when it was recorded).

A second run, five noisy copies of one pattern against quiet negatives, left the gap check passing (weakest needed 0.293, loudest other sound 0.126) and still failed because every peak was under the 0.50 floor, so heard was 0 of 5. That one is a recall miss. "Record 5 more clips, said clearly" matches it. The same sentence is what the code uses for the near-copy case, where it does not match.

Evidence the UI overwrites a better sentence: a collision message (`fewshot.py` 272) already says "Try a different phrase" and has no "N of M". The dialog's other branch (`app.js` 1743) still says five more recordings should help.

### 3. Tiny negative set makes false accepts per hour blow up. Ruled out for the live gate.

The bank is 0.139 hours. One hit divided by that duration is about 7.2 per hour, over a 1.0 per hour line. `calibrate` never divides by duration. `training_counts` divides by the number of negatives. The pass test does not read that rate. An empty negative list sets `neg_max` to 0 and makes the gap check easier, not harder. The voice path's missing room clip is that case. Ruled out as the reason a recording fails.

The older `enroll_eval.py` path does apply a per-hour rate from its report. A phrase whose report is over 1.0 at every threshold publishes nothing, and `result.json` then omits `recall_real` and `fa_h` (`enroll_eval.py` 37-39), so `quality_message` cannot name a number. That path is not what `enroll` calls.

### 4. Train/test leak or a bad split. Ruled out.

Leave-one-out does not score a clip against its own template (`fewshot.py` 249-251). Negatives are scored against the templates that would be saved (254-255), which is the runtime set. `enroll_feats.py` holds clips out of training, and the two lists are disjoint. It is not on the live path. The seed is only `len(clips)`, so two sessions with the same count share a shuffle. That does not put a held-out path into the train list.

### 5. A short or confusable phrase. Plausible, not a code defect.

A short name has a short template (`clip_frames`, 36-45, keeps frames in a window a bit longer than the trimmed clip). The offline note in `docs/enroll.md` says the three gate failures in that run were the shortest phrase, and the gate asked for more recordings. That is the gap check working as written. It is a plausible reason a first attempt fails before anyone presses Clear. It is not a wrong comparison in the code. Changing `MARGIN`, `SEP_MARGIN`, or the 0.50 floor would change the threshold. Not done. See proposals.

### 6. An old failed score is reused after a new training. Ruled out for the decision.

The gate reads the wavs in the current window, not a stored score. A failed build does not write `metrics`. The status message is replaced when the listener reports a new stage. What does stick is the clips (until Clear) and, after `silence_weak`, `enabled: false`.

### 7. Units: a count printed as per hour. Confirmed in `quality_message`, not on the live sentence.

`quality_message` 1963 falls through from `fa_h` to `false_accepts` and prints `/h`. `false_accepts` on a few-shot result is a count (`training_counts` 151). The live listener message is the `calibrate` sentence, which does not go through this fallback when it already starts with "Not good enough". The fallback is still a wrong unit if a count is ever passed in.

### 8. Threshold applied to the wrong metric. Confirmed as the reason the score line stays at 0. Not a wrong pass/fail test.

The pass/fail test uses heard-at-threshold and the 0.03 gap. The false-accept count uses only the threshold. After the threshold is lifted to `neg_max + 0.03`, the count is 0 whenever `neg_max + 0.03` is inside the clip range. The count is not the gap check. The fix is to say the gap check in the sentence. The count's definition is left as it is, so the 0.13.22 score line still means "at the threshold the word would use."

## What this build changes

The sentence names the check that failed and the numbers, and the next step matches that check. The session id uses `session_stamp`, the same UTC clock as `cleared.json`, so a recording after Clear is scored. The 0.50 floor, the 0.03 gap, the 80% line, and the model are unchanged.

## Proposals not built

- Lower the 0.50 threshold floor, or the 0.03 gap, so a short or noisy phrase passes. The noisy synthetic run failed only on the floor: the gap check had already passed. Lowering either number lets more phrases through and raises false wakes. That is a threshold change.
- Score the live gate as false wakes per hour on the 0.139 hour bank. One hit would be about 7 per hour and would fail recordings the gap check accepts.
- When `enroll_eval.py` has no config under 1.0 per hour, still write the best recall and rate into `result.json`. That path is not what the station runs.
- After a later pass, set `enabled` back to true. `silence_weak` turns a weak result off on purpose, including an older model. A pass clears the status and does not touch the override. Turning it on automatically would change that switch. Left as is.
- Put the gate numbers on the station screen. The screen line stays the short notice.
