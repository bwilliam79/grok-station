"""Sound ids from assets/sounds/manifest.json. Callers pass an id, never a path.

The thinking chime is synthesized here (stdlib only). Nothing is downloaded.
"""
import array, json, math, os, re

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


# Thinking chime. Two soft notes, well under speech. The speaker's own volume still applies
# because this is ordinary PCM on the same device. Night/dim scales it down further.
WAIT_TONE_SR = 24000
WAIT_TONE_PEAK = 0.10
WAIT_TONE_QUIET_FLOOR = 0.35
WAIT_TONE_NOTES = ((392.0, 0.14), (523.25, 0.18))  # G4, C5
WAIT_TONE_GAP_S = 0.06


def wait_tone_gain(station_volume=1.0, quiet=False, night_level=20, day_level=90):
    """Full-scale fraction for one chime. station_volume is 0..1 (1 leaves the speaker volume in charge).

    quiet is the night clock or a dim window. The chime then follows night brightness / day brightness,
    and never drops below WAIT_TONE_QUIET_FLOOR of the daytime chime.
    """
    try:
        vol = float(station_volume)
    except (TypeError, ValueError):
        vol = 1.0
    if vol != vol or vol < 0:  # NaN or negative
        vol = 0.0 if vol == vol and vol < 0 else 1.0
    vol = min(1.0, vol)
    gain = WAIT_TONE_PEAK * vol
    if not quiet:
        return gain
    try:
        night, day = float(night_level), float(day_level)
    except (TypeError, ValueError):
        night, day = 20.0, 90.0
    if day <= 0 or night != night or day != day:
        scale = WAIT_TONE_QUIET_FLOOR
    else:
        scale = max(WAIT_TONE_QUIET_FLOOR, min(1.0, night / day))
    return gain * scale


def _note_env(i, n, sr):
    attack = max(1, int(sr * 0.008))
    release = max(1, int(sr * 0.02))
    env = math.exp(-7.0 * (i / sr))
    if i < attack:
        env *= i / attack
    if i > n - release:
        env *= max(0.0, (n - i) / release)
    return env


def wait_tone_pcm(peak):
    """PCM16 mono at WAIT_TONE_SR. peak is the full-scale fraction from wait_tone_gain. Empty when silent."""
    try:
        peak = float(peak)
    except (TypeError, ValueError):
        return b""
    if peak != peak or peak <= 0:
        return b""
    peak = min(1.0, peak)
    sr = WAIT_TONE_SR
    seq = []
    gap = [0.0] * int(sr * WAIT_TONE_GAP_S)
    for n_i, (freq, dur) in enumerate(WAIT_TONE_NOTES):
        if n_i:
            seq.extend(gap)
        n = int(sr * dur)
        for i in range(n):
            seq.append(math.sin(2 * math.pi * freq * (i / sr)) * _note_env(i, n, sr))
    loudest = max(abs(s) for s in seq) or 1.0
    scale = peak * 32767.0 / loudest
    out = array.array("h", (max(-32767, min(32767, int(s * scale))) for s in seq))
    return out.tobytes()
