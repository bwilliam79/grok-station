"""Late teammate answers: the spoken prefix and when to speak, queue, or drop.

Nothing here talks to the network or the speaker. The relay calls these.
"""
import re

CHECKING_LINE = "Checking, one moment."
ANSWER_BY_DEFAULT = 180   # reply_by and pending validity for a spoken answer
CHECKING_AFTER_DEFAULT = 8
LATE_QUEUE_DEFAULT = 120  # how long a late answer may wait for the station to be idle

# Longer topics first so "notes" wins over "note".
_TOPICS = ("calendar", "schedule", "weather", "recipe", "score", "timer", "list",
           "notes", "note", "reminder")
_STOP = frozenset(
    "a an the of to for on in my your our is are was were what what's whats who when where "
    "why how please tell me about and or it its it's this that with from at be do does did "
    "can could would should just hey".split())
_WORDS = re.compile(r"[a-z0-9']+")


def late_topic(question):
    """A short topic for the spoken prefix. A known word wins; otherwise a few content words."""
    words = _WORDS.findall(str(question or "").casefold())
    for topic in _TOPICS:
        if topic in words:
            return topic
    kept = [w for w in words if w not in _STOP and len(w) > 2]
    if not kept:
        return "question"
    return " ".join(kept[:3])


def late_line(name, question, answer):
    """'Rhea, about your calendar: Dinner is at 6.' The answer is not reworded."""
    who = " ".join(str(name or "").split()) or "The bot"
    text = " ".join(str(answer or "").split())
    return f"{who}, about your {late_topic(question)}: {text}"


def late_decide(now, hard_at, busy, queue_until=None):
    """'speak', 'queue', or 'drop'.

    Drop once the accept window or the idle-queue window is past. Speak when the station
    is idle and still inside both. Otherwise keep it queued.
    """
    if now > hard_at:
        return "drop"
    if queue_until is not None and now > queue_until:
        return "drop"
    if busy:
        return "queue"
    return "speak"
