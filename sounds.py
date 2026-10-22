"""Sound ids from assets/sounds/manifest.json. Callers pass an id, never a path."""
import json, os, re

ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_CATS = ("alarm", "listen", "end", "fail")
_KEYS = {"ALARM_SOUND": "alarm", "LISTEN_TONE": "listen", "END_TONE": "end"}


def root():
    """Directory that holds manifest.json and the wavs. An override is for tests."""
    env = os.environ.get("GROK_STATION_SOUNDS")
    if env:
        return env
    opt = "/opt/grok-station/assets/sounds"
    if os.path.isfile(os.path.join(opt, "manifest.json")):
        return opt
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "sounds")


def _root_real():
    return os.path.realpath(root())


def manifest():
    try:
        with open(os.path.join(root(), "manifest.json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = {}
    for cat in _CATS:
        ids = data.get(cat) or []
        out[cat] = []
        if not isinstance(ids, list):
            continue
        for i in ids:
            if isinstance(i, str) and ID_RE.fullmatch(i) and i not in out[cat]:
                out[cat].append(i)
    return out


def _wav(sid):
    """Absolute wav for one catalog id, or None. A symlink that leaves the catalog is refused."""
    if not isinstance(sid, str) or not ID_RE.fullmatch(sid):
        return None
    base = _root_real()
    real = os.path.realpath(os.path.join(base, sid + ".wav"))
    try:
        if os.path.commonpath([base, real]) != base:
            return None
    except ValueError:
        return None
    return real if os.path.isfile(real) else None


def present(cat):
    """Ids in this category whose wav is actually in the catalog."""
    return [i for i in manifest().get(cat, []) if _wav(i)]


def chime_path():
    """Today's wake chime. WAKE_CHIME, when set, is the only candidate (a missing file means no chime)."""
    explicit = os.environ.get("WAKE_CHIME")
    if explicit:
        return os.path.realpath(explicit) if os.path.isfile(explicit) else None
    here = os.path.dirname(os.path.abspath(__file__))
    for p in ("/opt/grok-station/wake/chime.wav", os.path.join(here, "wake", "chime.wav")):
        if os.path.isfile(p):
            return os.path.realpath(p)
    return None


def alarm_options():
    return present("alarm")


def listen_options():
    opts = ["none"]
    if chime_path():
        opts.append("chime")
    opts.extend(present("listen"))
    return opts


def end_options():
    return ["none"] + present("end")


def choices():
    alarm = alarm_options()
    if "alarm-1" in alarm:
        adef = "alarm-1"
    elif alarm:
        adef = alarm[0]
    else:
        adef = ""
    listen = listen_options()
    return {
        "alarm": alarm,
        "listen": listen,
        "end": end_options(),
        "defaults": {
            "alarm": adef,
            "listen": "chime" if "chime" in listen else "none",
            "end": "none",
        },
    }


def default_value(key):
    slot = _KEYS.get(key)
    if not slot:
        return ""
    return choices()["defaults"][slot]


def allowed(key, value):
    """True when value is an id the Audio card may store. Never a path."""
    if not isinstance(value, str):
        return False
    if key == "ALARM_SOUND":
        opts = alarm_options()
        return value == "" if not opts else value in opts
    if key == "LISTEN_TONE":
        return value in listen_options()
    if key == "END_TONE":
        return value in end_options()
    return False


def file_for(sid):
    """Wav path for a catalog id, the chime, or None. 'none' and anything unlisted do not play."""
    if not isinstance(sid, str) or sid in ("", "none"):
        return None
    if sid == "chime":
        return chime_path()
    listed = manifest()
    if not any(sid in listed[c] for c in _CATS):
        return None
    return _wav(sid)


def fail_file():
    """Fixed tone for a relay timeout or a failed webhook POST. Not a setting."""
    return file_for("comms-fail-2")
