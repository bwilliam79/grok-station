#!/usr/bin/env python3
"""Release check for admin/web/deploy-pins.js.

The pins name the parent of the release commit (the release commit cannot name
itself). This is safe only if every pinned file is byte-identical at HEAD, so:

  1. each pin == sha256 of `git show <pinned commit>:<path>`
  2. `git show HEAD:<path>` has the same bytes (the release does not change them)
  3. with --remote: each file downloaded from GitHub at the pinned commit matches

Usage: tools/check-deploy-pins.py [--remote] [--rev REV]
Exit 0 when all checks pass, 1 otherwise.
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


def check(remote=False, rev="HEAD", pins_path=PINS):
    commit, files = load_pins(pins_path)
    problems = []
    for path, want in sorted(files.items()):
        old = blob(commit, path)
        if old is None:
            problems.append(f"{path}: not in pinned commit {commit[:12]}")
            continue
        if hashlib.sha256(old).hexdigest() != want:
            problems.append(f"{path}: pin does not match git show {commit[:12]}")
        cur = blob(rev, path)
        if cur != old:
            problems.append(f"{path}: differs at {rev}; re-pin with tools/gen-deploy-pins.py")
        if remote:
            status, data = fetch(RAW + commit + "/" + path)
            if status != 200:
                problems.append(f"{path}: GitHub returned HTTP {status}")
            elif hashlib.sha256(data).hexdigest() != want:
                problems.append(f"{path}: GitHub bytes do not match the pin")
    return commit, files, problems


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", action="store_true")
    ap.add_argument("--rev", default="HEAD")
    args = ap.parse_args(argv)
    commit, files, problems = check(args.remote, args.rev)
    for p in problems:
        print("FAIL " + p)
    if problems:
        return 1
    where = "git and GitHub" if args.remote else "git"
    print(f"ok: {len(files)} pinned files at {commit} match ({where})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
