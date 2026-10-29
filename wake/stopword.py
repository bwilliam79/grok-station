"""On-device "stop" during wake-word training.

vosk: offline speech recognition so saying stop works during wake-word training; grammar limited to stop, never leaves the station.
The grammar is only "stop" and the unknown token. Audio is not logged, stored, or sent anywhere.
vosk is imported inside the factory, not when this module loads. If it is missing, or the model
cannot be loaded, the factory returns None and says so once. There is no cloud speech fallback.
"""
import json
import os
import threading

_DEFAULT_MODEL = "/opt/grok-station/wake/models/vosk-model-small-en-us-0.15"
_OFF = "stop word: off (on-device recogniser not installed); touch still works"
_warned = False
_warn_lock = threading.Lock()


def _off():
    """One non-sensitive line per process. Never includes a path, a transcript, or a token."""
    global _warned
    with _warn_lock:
        if _warned:
            return
        _warned = True
    print(_OFF, flush=True)


def recogniser(model_dir=None):
    """A small on-device recogniser, or None when vosk or its model is not on this station."""
    path = _DEFAULT_MODEL if model_dir is None else model_dir
    if model_dir is None:
        path = os.environ.get("WAKE_STOP_MODEL", _DEFAULT_MODEL)
    try:
        import vosk
    except Exception:
        _off()
        return None
    if not isinstance(path, str) or not path or not os.path.isdir(path):
        _off()
        return None
    try:
        vosk.SetLogLevel(-1)
        model = vosk.Model(path)
        rec = vosk.KaldiRecognizer(model, 16000, json.dumps(["stop", "[unk]"]))
    except Exception:
        _off()
        return None
    return _Recognizer(rec)


def _utterance(raw):
    try:
        doc = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return ""
    if not isinstance(doc, dict):
        return ""
    for key in ("text", "partial"):
        val = doc.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


class _Recognizer:
    """AcceptWaveform on 16 kHz int16 bytes. Result when the word is final, otherwise the partial."""

    def __init__(self, rec):
        self._rec = rec

    def feed(self, samples):
        data = samples.tobytes() if hasattr(samples, "tobytes") else bytes(samples)
        if self._rec.AcceptWaveform(data):
            raw = self._rec.Result()
        else:
            raw = self._rec.PartialResult()
        return _utterance(raw)

    def reset(self):
        self._rec.Reset()
