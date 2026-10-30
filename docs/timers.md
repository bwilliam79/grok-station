# Timers

The station runs up to eight kitchen timers at once. They keep going after the conversation ends, show on the idle screen, and ring here. Nothing in this feature places a call, sends a message, or runs a command. A timer name is plain text: letters, numbers, spaces, apostrophes and hyphens, at most 24 characters. Anything else is dropped. The name is drawn as text, not as HTML.

## Voice

Every persona gets four tools: `set_timer`, `list_timers`, `cancel_timer`, `extend_timer`.

- "Set a pasta timer for 10 minutes" stores the name **pasta** (a trailing "timer" is not kept).
- A timer with no name is **Timer**. The next unnamed one is **Timer 2**, then **Timer 3**.
- "What's running?" lists them soonest first. A ringing timer is first.
- "Stop", "stop the timer", or "dismiss timer" clears every ringing timer at once. Running timers stay. Grok confirms with the names, as in "Stopped Orzo, Garlic bread and Timer." To drop just one, say "cancel the <name> timer".
- Bare "dismiss" while a timer is ringing clears the one that ended first. With nothing ringing, and a recipe up, "dismiss" or "clear that" still removes the newest recipe chip.
- If several are running and none is ringing, "stop" with no name asks which one.
- "Stop the pasta timer", "cancel the oven", "the second one", or "timer 2" pick one. A stored name wins over an index, so a timer named "Timer 2" is that name, not index 2. A bare "2" is the index.
- "Add two minutes to the pasta timer" extends it. Extending a ringing timer starts it again.
- A ninth timer is refused until one is cancelled. One second to 24 hours.

A timer that has reached zero stays in the list as ringing, even if a conversation has not finished and the chime is waiting. "Stop" still dismisses it. One that is more than an hour late is dropped quietly, the same rule the display uses.

## Screen

On the idle screen, up to four timers are countdown rings (soonest first). The rest are chips along the bottom. The ring is teal and shrinks with the time left. Closing a recipe leaves the timers on this idle screen; it does not cancel them.

When one is done, that timer takes the idle screen: a full amber ring, a two-line headline ("Pasta is done"), and the hint Say “stop” to dismiss in the shared voice-hint style (muted connective words, the spoken word in the text colour). Every other timer is a chip. The amber ring pulses about once a second. `GROK_STATION_REDUCE_MOTION=1` leaves a static outline instead of the pulse. These screens do not use red. Red on the station is only the microphone-off state and a stock that went down.

The alarm sounds when the timer ends, then again every 10 seconds for 2 minutes, then every 60 seconds, until it is dismissed or the one-hour drop.

While a recipe is up, timers use the right-hand rail instead of the bottom strip (see display.md). A plain unnamed timer reads "Timer · 5 min" with the line "Set by voice". "Timer 2" keeps that name. A timer started from a recipe step is named after the card's `dish` ("Orzo") or "Step N" when the card has no dish, and its source line is "From step N". Ringing timers come first, in the order they ended, then running timers, oldest first. With more than three timers and at most two ringing, slot 3 is "+N more", soonest first, up to three rows. With more than two ringing, slots 1 and 2 keep the full ringing treatment and slot 3 is "+N more" with an amber border and the alert fill, no bell ring. Its rows list the other ringing timers as a bell plus "<Name> · done", then running timers by time left, up to three rows. A ringing timer stays on the rail. "Stop" clears every ringing timer and the rail slides once. Dismissing or cancelling slides the later timers up to fill the gap (240 ms, or instant under reduced motion). Nothing outside the rail moves. While any timer is ringing, an amber banner replaces the sports and calendar chips: one name ("Orzo timer is done"), two ("Orzo and Garlic bread are done"), or two names and "+ N more". Each name is capped; "+ N more" is not. The chips come back when nothing is ringing. A step timer offer is made once per step for the life of the recipe, not once per wake: a declined step is offered again only on "repeat". A "yes" spoken while the offer is still playing is ignored; answer after it finishes. If "yes" cannot set the timer (eight already running), Grok says so.

During a conversation that is not a recipe, the timers stay a chip strip at the bottom, so the reply stays visible. The night clock hides them. The digits follow a monotonic clock taken when the list arrives, so a wall-clock step does not jump them.

## What is stored

Two files, both rewritten by replacing a temporary file:

- Wake listener: `/opt/grok-station/wake/timers.json` (override `WAKE_TIMERS`). This is the ringer. It stores a wall deadline and, while the process is up, a monotonic deadline. A restart re-bases the monotonic clock from the wall deadline.
- Relay: `/var/lib/grok-station/state/timers.json` (override `GROK_TIMER_STATE`). This is the voice book, so a relay restart still knows the names. While the relay process is up, deadlines use a monotonic clock. The file itself stores remaining time against wall time, because the monotonic clock does not survive a restart.

Each new voice session sends the wake listener's list. That list wins. If a client sends no list, the relay reads its file and hands the timers back. A ringing one is handed back as already done, not as a new countdown. The station does not open a new port for this.

## Validation on the Pi

What changed: up to eight named timers, set and dismissed by voice, drawn as countdown rings. A finished timer is an amber pulse until you dismiss it. Both the relay and the wake listener keep a state file, and a wall-clock step does not finish a timer early.

How to test, after installing this tree and restarting `grok-station`, `grok-station-wake`, and `grok-station-relay`:

1. Say "set a pasta timer for one minute" and "set an oven timer for five minutes". The idle clock gives way to two teal rings, Pasta and Oven.
2. Ask what is running. Pasta is first (it finishes sooner), then Oven.
3. Say "add a minute to the pasta timer". The pasta ring moves forward by about a minute.
4. When pasta finishes, the screen is one amber ring, the words "Pasta is done", and Oven as a chip. There is no red. Say "stop". The amber screen clears and the oven ring remains.
5. Say "set a timer" with no name. It is called Timer. A second unnamed one is Timer 2.
6. Restart the relay (`sudo systemctl restart grok-station-relay`), then the wake listener. The oven timer is still counting, with about the time it had left. A timer that was ringing can still be dismissed with "stop".

Expected result: a ninth timer is refused. Names ignore punctuation. After either restart, a running timer keeps its remaining time, and "stop" or "stop the pasta timer" dismisses the one you named.
