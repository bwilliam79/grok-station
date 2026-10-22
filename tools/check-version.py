#!/usr/bin/env python3
"""Fail unless HEAD's VERSION is a semantic version strictly greater than HEAD~1's.

Pass when HEAD~1 has no VERSION file (including the first commit that adds one,
and a repository with only that commit). Fail when HEAD has no VERSION file,
when it is not MAJOR.MINOR.PATCH, or when it is not newer.

The comparison matches tools/semver-order.sh and admin version_order: numeric
MAJOR.MINOR.PATCH, and a non-semver parent counts as older than a semver HEAD.
"""
import os
import re
import subprocess
import sys

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def semver_tuple(value):
    if not isinstance(value, str):
        return None
    match = _SEMVER.fullmatch(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def version_order(installed, available):
    """newer, same, older, or unknown.

    unknown: available is not MAJOR.MINOR.PATCH (never treat it as an upgrade).
    newer: available is semver and installed is not (a short commit id, or dev).
    """
    avail = semver_tuple(available)
    if avail is None:
        return "unknown"
    inst = semver_tuple(installed)
    if inst is None:
        return "newer"
    if avail > inst:
        return "newer"
    if avail < inst:
        return "older"
    return "same"


def git_show(repo, rev):
    """File text, or None when that revision has no VERSION file."""
    proc = subprocess.run(
        ["git", "-C", repo, "show", f"{rev}:VERSION"],
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def problem(repo):
    """None when this repo may ship. Otherwise a one-line reason."""
    head = git_show(repo, "HEAD")
    if head is None:
        return "HEAD has no VERSION file."
    if semver_tuple(head) is None:
        shown = " ".join(head.split())[:40] or "empty"
        return f"HEAD VERSION {shown} is not MAJOR.MINOR.PATCH."
    parent = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet", "HEAD~1"],
        capture_output=True,
    )
    if parent.returncode != 0:
        return None
    prev = git_show(repo, "HEAD~1")
    if prev is None:
        return None
    if version_order(prev, head) != "newer":
        return f"VERSION {head.strip()} is not newer than {prev.strip()}."
    return None


def main(argv):
    repo = argv[1] if len(argv) > 1 else os.getcwd()
    msg = problem(repo)
    if msg:
        print(msg, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
