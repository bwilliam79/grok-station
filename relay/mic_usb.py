"""Speakerphone that is still on the USB bus but not configured.

Stdlib only. The display, the admin page, and the wake listener import this
one copy (installed at /opt/grok-station/relay/mic_usb.py). A missing ALSA
card plus USB 328f:0109 with bConfigurationValue other than 1 is the stuck
state left by a reset the device refused. Only a power cycle clears it.

Pass an explicit sysfs root in tests. The default is /sys. There is no
environment override: the check only reads.
"""
import os
import re
import stat

# Numeric USB ids, lowercase hex without a 0x prefix: the configured speakerphone.
EMEET_IDS = frozenset({("328f", "0109")})
POWER_CYCLE = "Mic needs a power cycle"
POWER_CYCLE_HELP = "Unplug the speakerphone or switch it off and on."


def _reg_text(path, limit=128):
    """Text of a regular file. A symlink, or anything else, is None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        data = os.read(fd, limit)
    finally:
        os.close(fd)
    return data.decode("utf-8", "replace")


def _ids(usb):
    vend = (_reg_text(os.path.join(usb, "idVendor")) or "").strip().lower()
    prod = (_reg_text(os.path.join(usb, "idProduct")) or "").strip().lower()
    return vend, prod


def _configured(usb):
    return (_reg_text(os.path.join(usb, "bConfigurationValue")) or "").strip() == "1"


def _card_for(sysfs, usb):
    """An ALSA card whose device link lands on this USB device, or None."""
    usb_real = os.path.realpath(usb)
    sound = os.path.join(sysfs, "class", "sound")
    try:
        names = os.listdir(sound)
    except OSError:
        return None
    for name in names:
        if not re.fullmatch(r"card[0-9]{1,2}", name):
            continue
        dev = os.path.join(sound, name, "device")
        if not os.path.lexists(dev):
            continue
        try:
            landed = os.path.realpath(dev)
        except OSError:
            continue
        if landed == usb_real or landed.startswith(usb_real + os.sep):
            return name
    return None


def unconfigured_speakerphone(sysfs=None):
    """The stuck speakerphone, or None.

    Match is the USB vendor:product. The ALSA card is already gone, and
    bConfigurationValue is not 1 (empty counts). A configured device, or any
    other USB id, is not this state.
    """
    root = "/sys" if not sysfs else sysfs
    bus = os.path.join(root, "bus", "usb", "devices")
    try:
        names = os.listdir(bus)
    except OSError:
        return None
    for name in names:
        if name.startswith("usb"):
            continue
        path = os.path.join(bus, name)
        try:
            if os.path.islink(path):
                path = os.path.realpath(path)
        except OSError:
            continue
        if not os.path.isdir(path):
            continue
        if _ids(path) not in EMEET_IDS:
            continue
        if _configured(path):
            continue
        if _card_for(root, path) is not None:
            continue
        return {"detail": POWER_CYCLE, "help": POWER_CYCLE_HELP}
    return None
