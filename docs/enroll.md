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

Privacy: clips stay on the LAN (Pi local copy + training host inbox). No speaker names: sessions are timestamps.
Tokens stay in the existing secret files (`/etc/grok-station/env`, `/etc/grok-station/wake.env`, relay secrets).
Phrases accepted: `^hey_[a-z]{2,16}$` and listed in `wakewords.json` (`words` or `enroll`). Limits: 40 clips per
session, 5 s per clip, 16 kHz mono PCM16 WAV.
