"""Station owner name check. Stdlib only.

One copy of the allowlist. The relay imports this file from its own folder, installed
root-owned beside safe_io.py. admin/gs_routine.py loads the same file. scripts/gs-update.sh
embeds a byte-identical copy and writes it next to gs_routine.py on the bot.
"""
import re

OWNER_NAME = re.compile(r"[A-Za-z][A-Za-z -]{0,59}")


def owner_ok(value):
    """Stripped owner when it matches OWNER_NAME, else None."""
    value = value.strip() if isinstance(value, str) else ""
    return value if OWNER_NAME.fullmatch(value) else None


def owner_name(value=None):
    """Safe owner for a template. Anything else is the placeholder."""
    return owner_ok(value) or "your owner"
