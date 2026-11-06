#!/usr/bin/env python3
"""Fail unless HEAD's VERSION is a semantic version strictly greater than HEAD~1's.

Pass when HEAD~1 has no VERSION file (including the first commit that adds one,
and a repository with only that commit). Fail when HEAD has no VERSION file,
when it is not MAJOR.MINOR.PATCH, or when it is not newer.

A release commit may keep the same VERSION only when its subject is exactly
"release: update manifest for X" or "release: re-pin deploy files for X" (X is
that VERSION, nothing after it) and every file in the commit is part of that
ritual. A manifest commit may change VERSION, README.md, docs/versioning.md,
scripts/update-manifest.txt, and admin/bot-releases.txt. A re-pin commit may
change admin/web/deploy-pins.js. Anything else, a suffix on the subject, or a
different X still fails. reused_release treats only that exact ritual commit as
the release boundary.

The comparison matches tools/semver-order.sh and admin version_order: numeric
MAJOR.MINOR.PATCH, and a non-semver parent counts as older than a semver HEAD.
"""
import os
import re
import subprocess
import sys

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# Full subject only. A suffix such as 0.13.27-fix must not match.
_RELEASE_SUBJECT = re.compile(r"^release: (update manifest|re-pin deploy files) for (\d+\.\d+\.\d+)$")
_RITUAL_FILES = {
    "update manifest": frozenset({
        "VERSION",
        "README.md",
        "docs/versioning.md",
        "scripts/update-manifest.txt",
        "admin/bot-releases.txt",
    }),
    "re-pin deploy files": frozenset({
        "admin/web/deploy-pins.js",
    }),
}


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


def changed_files(repo, rev):
    """Paths changed by rev, or None when git cannot show it."""
    proc = subprocess.run(
        ["git", "-C", repo, "show", "--name-only", "--format=", rev],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def ritual_commit(subject):
    """(kind, version) when subject is exactly one release ritual line, else None."""
    match = _RELEASE_SUBJECT.fullmatch(subject.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def ritual_extra(repo, rev, kind):
    """Files in rev that are outside that ritual's allowlist. None when rev cannot be read."""
    files = changed_files(repo, rev)
    if files is None:
        return None
    return [path for path in files if path not in _RITUAL_FILES[kind]]


def _same_version(prev, head, extra=None):
    msg = f"VERSION {head.strip()} is not newer than {prev.strip()}."
    if extra:
        msg = msg[:-1] + f"; {extra[0]} is not a release file."
    return msg


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
        # A same-version exception is only an exact ritual commit for this VERSION
        # whose files stay inside that ritual's allowlist.
        subject = subprocess.run(
            ["git", "-C", repo, "log", "-1", "--format=%s"],
            capture_output=True, text=True,
        )
        ritual = ritual_commit(subject.stdout) if subject.returncode == 0 else None
        if ritual and ritual[1] == head.strip() and version_order(prev, head) == "same":
            extra = ritual_extra(repo, "HEAD", ritual[0])
            if extra == []:
                return None
            if extra:
                return _same_version(prev, head, extra)
        return _same_version(prev, head)
    return None


_NOTES_ONLY = ("docs/", "tests/", "README.md")


def reused_release(repo):
    """None when VERSION has not already shipped with later code. Otherwise a one-line reason.

    A release ends at the newest commit whose subject is exactly "release: update manifest for X" or
    "release: re-pin deploy files for X" (X is VERSION, nothing after it) and whose files are only that
    ritual's allowlist. A suffix, a different X, or an exact subject that also changes a product file is not
    that boundary: it is later code, and an extra file fails the check. A commit after a real boundary that
    changes anything outside docs/, tests/ or README.md ships new code, so it needs a new VERSION; reusing X
    means a station already on X is never offered it (0.12.6 to 0.12.7). Also fails unless docs/versioning.md
    has exactly one entry for VERSION. Passes when VERSION, the notes, or git history are unavailable.
    """
    try:
        version = open(os.path.join(repo, "VERSION")).read().strip()
        notes = open(os.path.join(repo, "docs", "versioning.md")).read()
    except OSError:
        return None  # not a full checkout (no release notes): nothing to compare
    count = len(re.findall(r"^## " + re.escape(version) + r"\s*$", notes, re.M))
    if count != 1:
        return f"docs/versioning.md has {count} entries for {version}, expected 1."
    log = subprocess.run(["git", "-C", repo, "log", "--format=%H %s", "-400"], capture_output=True, text=True)
    if log.returncode != 0:
        return None
    after = []
    for line in log.stdout.splitlines():
        sha, _, subject = line.partition(" ")
        ritual = ritual_commit(subject)
        if ritual and ritual[1] == version:
            extra = ritual_extra(repo, sha, ritual[0])
            if extra == []:
                break  # exact ritual for this VERSION: the release boundary
            if extra:
                return (f"VERSION {version} was already released; commit {sha[:7]} "
                        f"changes {extra[0]} without a new VERSION.")
        after.append(sha)  # suffix, wrong X, or unreadable: later code, not a boundary
    else:
        return None
    for sha in after:
        files = changed_files(repo, sha) or []
        code = [f for f in files if not f.startswith(_NOTES_ONLY)]
        if code:
            return f"VERSION {version} was already released; commit {sha[:7]} changes {code[0]} without a new VERSION."
    return None


def main(argv):
    repo = argv[1] if len(argv) > 1 else os.getcwd()
    msg = problem(repo) or reused_release(repo)
    if msg:
        print(msg, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
