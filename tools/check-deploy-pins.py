#!/usr/bin/env python3
"""Release check for admin/web/deploy-pins.js.

The pins name the parent of the release commit (the release commit cannot name
itself). That is safe only when every pinned file is byte-identical at HEAD.

  1. each pin == sha256 of `git show <pinned commit>:<path>` (hard failure)
  2. `git show HEAD:<path>` has the same bytes (a pending re-pin note when it does not)
  3. every path in FILES is present in the pin file (a pending note when a new file is not pinned yet)
  4. with --remote: each file downloaded from GitHub at the pinned commit matches (hard failure)

A release that changes a pinned file cannot update the pins in the same commit.
0.11.0 is that case: deploy-pins.js stays on the previous commit, and this
check prints notes instead of failing. The next release re-pins with
tools/gen-deploy-pins.py.

Usage: tools/check-deploy-pins.py [--remote] [--rev REV]
Exit 0 when there is no hard failure. Pending notes still exit 0.
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PINS = os.path.join(ROOT, "admin", "web", "deploy-pins.js")
RAW = "https://raw.githubusercontent.com/bwilliam79/grok-station/"
# Same list as tools/gen-deploy-pins.py FILES. A path missing from the pin
# file is a pending note, not a hard failure, until the next re-pin.
FILES = (
    "tools/grok-station-reply",
    "scripts/gs-seal-setup",
    "scripts/gs-seal-reply",
    "scripts/gs_seal_common.py",
    "scripts/vendor-lock.txt",
    "scripts/install-home.sh",
    "scripts/gs-update.sh",
)


def load_pins(path=PINS):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r'commit:\s*"([0-9a-f]{40})"', text)
    if not m:
        raise SystemExit("deploy-pins.js: no 40-character commit")
    files = dict(re.findall(r'"([^"]+)":\s*"([0-9a-f]{64})"', text))
    if not files:
        raise SystemExit("deploy-pins.js: no file hashes")
    return m.group(1), files


def blob(rev, path):
    proc = subprocess.run(["git", "-C", ROOT, "show", f"{rev}:{path}"], capture_output=True)
    return proc.stdout if proc.returncode == 0 else None


def fetch(url):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(url, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def pinned_commits(pins_path=PINS, rev="HEAD"):
    """Every commit a bot or the station fetches by id: deploy-pins commit and update_commit, the update
    manifest's update line, and install-home.sh's COMMIT (at the deploy-pins commit and at rev)."""
    text = open(pins_path, encoding="utf-8").read()
    out = {}
    m = re.search(r'^  commit: "([0-9a-f]{40})",$', text, re.M)
    if m:
        out["deploy-pins commit"] = m.group(1)
    m = re.search(r'^  update_commit: "([0-9a-f]{40})",$', text, re.M)
    if m:
        out["deploy-pins update_commit"] = m.group(1)
        man = blob(m.group(1), "scripts/update-manifest.txt") or b""
        u = re.search(rb"^update ([0-9a-f]{40}) ", man, re.M)
        if u:
            out["manifest update"] = u.group(1).decode()
    for where, ref in (("deploy-pins commit", out.get("deploy-pins commit")), (rev, rev)):
        ih = blob(ref, "scripts/install-home.sh") if ref else None
        c = re.search(rb"^COMMIT=([0-9a-f]{40})$", ih or b"", re.M)
        if c:
            out["install-home COMMIT at " + where] = c.group(1).decode()
    return out


TEMPLATES = ("admin/routine-home.txt", "admin/routine-cloud.txt")


def manifest_templates(pins_path=PINS):
    """Problems unless the pinned update manifest lists both templates (0.11.9: gs-update.sh has no fallback)."""
    text = open(pins_path, encoding="utf-8").read()
    m = re.search(r'^  update_commit: "([0-9a-f]{40})",$', text, re.M)
    man = (blob(m.group(1), "scripts/update-manifest.txt") or b"").decode("ascii", "replace") if m else ""
    return [f"update manifest lists no {t}" for t in TEMPLATES
            if not re.search(r"^file [0-9a-f]{64} " + re.escape(t) + "$", man, re.M)]


def ancestry(main_ref="origin/main", pins_path=PINS, rev="HEAD"):
    """Problems for any pinned commit that is not an ancestor of main_ref (git merge-base --is-ancestor)."""
    problems = []
    for name, sha in sorted(pinned_commits(pins_path, rev).items()):
        r = subprocess.run(["git", "-C", ROOT, "merge-base", "--is-ancestor", sha, main_ref], capture_output=True)
        if r.returncode != 0:
            problems.append(f"{name} {sha[:12]} is not on {main_ref}")
    return problems


def check(remote=False, rev="HEAD", pins_path=PINS):
    """Return (commit, files, problems, notes).

    problems are hard failures (a pin does not match the pinned commit, or
    GitHub does not serve those bytes). notes are pending re-pin reminders:
    HEAD differs, or a file in FILES is not in the pin file yet.
    """
    commit, files = load_pins(pins_path)
    problems = []
    notes = []
    for path in FILES:
        if path not in files:
            notes.append(f"{path}: not pinned yet; re-pin with tools/gen-deploy-pins.py")
    for path, want in sorted(files.items()):
        old = blob(commit, path)
        if old is None:
            problems.append(f"{path}: not in pinned commit {commit[:12]}")
            continue
        if hashlib.sha256(old).hexdigest() != want:
            problems.append(f"{path}: pin does not match git show {commit[:12]}")
        cur = blob(rev, path)
        if cur is None:
            notes.append(f"{path}: missing at {rev}; re-pin with tools/gen-deploy-pins.py")
        elif cur != old:
            notes.append(f"{path}: differs at {rev}; re-pin with tools/gen-deploy-pins.py")
        if remote:
            status, data = fetch(RAW + commit + "/" + path)
            if status != 200:
                problems.append(f"{path}: GitHub returned HTTP {status}")
            elif hashlib.sha256(data).hexdigest() != want:
                problems.append(f"{path}: GitHub bytes do not match the pin")
    problems += manifest_templates(pins_path)
    return commit, files, problems, notes


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", action="store_true")
    ap.add_argument("--rev", default="HEAD")
    ap.add_argument("--main-ref", default="origin/main", help="ref every pinned commit must be on (with --remote)")
    args = ap.parse_args(argv)
    commit, files, problems, notes = check(args.remote, args.rev)
    if args.remote:
        subprocess.run(["git", "-C", ROOT, "fetch", "-q", "origin", "main"], capture_output=True)
        anc = ancestry(args.main_ref, rev=args.rev)
        problems += anc
        if not anc:
            print(f"ok: {len(pinned_commits(rev=args.rev))} pinned commits are on {args.main_ref}")
    for note in notes:
        print("NOTE " + note)
    for p in problems:
        print("FAIL " + p)
    if problems:
        return 1
    where = "git and GitHub" if args.remote else "git"
    print(f"ok: {len(files)} pinned files at {commit} match ({where})")
    if notes:
        print(f"pending: {len(notes)} re-pin note(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
