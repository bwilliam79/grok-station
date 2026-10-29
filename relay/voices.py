"""Map a voice a bot reports onto an xAI text-to-speech voice id.

Grok Bot stores the same ids xAI speaks. ALIASES is only for a future rename.
"""
import re

# Built-in xAI TTS voice ids.
# Source: https://docs.x.ai/developers/model-capabilities/audio/text-to-speech
# ("Voices" table, page last updated October 15, 2026; checked 2026-10-28).
XAI_VOICES = frozenset({
    "carina", "zagan", "helix", "orion", "luna", "iris", "altair", "zenith",
    "perseus", "helios", "lux", "kepler", "rigel", "cosmo", "celeste", "ursa",
    "sirius", "lumen", "castor", "naksh", "atlas", "aurora", "liora",
    "ara", "eve", "leo", "rex", "sal",
})

# Former Grok Bot names that are not the current xAI id. Empty until a rename ships.
ALIASES = {}

_VOICE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def map_reported_voice(raw, offered=None):
    """Lowercase xAI id, or None.

    Strip and casefold, then require the voice-id shape and membership in XAI_VOICES.
    When offered is a non-empty collection, the id must be in that list too (compared
    case-insensitively). None, an empty collection, or a string is not a live list:
    a string is not walked character by character.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip().casefold()
    if not _VOICE_RE.fullmatch(s):
        return None
    s = ALIASES.get(s, s)
    if not isinstance(s, str) or s not in XAI_VOICES:
        return None
    if not offered or isinstance(offered, (str, bytes)):
        return s
    try:
        folded = {item.strip().casefold() for item in offered if isinstance(item, str)}
    except TypeError:
        return s
    if s not in folded:
        return None
    return s
