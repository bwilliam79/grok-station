# Multi-turn sessions (follow-ups without the wake word)

## Goal
After a reply, keep the session open for a follow-up about 10 s long. Anyone can ask another
question without saying the wake word. The display shows the pulsing listening ring between turns. "Thanks" or "that's
all" ends it, and so do silence and the caps. The same persona answers every turn.

## Flow (one WebSocket, one xAI realtime session)
1. Wake word, then the Pi opens `/ws?persona=…` as before (turn 1).
2. On the final `response.done` the relay sends `{"type":"turn_done"}` (not `done`) and marks the turn as
   *playing*. While it is playing, the NO_SPEECH timer is held, because reply audio reaches the Pi faster than real time.
3. The Pi finishes playback and drains any queued mic audio. It shows `listening` for the same persona, resumes
   streaming, and sends `{"type":"listening"}`.
4. The relay clears the xAI input buffer (dropping any echo), starts the follow-up clock and resets NO_SPEECH. The
   existing NO_SPEECH close (10 s with no speech and no response) then ends an unanswered follow-up with
   `{"type":"timeout"}`. After turn 1 the Pi treats that as a quiet end, not as "Didn't catch that".
5. A follow-up whose transcript is a short closing phrase (thanks, thank you, that's all, that's it, no thanks,
   nothing else, bye, goodbye; at most 6 words) cancels any response. The relay sends `done` and closes.
6. The persona doesn't change: the xAI session and `st["persona"]` carry over. A `talk_to` switch made in an
   earlier turn also carries over.

## Caps
- **Per turn:** `MAX_SESSION` (60 s), measured from the start of the turn (session start, or the Pi's `listening`).
- **Total:** `MAX_TOTAL` (180 s) per connection.
- **Upstream audio:** `MAX_UP_SECONDS` now defaults to `MAX_TOTAL + 5`.
- **No daily limit:** there is no daily talk-time or session cap (`MAX_MINUTES_DAY` and `usage.json` were removed).
  Teammate handoffs keep their own per-bot and global daily caps (docs/handoff.md).
- **Kill switch:** `MULTI_TURN=0` restores the old one-answer-per-wake behavior.

## Compatibility
An old Pi build never sends `listening`, so the turn stays *playing*. The per-turn cap then closes the session
within 60 s, as before. An old relay never sends `turn_done`, so a new Pi behaves exactly as before.

## Tests
`tests/test_relay.py` runs the real `session()` handler against a fake xAI realtime server (aiohttp test server).
It covers:
- two turns in one session, with the same persona and only one session counted;
- the follow-up NO_SPEECH close;
- a "thanks" ending;
- the total cap;
- the per-turn cap;
- `MULTI_TURN=0`.
