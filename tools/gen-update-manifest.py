#!/usr/bin/env python3
"""Write scripts/update-manifest.txt for this release (0.11.8+).

    python3 tools/gen-update-manifest.py <commit that holds scripts/gs-update.sh>

The manifest lists the release, the pinned update script (commit + sha256, read from git at that commit) and the
sha256 of each file a bot's update fetches, read from git HEAD. Commit it; the station sends that commit id.

0.12.10: it also writes admin/bot-releases.txt (station side only; bots never read it), one line per published
manifest in git history plus this one: release, update commit, gs-update.sh sha256, and the content fingerprint
(admin/bot_content.py) of the home and cloud bot. The manifest format itself is unchanged, because the update
script already on every bot refuses a manifest line it doesn't know.
"""
import hashlib, os, re, subprocess, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin"))
import bot_content  # noqa: E402

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


def _parse(text):
    rel = re.search(r"^release ([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})$", text, re.M)
    upd = re.search(r"^update ([0-9a-f]{40}) ([0-9a-f]{64})$", text, re.M)
    files = set(re.findall(r"^file [0-9a-f]{64} (\S+)$", text, re.M))
    return (rel.group(1), upd.group(1), upd.group(2), files) if rel and upd else None


def fp_line(rel, upd_commit, upd_sha, ref):
    """One bot-releases.txt line. Templates and helpers from ref (the manifest's commit), the script from upd_commit."""
    script = blob(upd_commit, "scripts/gs-update.sh")
    if hashlib.sha256(script).hexdigest() != upd_sha:
        raise SystemExit("gs-update.sh at %s does not match the manifest" % upd_commit)
    fps = [bot_content.content_fp(blob(ref, "admin/routine-%s.txt" % m), script, blob(ref, bot_content.HELPER[m]))
           for m in ("home", "cloud")]
    return "%s %s %s %s %s" % (rel, upd_commit, upd_sha, fps[0], fps[1])


def releases(manifest_text, ref="HEAD"):
    """bot-releases.txt: every manifest committed on this branch that lists both templates, then this one."""
    log = subprocess.run(["git", "-C", ROOT, "log", "--format=%H", "--reverse", ref, "--", "scripts/update-manifest.txt"],
                         check=True, capture_output=True, text=True).stdout.split()
    out, seen = [], set()
    for c in log + [None]:
        try:
            got = _parse(blob(c, "scripts/update-manifest.txt").decode("ascii") if c else manifest_text)
        except (subprocess.CalledProcessError, UnicodeDecodeError):
            continue
        if not got or not {"admin/routine-home.txt", "admin/routine-cloud.txt"} <= got[3]:
            continue  # before 0.11.9 a manifest listed no templates
        try:
            line = fp_line(got[0], got[1], got[2], c or ref)
        except (subprocess.CalledProcessError, SystemExit):
            continue  # a file missing at that commit (rewritten history): that release just isn't listed
        if line not in seen:
            seen.add(line); out.append(line)
    return "\n".join(out) + "\n"


def main(argv):
    if len(argv) != 2 or not re.fullmatch(r"[0-9a-f]{40}", argv[1]):
        sys.stderr.write(__doc__)
        return 2
    with open(os.path.join(ROOT, "scripts", "update-manifest.txt"), "w", newline="\n") as fh:
        text = build(argv[1])
        fh.write(text)
    with open(os.path.join(ROOT, "admin", "bot-releases.txt"), "w", newline="\n") as fh:
        fh.write(releases(text))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
