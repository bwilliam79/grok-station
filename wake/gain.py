"""Mic-gain defaults shared by the wake listener and the admin helper.

No numpy. The listener (wake/grok_wake.py) applies:

    WAKE_GAIN   env WAKE_GAIN, else default_gain(WAKE_DEVICE_NAME or "EMEET")
    VOICE_GAIN  env VOICE_GAIN, else the wake gain it actually applied

"2" is the EMEET speakerphone. Anything else (the quieter cards) is "4".
"""


def default_gain(device):
    """Software gain when WAKE_GAIN is unset. '2' if the name contains emeet, else '4'."""
    return "2" if "emeet" in device.lower() else "4"


def effective_gains(device, wake_gain=None, voice_gain=None):
    """Gains the listener would apply. Blank device matches its EMEET default.

    A blank stored gain is unset. Wake then uses default_gain(device). Voice uses
    the wake gain (the stored WAKE_GAIN when that is set, otherwise the device default).
    """
    dev = device.strip() if isinstance(device, str) and device.strip() else "EMEET"
    wake = wake_gain.strip() if isinstance(wake_gain, str) and wake_gain.strip() else default_gain(dev)
    voice = voice_gain.strip() if isinstance(voice_gain, str) and voice_gain.strip() else wake
    return {"WAKE_GAIN": wake, "VOICE_GAIN": voice}


def gain_by_device():
    """The two device classes, so the admin page can retarget a slider before save."""
    return {"emeet": default_gain("emeet"), "other": default_gain("other")}
