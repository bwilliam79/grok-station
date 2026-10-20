#!/usr/bin/env python3
"""Idempotent kernel command line for grok-station (cmdline.txt). Prints "reboot" if anything changed.

Adds the quiet-boot options, including vt.global_cursor_default=0 (no blinking console cursor), only when missing
(an option already set to another value is kept). The first change backs the file up once to
cmdline.txt.grok-station.bak. config.txt is not touched here.
usage: boot-config.py [--boot DIR] [--dry-run]
"""
import os, re, shutil, sys

CMDLINE_OPTS = ["quiet", "splash", "loglevel=3", "vt.global_cursor_default=0", "logo.nologo", "consoleblank=0",
                "plymouth.ignore-serial-consoles"]


def cmdline_txt(text):
    line = text.strip().splitlines()[0] if text.strip() else ""
    words = line.split()
    for o in CMDLINE_OPTS:
        key = o.split("=", 1)[0]
        if not any(w == o or w.split("=", 1)[0] == key for w in words):
            words.append(o)
    return " ".join(words) + "\n"


def apply(path, fn, dry):
    try:
        old = open(path).read()
    except OSError:
        return False
    new = fn(old)
    if new == old:
        return False
    if not dry:
        bak = path + ".grok-station.bak"
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
        tmp = path + ".grok-station.tmp"
        with open(tmp, "w") as f:
            f.write(new)
        os.replace(tmp, path)
    return True


def main(argv):
    boot = "/boot/firmware"
    if "--boot" in argv:
        boot = argv[argv.index("--boot") + 1]
    dry = "--dry-run" in argv
    changed = apply(os.path.join(boot, "cmdline.txt"), cmdline_txt, dry)
    print("reboot" if changed else "unchanged")


if __name__ == "__main__":
    main(sys.argv[1:])
