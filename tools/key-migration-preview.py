#!/usr/bin/env python3
"""Preview the 0.13.37 key-location migration on a copy of a station's state. Writes nothing to the station.

    sudo python3 tools/key-migration-preview.py [/var/lib/grok-station]

It copies only the files the migration reads (COPIED below: public verify keys, the persona overlay, helper and
update statuses, pairing stamps, the persona registry) into a private 0700 temp directory, imports this tree's
relay against the copy, and prints one line per pinned key: slug, verdict (home, home-evidence, cloud,
no-evidence), short fingerprint and the home evidence found. "cloud" and "no-evidence" keys would be marked
"needs Replace key" when 0.13.37 first starts. Webhooks, minted tokens, secrets and private keys are never copied.
The copy is removed on exit and on SIGINT, SIGTERM, SIGHUP or SIGQUIT, and lives on /dev/shm (memory) when that is
available. No key material is printed. If the overlay or bot_keys.json can't be read or parsed, or bot_keys.json is
missing on a station that has bots, it prints "could not decide" and exits 1 instead of an empty would_mark.
"""
import json, os, shutil, signal, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# GS1337-04: an allowlist, never a glob. These are every file run_key_migration(preview=True) reads.
COPIED = (
    ("bots", "bot_keys.json"),          # pinned public verify keys
    ("bots", "personas-overlay.json"),  # the old key_mode, added and hidden bots
    ("bots", "helpers.json"),           # helper versions (signed and X-GS-Helper)
    ("bots", "updates.json"),           # signed update statuses
    ("bots", "paired.json"),            # pairing stamps
    ("personas", "personas.json"),      # the persona registry
)


def copy_state(src, dst):
    """Copy the allowlisted regular files from src into dst. Returns the relative paths copied."""
    done = []
    for sub, name in COPIED:
        f = os.path.join(src, sub, name)
        if os.path.isfile(f) and not os.path.islink(f):
            os.makedirs(os.path.join(dst, sub), mode=0o700, exist_ok=True)
            shutil.copyfile(f, os.path.join(dst, sub, name))
            os.chmod(os.path.join(dst, sub, name), 0o600)
            done.append(sub + "/" + name)
    return done


def make_tmp():
    base = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None
    tmp = tempfile.mkdtemp(prefix="gs-keymig.", dir=base)
    os.chmod(tmp, 0o700)
    return tmp


def install_cleanup(tmp):
    """Remove tmp on SIGINT, SIGTERM, SIGHUP and SIGQUIT too (a SIGKILL or power loss can't be caught; /dev/shm
    does not survive a reboot)."""
    def gone(signum, _frame):
        shutil.rmtree(tmp, ignore_errors=True)
        os._exit(128 + signum)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(sig, gone)


def main(argv):
    src = argv[1] if len(argv) > 1 else "/var/lib/grok-station"
    tmp = make_tmp()
    install_cleanup(tmp)
    try:
        copy_state(src, tmp)
        os.environ["GROK_STATE_DIR"] = tmp
        os.environ["GROK_SECRETS_DIR"] = os.path.join(tmp, "secrets")  # never the station's secrets
        for k in ("WEBHOOKS_FILE", "UPDATES_FILE", "PAIRED_FILE", "HELPER_FILE", "PERSONAS_FILE"):
            os.environ.pop(k, None)
        sys.path.insert(0, os.path.join(ROOT, "relay"))
        import relay  # noqa: E402
        got = relay.run_key_migration(preview=True)
        if got is None:
            # GS1337-06: never an empty would_mark for a station whose keys file can't be judged.
            print("could not decide: bots/personas-overlay.json or bots/bot_keys.json is unreadable or damaged, "
                  "or bot_keys.json is missing on a station that has bots. 0.13.37 would distrust every key "
                  "until it can read them; fix the file before installing.")
            return 1
        for slug, v in sorted(got.items()):
            print("%-24s %-14s %s evidence=%s" % (slug, v["verdict"], v["fp"], ",".join(v["evidence"]) or "-"))
        print(json.dumps({"would_mark": sorted(s for s, v in got.items() if v["verdict"] in relay.KEY_MOVE_WHY)}))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
