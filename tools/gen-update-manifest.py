#!/usr/bin/env python3
"""Write scripts/update-manifest.txt for this release (0.11.8+).

    python3 tools/gen-update-manifest.py <commit that holds scripts/gs-update.sh>

The manifest lists the release, the pinned update script (commit + sha256, read from git at that commit) and the
sha256 of each file a bot's update fetches, read from git HEAD. Commit it; the station sends that commit id.
"""
import hashlib, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ("admin/routine-home.txt", "admin/routine-cloud.txt", "scripts/install-home.sh", "tools/grok-station-reply")


def blob(ref, path):
    return subprocess.run(["git", "-C", ROOT, "show", "%s:%s" % (ref, path)], check=True, capture_output=True).stdout


def build(update_commit, ref="HEAD"):
    rel = open(os.path.join(ROOT, "VERSION")).read().strip()
    common = blob(ref, "scripts/gs_seal_common.py").decode()
    helper = re.search(r'^HELPER_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', common, re.M).group(1)
    out = ["release " + rel, "helper " + helper, "update %s %s" % (update_commit, hashlib.sha256(blob(update_commit, "scripts/gs-update.sh")).hexdigest())]
    out += ["file %s %s" % (hashlib.sha256(blob(ref, p)).hexdigest(), p) for p in FILES]
    return "\n".join(out) + "\n"


def main(argv):
    if len(argv) != 2 or not re.fullmatch(r"[0-9a-f]{40}", argv[1]):
        sys.stderr.write(__doc__)
        return 2
    with open(os.path.join(ROOT, "scripts", "update-manifest.txt"), "w", newline="\n") as fh:
        fh.write(build(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
