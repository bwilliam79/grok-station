"""Soft chime while the station is thinking.

The display ring and this chime share one phase: whatever post_state sends.
A chime starts WAIT_TONE_DELAY after thinking begins, then every WAIT_TONE_EVERY,
and stops before any other playback so it never overlaps speech.
"""
import importlib.util
import json
import os
import subprocess
import threading
import time

WAIT_TONE_DELAY = 0.4
WAIT_TONE_EVERY = 2.5
_TONE_FILE = "/var/lib/grok-station/admin/wait-tone.json"


def wait_tone_enabled(path=None):
    """Default on. A bool `on` in the Audio-page file wins. Anything else stays on."""
    path = path or os.environ.get("WAIT_TONE_FILE", _TONE_FILE)
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("on"), bool):
        return data["on"]
    raw = os.environ.get("WAIT_TONE", "")
    if raw == "":
        return True
    return raw.strip().lower() not in ("0", "false", "off", "no")


def wait_tone_style_saved(path=None):
    """"soft" (0.12.11 default) or "classic" from the Audio-page file."""
    path = path or os.environ.get("WAIT_TONE_FILE", _TONE_FILE)
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = None
    style = data.get("style") if isinstance(data, dict) else None
    return style if style in ("soft", "classic") else "soft"


def _load(name, candidates):
    cached = getattr(_load, name, None)
    if cached:
        return cached
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location(name, path)
        if not spec or not spec.loader:
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            continue
        setattr(_load, name, mod)
        return mod
    return None


def sounds_mod():
    here = os.path.dirname(os.path.abspath(__file__))
    return _load("grok_station_sounds", [
        os.environ.get("GROK_STATION_SOUNDS_PY"),
        os.path.join(here, "..", "sounds.py"),
        "/opt/grok-station/sounds.py",
    ])


def dimming_mod():
    here = os.path.dirname(os.path.abspath(__file__))
    return _load("grok_station_dimming", [
        os.path.join(here, "..", "app", "dimming.py"),
        os.path.join(here, "..", "dimming.py"),
        "/opt/grok-station/dimming.py",
    ])


def read_station_env(path=None):
    """Display settings. The wake service does not load this file itself."""
    path = path or os.environ.get("GROK_STATION_ENV", "/etc/grok-station/env")
    vals = {}
    try:
        with open(path) as handle:
            for line in handle:
                if "=" not in line or line.startswith("#"):
                    continue
                key, _, value = line.strip().partition("=")
                if key.replace("_", "").isalnum() and key.isupper():
                    vals[key] = value.strip().strip('"')
    except OSError:
        pass
    return vals


def night_clock_quiet(spec, now):
    """Display night-clock hours. Missing spec is 22-6. Empty spec is off. now is local time."""
    if spec is None:
        spec = "22-6"
    spec = str(spec).strip()
    if not spec:
        return False
    try:
        start_s, end_s = spec.split("-")
        start, end = int(start_s), int(end_s)
    except (TypeError, ValueError):
        return False
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return False
    hour = now.tm_hour
    if start > end:
        return start <= hour or hour < end
    return start <= hour < end


def tone_context(env, now, lux=None):
    """(quiet, night_level, day_level).

    Brightness off: the night-clock hours. Any dim mode: that mode's dim window
    (the same choice as the night face). Levels are the day and night brightness.
    """
    env = env if isinstance(env, dict) else {}
    dim = dimming_mod()
    if dim is None:
        return night_clock_quiet(env.get("GROK_STATION_NIGHT", "22-6"), now), 20, 90
    try:
        cfg = dim.config_from_env(env)
        day, night = int(cfg["day"]), int(cfg["night"])
        if (cfg.get("mode") or "off") == "off":
            quiet = night_clock_quiet(env.get("GROK_STATION_NIGHT", "22-6"), now)
        else:
            quiet = dim.in_dim_period(cfg, now, lux=lux) is True
    except Exception:
        return night_clock_quiet(env.get("GROK_STATION_NIGHT", "22-6"), now), 20, 90
    return quiet, night, day


def render_pcm(env=None, now=None, lux=None, volume=1.0):
    """(pcm, sample_rate). Empty pcm when the gain is zero. Synthesized, not a file."""
    mod = sounds_mod()
    if mod is None:
        return b"", 24000
    if env is None:
        env = read_station_env()
    if now is None:
        now = time.localtime()
    quiet, night, day = tone_context(env, now, lux=lux)
    gain = mod.wait_tone_gain(volume, quiet=quiet, night_level=night, day_level=day)
    return mod.wait_tone_pcm(gain, wait_tone_style_saved()), mod.WAIT_TONE_SR


def tone_every():
    """Seconds between pulses: WAIT_TONE_EVERY_SOFT for the soft pulse, 2.5 s for the classic chime."""
    mod = sounds_mod()
    style = wait_tone_style_saved()
    if mod is None or not hasattr(mod, "wait_tone_every"):
        return WAIT_TONE_EVERY
    return mod.wait_tone_every(style)


class WaitTone:
    """Start and stop decisions for the thinking chime. Playback is a daemon thread started from main."""

    def __init__(self):
        self.phase = None
        self.speech = 0
        self.armed_at = None
        self.next_at = None
        self.playing = False
        self._proc = None
        self._cancel = False
        self._lock = threading.Lock()
        self._speaker = threading.Lock()  # held only while aplay is started or stopped
        self._wake = threading.Event()
        self._stop = False
        self._thread = None
        self._device = None

    def reset(self):
        self._stop_playback()
        with self._lock:
            self.phase = None
            self.speech = 0
            self.armed_at = None
            self.next_at = None
            self.playing = False
            self._cancel = False

    def note_phase(self, state, now=None):
        """The display's voice state. Only `thinking` arms the chime. Leaving it stops playback now."""
        now = time.monotonic() if now is None else now
        with self._lock:
            prev = self.phase
            self.phase = state if isinstance(state, str) else None
            if self.phase == "thinking":
                if prev != "thinking" and self.speech == 0:
                    self.armed_at = now
                    self.next_at = now + WAIT_TONE_DELAY
            else:
                self.armed_at = None
                self.next_at = None
            leave = self.phase != "thinking"
        if leave:
            self._stop_playback()
        self._wake.set()

    def begin_speech(self, now=None):
        """Call before any other aplay. Returns only after the chime process has exited."""
        with self._lock:
            self.speech += 1
            self.next_at = None
        self._stop_playback()
        self._wake.set()

    def end_speech(self, now=None):
        """Speech finished. If the ring is still thinking, the chime waits out the start delay again."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self.speech = max(0, self.speech - 1)
            if self.speech == 0 and self.phase == "thinking":
                self.armed_at = now
                self.next_at = now + WAIT_TONE_DELAY
        self._wake.set()

    def due(self, now, enabled=True):
        with self._lock:
            return self._due(now, enabled)

    def _due(self, now, enabled):
        if not enabled or self.phase != "thinking" or self.speech or self.playing:
            return False
        if self.next_at is None:
            return False
        return now >= self.next_at

    def claim(self, now, enabled=True):
        """Mark a chime started. False when it is no longer due. The next one is WAIT_TONE_EVERY later."""
        with self._lock:
            if not self._due(now, enabled):
                return False
            self.playing = True
            self._cancel = False
            self.next_at = now + tone_every()
            return True

    def _stop_playback(self):
        with self._speaker:
            with self._lock:
                self._cancel = True
                proc = self._proc
                self._proc = None
                self.playing = False
            if proc is not None and proc.poll() is None:
                try:
                    stdin = getattr(proc, "stdin", None)
                    if stdin is not None:
                        stdin.close()
                except OSError:
                    pass
                try:
                    proc.terminate()
                except OSError:
                    proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=0.2)
                except (OSError, subprocess.TimeoutExpired):
                    pass

    def start(self, device):
        """device() returns the playback device, or None. Idempotent."""
        self._device = device
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._loop, name="wait-tone", daemon=True)
            self._thread.start()

    def _loop(self):
        while not self._stop:
            self._wake.wait(0.05)
            self._wake.clear()
            if self._stop:
                return
            enabled = wait_tone_enabled()
            now = time.monotonic()
            if not enabled:
                self._stop_playback()
                continue
            if not self.due(now, enabled):
                continue
            dev = self._device() if self._device else None
            if not dev:
                continue
            try:
                pcm, rate = render_pcm()
            except Exception:
                continue
            if not pcm or not self.claim(time.monotonic(), wait_tone_enabled()):
                continue
            self._sound(dev, pcm, rate)

    def _sound(self, dev, pcm, rate):
        with self._speaker:
            with self._lock:
                if self._cancel or self.speech or self.phase != "thinking":
                    self.playing = False
                    return
            try:
                proc = subprocess.Popen(
                    ["aplay", "-q", "-D", dev, "-f", "S16_LE", "-r", str(int(rate)), "-c", "1", "-t", "raw"],
                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                with self._lock:
                    self.playing = False
                return
            with self._lock:
                if self._cancel or self.speech or self.phase != "thinking":
                    abort = proc
                    self.playing = False
                else:
                    abort = None
                    self._proc = proc
        if abort is not None:
            try:
                abort.terminate()
            except OSError:
                pass
            try:
                abort.wait(timeout=0.3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    abort.kill()
                except OSError:
                    pass
            return
        try:
            try:
                proc.stdin.write(pcm)
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    self.playing = False


TONE = WaitTone()


class speech:
    """Hold the speaker around one playback. The chime cannot start until the block exits."""

    def __enter__(self):
        TONE.begin_speech()
        return self

    def __exit__(self, *exc):
        TONE.end_speech()
        return False
