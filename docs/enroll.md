# Custom wake words

Hey Grok is built in. Every other wake word is made on the station from your own voice. You record the phrase 10
times and the station builds the word from those recordings in a few seconds. There's no training computer and no
upload.

## Make a wake word for a bot

1. Open the admin page, go to **Bots**, and open **Configure** on the bot. Hey Grok is built in and always on, and it is not configured here.
2. Under **Wake word**, press **Set wake word**. It starts as "Hey Kai". Two or three syllables works best.
3. Press **Start recording**. Stand where you normally talk to the station, and keep the room quiet. The station listens to the room for 3 seconds first, so stay quiet until the first chime.
4. Say the phrase after each chime, 10 times, the way you would normally say it. A clip it can't use is asked again right away, so the word is built from 10 kept recordings.
5. Then it shows one of these:
   - **Ready.** Say the phrase and the station starts that bot. It's on now.
   - **Not good enough yet.** Press **Train more** to record 10 more. The first 10 are kept, and the word is rebuilt. You can do this more than once.
   - Too close to Hey Grok, or already used by another bot. A word that already belongs to this bot, or to a bot that was deleted, can be used again. Otherwise pick a different phrase.
6. **Remove wake word** stops listening and leaves the recordings under **Unused wake words** on the Bots page. **Delete** there removes every recording session, the trained model and the row. It still works when the recordings are already gone. Deleting the bot moves its wake word to Unused the same way; it does not delete the recordings. Adding that same bot id again reclaims the word. A word whose bot is gone, including one that was never finished or was switched off, is listed there too, and Delete works for it.

"Hey Grok, ask Kai …" always works too, with no recording. You can also say "Hey Grok, can you connect me with Kai" and the station switches the conversation over. See [bots.md](bots.md).

## How it works

- **Same features as Hey Grok.** openWakeWord turns every 80 ms of sound into a short list of numbers
  (`melspectrogram.onnx` and `embedding_model.onnx`). Hey Grok's model reads that stream, and so do custom words.
  This adds no audio processing (`wake/fewshot.py`).
- **Templates.** Each recording becomes a template: the frames that cover the spoken phrase, up to 1.44 s.
  At most 20 recordings per word are kept, the ones most like the others.
  Only finished sessions count: a session with its `session.json` and 10 clips (5 for Train more). Empty, unfinished and duplicate sessions are skipped, so the "N recordings" shown matches what you recorded.
- **Matching.** Each new frame is compared with every template using streaming dynamic time warping with cosine
  distance, so a slower or faster delivery still lines up. The word's score is the mean of its best two template
  scores. It has to stay over the word's threshold for 2 frames in a row (patience). The listener's normal cooldown
  applies after any wake.
- **How strict it is (calibration).** Each recording is scored against the other 9 (leave one out). The station
  also scores these, which must never wake it:
  - the 3 s of room noise recorded before the first chime
  - a bank of everyday speech shipped with the station (`wake/models/fewshot_negatives.npz`, 210 sentences from
    synthetic voices)
  - your other custom words
  - Hey Grok
  The threshold sits between the loudest of those and the recordings it must hear.
- **The quality gate.** The word is ready when at least 8 of 10 recordings are heard (80% after Train more) and the
  weakest of them still beats the loudest negative by 0.03. Otherwise the station shows "Not good enough yet". If
  Hey Grok fires on your recordings, or another of your words does, the phrase is refused.
- **Files on the Pi.**
  - Recordings and room noise: `/var/lib/grok-station/enroll/inbox/<phrase>/<session>/`
  - The built word: `/opt/grok-station/wake/models/enrolled/<phrase>.fewshot.npz`, listed in
    `wakewords.enrolled.json` as `"kind": "fewshot"` with its threshold
  - The sensitivity slider doesn't change a recorded word's threshold. Train more to make it better.
- **CPU.** Matching costs about 0.05 ms per word per 80 ms frame on a desktop CPU. Expect roughly 0.3 to 0.5 ms on
  a Pi 4 and about twice that on a Pi 3B, so under 1% of a core per word. That's on top of the feature models that
  run for Hey Grok anyway. Building a word took 1.7 s on a desktop, so expect about 10 to 30 s on a Pi. Check on
  the Pi with `sudo -u grok /opt/grok-station/wake/venv/bin/python /opt/grok-station/wake/grok_wake.py --bench some.wav`.
  A 16 kHz mono recording works, or noise if you give a number of seconds.
- **Who it works for.** It's tuned to the voice that recorded it. Other people may or may not wake it.

## Offline eval (dev only)

```
python3 tools/fewshot_tts.py en_US-libritts_r-medium.onnx /tmp/fsdata   # Piper TTS, 12 synthetic voices
python tools/fewshot_eval.py /tmp/fsdata --bank wake/models/fewshot_negatives.npz
```

Run these with openwakeword 0.6.0 and onnxruntime installed, like the wake venv. The eval covers 4 phrases (hey ash,
hey jules, hey rhea, okay milo), each enrolled from 4 voices with 10 recordings each.

- True accepts: 20 more takes from the same voice, with room noise at 15 dB SNR.
- False accepts: 10 minutes of everyday speech and near misses from voices not used in the bank, plus the other
  phrases.

Results at 0.10.0 (synthetic voices, so a sanity check, not a real-room number):

| | Result |
|---|---|
| Passed the gate | 13 of 16 enrollments. The 3 failures were all "hey rhea", the shortest phrase, which correctly asked for Train more. |
| Same-voice true accept | 92.7% |
| Other-voice accept | 55% (it's tuned to one voice) |
| False accepts | 0 in 2.5 h of speech |

Real rooms, real mics and real voices will be worse than this.

## Advanced: a model trained on another computer

The older path still works. A computer with the `train/` scripts can train a full openWakeWord model from the
clips in the inbox. Put the trained `<phrase>.onnx` and its `<phrase>.json` (with `sha256`, `threshold`, `patience`
and `persona`) in `/var/lib/grok-station/wakewords`, then run `grok_wake.py --sync` or wait for the listener's sync.
An uploaded model replaces a recorded word with the same name. See `train/README.md`.

Privacy: recordings stay on the station. There is no speaker id, and nothing is uploaded.
