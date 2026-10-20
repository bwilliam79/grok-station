# Enroll mode: real-voice wake-word training

Goal: record 20-30 real clips of a wake phrase per person on the station, train on the training host with those
clips mixed into the synthetic set, gate on the same bar (recall >= 0.80, <= 1.0 false wakes per hour), and have
the station load the new model and its threshold/patience automatically. Never started by voice.

```
 LAN client ──POST /enroll (show token)──▶ display :8787 ──▶ wake listener control 127.0.0.1:8789
 or on the Pi:  grok_wake.py --enroll hey_finn 25 ───────────────────────▶ (same control endpoint)
                                                                                 │ pauses wake detection
 display "enroll" screen ◀── POST /state {"state":"enroll", phrase, i, n} ───────┤ chime, 2 s capture per clip,
                                                                                 │ level/clip checks, retry
 relay (mTLS + relay token) ◀── POST /enroll/<phrase>/<session>/<NN>.wav ────────┤ upload each clip
                            ◀── POST /enroll/<phrase>/<session>/done ────────────┘
   writes $GROK_STATION_ENROLL/<phrase>/<session>/  (inbox, host dir)
 training host: enroll_watch.sh (launchd, every 60 s) ── READY session ──▶ enroll_train.sh (nice 10, caffeinate)
   1 synthetic clips + features for the phrase (generated once if missing, e.g. hey_hank)
   2 real clips: 80 % train (augmented x ROUNDS with RIRs + background), 20 % held out
   3 train a variant from synthetic + real positive features, kept background/ACAV negatives
   4 evaluate2 (FA/h on the 10.7 h validation set) + held-out real-clip recall per threshold/patience
   5 gate: pick FA/h <= 1.0 config with best real recall; publish only if real recall >= 0.80
     → $GROK_STATION_WAKEWORDS/<phrase>.onnx + <phrase>.json (threshold, patience, persona, metrics)
   watchdog (same watch script): job dead without a result, or log idle > 30 min → STALL in enroll.log,
   one automatic restart, then marks the session FAILED
 Pi: sync_wakewords (startup, hourly, every 5 min for 3 h after an upload) GET /wakewords + /wakeword/<file>
   → /opt/grok-station/wake/models/enrolled/ + wakewords.enrolled.json (sha256 checked), detector reloads
   between sessions; enrolled entries override the repo config for the same phrase.
```

On the station screen, a wake word with no persona shows the Echo mark on a surface-2 circle (not a portrait file). The boot splash is the same Echo brand; `tools/install-splash.sh` installs it.

Privacy: clips stay on the LAN (Pi local copy + training host inbox). No speaker names: sessions are timestamps.
Tokens stay in the existing secret files (`/etc/grok-station/env`, `/etc/grok-station/wake.env`, relay secrets).
Phrases accepted: `^hey_[a-z]{2,16}$` and listed in `wakewords.json` (`words` or `enroll`). The station recorder
also accepts `hey_<bot id>` (a letter first, then letters, digits, or hyphens). The relay upload may still only
accept the letter-only form. The recorder asks for **20–30 clips, default 25**, on the admin page, the admin API,
and the wake listener (`count must be 20-30`). 16 kHz mono PCM16 WAV.

`enroll/status.json` (and each session's `.outcome.json`) uses `recording`, `processing`, `published`,
`failed_quality`, or `failed`. `done` is not a success. "JOB DONE published" is `published`. "JOB DONE below the
gate, not published" is `failed_quality` and keeps the gate scores (recall and false accepts versus recall 0.80 and
1.0 false accepts per hour). A check run ("JOB DONE check (…), not published") is `failed` and is never published.
When the wake listener sees `published` or `failed_quality`, the station display shows a `notice` for about 10
seconds: "Hey Finn is ready", or "Hey Finn needs more clips. Record 20-30 somewhere quiet." A hard failure stays on
the enrollment error screen. A tool card still holds 4 seconds after a session; that is separate from the notice.
