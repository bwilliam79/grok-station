"""T1 voice notes on the relay side (docs/handoff.md, "T1"). OFF unless T1_ENABLED=1.
The relay never authorizes a write. It shows and reads back a bot's proposed note, listens for the confirm at the
station, and records what happened in CONFIRMS_DIR for tile-ingest, which alone holds the receipt key.
Spoken confirm rules (Nix): station mic only, only after the read-back has finished playing, within 15 s, and the whole
utterance must be "yes, save it". The read-back never contains that phrase. Shared (non own-notes) changes need a tap.
Gates: guest mode off, not 10 PM to 7 AM, and a physical tap on the station in the last 30 min ("recent tap")."""
import datetime, json, os, re, secrets, time
import safe_io
from zoneinfo import ZoneInfo

E = os.environ.get
ENABLED = E("T1_ENABLED", "0") == "1"
CONFIRMS_DIR = E("T1_CONFIRMS_DIR", "/voice/confirms")
TZ = E("TZ_NAME", "America/Chicago")
WINDOW_S = 15            # spoken confirm window after the read-back finishes
CARD_S = 30              # how long the card (and a tap) stays valid after the read-back starts
RECENT_TAP_S = 30 * 60   # T1 needs a physical tap on the station within this long
QUIET = (22, 7)          # T1 off from 10 PM to 7 AM (station time zone)
PHRASE = "yes, save it"
VERBS = {"add": "add", "remove": "remove", "remember": "remember", "undo": "undo the note"}


def norm(t):
    return " ".join(re.sub(r"[^a-z ]", " ", str(t or "").lower()).split())


def is_phrase(transcript):
    """The whole utterance must be the phrase (punctuation and case ignored)."""
    return norm(transcript) == "yes save it"


def parse_station(raw, now=None):
    """Pi query ?t1=<json {guest: bool, last_tap: epoch s}>. Missing or malformed means guest on, no tap (T1 off)."""
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    tap = d.get("last_tap")
    now = now or time.time()
    tap = float(tap) if isinstance(tap, (int, float)) and 0 < tap <= now + 5 else 0.0
    return {"guest": d.get("guest") is not False, "last_tap": tap}


def gate(station, now=None):
    """Reason T1 is unavailable right now, or None."""
    if not ENABLED:
        return "off"
    now = now or time.time()
    h = datetime.datetime.fromtimestamp(now, ZoneInfo(TZ)).hour
    if h >= QUIET[0] or h < QUIET[1]:
        return "quiet hours"
    if station.get("guest", True):
        return "guest mode"
    if now - station.get("last_tap", 0) > RECENT_TAP_S:
        return "no recent tap"
    return None


GATE_SAY = {"off": "Saving notes from the station isn't turned on.",
            "quiet hours": "I can't save notes from the station between 10 at night and 7 in the morning.",
            "guest mode": "Saving notes is off while guest mode is on.",
            "no recent tap": "To save notes from here, press the call button on the speaker first, then ask again."}


def readback(name, note):
    """Fixed template. Never contains the confirm phrase (or "save"), so playback can't confirm itself."""
    verb = VERBS.get(note["op"], note["op"])
    where = f" to the {note['list']} list" if note["op"] == "add" and note.get("list") else (
        f" from the {note['list']} list" if note["op"] == "remove" and note.get("list") else "")
    how = "press the call button on the speaker" if note["needs_tap"] else "say the phrase on the screen, or press the call button"
    return f"{name} would like to {verb}{where}: {note['text']}. To keep it, {how}. Anything else cancels."


class Pending:
    def __init__(self, rid, persona, note, now=None):
        self.rid, self.persona, self.note = rid, persona, note
        self.phase = "readback"
        self.started = now or time.time()
        self.readback_done = None
        self.done = False

    def open(self, now=None):
        """The Pi finished playing the read-back: the spoken window opens now."""
        if self.phase == "readback":
            self.phase, self.readback_done = "open", (now or time.time())

    def judge_speech(self, transcript, now=None):
        """-> confirm | decline | early | expired | tap-required. "early" = heard during the read-back: never counts."""
        now = now or time.time()
        if self.phase == "readback":
            return "early"
        if now - self.readback_done > WINDOW_S:
            return "expired"
        if is_phrase(transcript):
            return "tap-required" if self.note["needs_tap"] else "confirm"
        return "decline"

    def judge_tap(self, now=None):
        now = now or time.time()
        return "confirm" if now - self.started <= CARD_S + WINDOW_S else "expired"


def write_event(p, verdict, how, source, station, now=None, confirms_dir=None):
    """Record the outcome for tile-ingest (which issues a receipt only for a valid confirm). No text, only the hash."""
    now = now or time.time()
    d = confirms_dir or CONFIRMS_DIR
    os.makedirs(d, exist_ok=True)
    doc = {"v": 1, "request_id": p.rid, "persona": p.persona, "proposal_sha": p.note["sha"], "verdict": verdict, "how": how,
           "source": source, "at_ms": int(now * 1000), "readback_done_ms": int(p.readback_done * 1000) if p.readback_done else None,
           "station": {"guest": station.get("guest", True), "last_tap_ms": int(station.get("last_tap", 0) * 1000)}}
    tmp = os.path.join(d, f".{p.rid}.{secrets.token_hex(4)}.tmp")
    safe_io.write_file(tmp, json.dumps(doc), "t1 confirm", mode=0o644, excl=True)
    os.replace(tmp, os.path.join(d, p.rid + ".json"))
    p.done = True
    return doc
