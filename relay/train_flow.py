"""Decisions for voice-started wake-word training. No audio device and no numpy.

The capture thread owns the microphone for the whole session, including the
1.5 s gap, so a word heard there is not a command.
"""

ENROLL_COUNT = 5
RETRY_S = 1.5
CANCEL_SAY = "Stopped. Nothing new was saved."
QUIET_SAY = "I didn't hear it. Again after the chime."
SILENT_TOUCH_SAY = "I didn't hear anything. Nothing new was saved."
SILENT_TOUCH_NOTE = "Tap Record voice to try again."
COUNT_CAPTION = "Recording in 3…"
GROK_BUILTIN_SAY = ("Grok's wake word is built in and can't be trained. "
                    "Say a bot's name to train its wake word.")


def wake_speak_label(phrase, name=""):
    """The word said after the chime, without a leading Hey."""
    raw = ""
    if isinstance(phrase, str) and phrase.startswith("hey_"):
        raw = phrase[4:]
    elif isinstance(phrase, str) and phrase:
        raw = phrase
    words = raw.replace("-", " ").replace("_", " ").split()
    label = " ".join((w[:1].upper() + w[1:]) for w in words if w)
    if label:
        return label
    who = " ".join(str(name or "").split())
    return who or "it"


def voice_train_plan(hit, has_model=False):
    """What one train sentence does. A matched phrase starts at get-ready, not the prompt tile.

    An unknown name is spoken and does not open a tile. Grok's wake word is built in:
    that sentence is spoken once, shown for 4 s, and does not record.
    """
    hit = hit if isinstance(hit, dict) else {}
    slug = str(hit.get("slug") or "")
    phrase = hit.get("phrase") if isinstance(hit.get("phrase"), str) and hit.get("phrase") else None
    spoken = " ".join(str(hit.get("spoken") or hit.get("name") or "").split())
    if not slug:
        who = spoken or "that"
        return {"begin": False, "phase": None, "say": "I don't have a bot called %s." % who,
                "enroll": None, "tile": False}
    if slug == "grok" or phrase == "hey_grok":
        return {"begin": False, "phase": None, "say": GROK_BUILTIN_SAY,
                "enroll": None, "tile": False, "hold_s": 4}
    if not phrase:
        return {"begin": False, "phase": None, "say": "", "enroll": None, "tile": False}
    label = wake_speak_label(phrase, hit.get("name"))
    topup = bool(has_model)
    if topup:
        say = "Okay. I'll add 5 more. Say %s after each chime. To stop, stay quiet." % label
    else:
        say = "Okay. I'll record you 5 times. Say %s after each chime. To stop, stay quiet." % label
    return {
        "begin": True,
        "phase": "ready",
        "say": say,
        "tile": False,
        "label": label,
        "enroll": {
            "phrase": phrase,
            "persona": slug,
            "count": ENROLL_COUNT,
            "topup": topup,
            "voice": True,
        },
    }


def train_steps(start, n=ENROLL_COUNT, gap_s=RETRY_S):
    """On-screen steps. Voice and a Record voice tap both count 3, 2, 1 once, then not again.

    Countdown seconds are not announced. Each clip is announced once.
    """
    n = max(1, int(n))
    steps = []
    for num in (3, 2, 1):
        steps.append({"kind": "count", "n": num, "caption": COUNT_CAPTION, "announce": None, "start": start})
    for i in range(1, n + 1):
        listen = "Listening… %d of %d" % (i, n)
        saved = "%d of %d saved" % (i, n)
        steps.append({"kind": "listen", "n": i, "line": listen, "announce": listen, "window": "recording"})
        row = {"kind": "saved", "n": i, "line": saved, "announce": saved}
        if i < n:
            row["cue"] = "Next"
            row["gap_s"] = gap_s
        steps.append(row)
    return steps


def silent_clip(streak, bot, voice=True):
    """The first silent clip retries. The second silent clip in a row stops the session.

    voice=True keeps the spoken Hey Grok line. A Record voice tap (voice=False) uses the touch lines.
    """
    who = " ".join(str(bot or "").split()) or "the bot"
    if int(streak) >= 2:
        if voice:
            return {
                "action": "stop",
                "say": "Stopped. Say 'Hey Grok, train my wake word for %s' to try again." % who,
            }
        return {"action": "stop", "say": SILENT_TOUCH_SAY, "note": SILENT_TOUCH_NOTE}
    return {"action": "retry", "say": QUIET_SAY}


def mic_stopped_line(bot):
    who = " ".join(str(bot or "").split()) or "the bot"
    return ("The microphone stopped while recording. "
            "Say 'Hey Grok, train my wake word for %s' or press Record voice to try again." % who)
