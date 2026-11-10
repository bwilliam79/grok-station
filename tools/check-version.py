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

0.13.29: it also fails when the home helper files (the ones scripts/install-home.sh fetches) changed but
HELPER_VERSION did not. tools/helper-releases.txt has one line per helper release, "X.Y.Z <sha256>", oldest
first; the sha256 covers those files. The last line must be HELPER_VERSION with the files' current digest,
and versions must go up. A new helper release adds a line; editing the digest on the same version fails.

The comparison matches tools/semver-order.sh and admin version_order: numeric
MAJOR.MINOR.PATCH, and a non-semver parent counts as older than a semver HEAD.
"""
import hashlib
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
        "admin/setup-params.txt",
        "admin/web/app.js",
        "docs/bots.md",
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


# Re-pin may change these three only on lines that are a pin literal. deploy-pins.js stays
# allowlisted by file name: its body is the pin object, not these prose lines.
_REPIN_LITERAL_FILES = frozenset({
    "admin/setup-params.txt",
    "admin/web/app.js",
    "docs/bots.md",
})
_REPIN_LINE = re.compile(
    r"(?:"
    r"T=\$\(mktemp\) && curl -fsS --proto '=https' --max-redirs 0 -o \"\$T\" "
    r"'https://raw\.githubusercontent\.com/bwilliam79/grok-station/[0-9a-f]{40}/scripts/install-home\.sh' "
    r"&& printf '%s  %s\\n' '[0-9a-f]{64}' \"\$T\" \| sha256sum -c - && sh \"\$T\" "
    r"--persona '<target>' --station '<station>' --pin-sha256 '<pin_sha256>' --station-pub '<station_pub>'; "
    r"rc=\$\?; rm -f \"\$T\"; \(exit \$rc\)"
    r"|\([0-9a-f]{40} and [0-9a-f]{64} are literal in this Station setup text for "
    r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}; the script checks HASH_SETUP/REPLY/COMMON/LOCK itself and stops on any problem\.\)"
    r"|(?:ROUTINE_COMMIT|UPDATE_COMMIT) [0-9a-f]{40}"
    r"|(?:HASH_ROUTINE_HOME|HASH_ROUTINE_CLOUD|HASH_UPDATE) [0-9a-f]{64}"
    r"|RELEASE [0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}"
    r")\Z"
)


def repin_nonliteral(repo, rev, files):
    """Paths whose re-pin diff has a line that is not a pin literal. Empty when every line matches."""
    bad = []
    for path in files:
        if path not in _REPIN_LITERAL_FILES:
            continue
        proc = subprocess.run(
            ["git", "-C", repo, "diff", "-U0", f"{rev}~1", rev, "--", path],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            bad.append(path)
            continue
        for line in proc.stdout.splitlines():
            if not line or line[:1] not in "+-" or line.startswith(("+++", "---")):
                continue
            if _REPIN_LINE.fullmatch(line[1:]) is None:
                bad.append(path)
                break
    return bad


def ritual_extra(repo, rev, kind):
    """Files in rev that are outside that ritual's allowlist. None when rev cannot be read.

    For a re-pin, admin/setup-params.txt, admin/web/app.js, and docs/bots.md are inside the
    allowlist only when every added or removed line is a pin literal.
    """
    files = changed_files(repo, rev)
    if files is None:
        return None
    extra = [path for path in files if path not in _RITUAL_FILES[kind]]
    if extra or kind != "re-pin deploy files":
        return extra
    return repin_nonliteral(repo, rev, files)


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


HELPER_RECORD = "tools/helper-releases.txt"
_FETCH_ONE = re.compile(r'^\s*fetch_one (\S+) "\$RAW/((?:scripts|admin|relay)/\S+)" ', re.M)
_HELPER_VERSION = re.compile(r'^HELPER_VERSION = "(\d+\.\d+\.\d+)"$', re.M)
_RECORD_LINE = re.compile(r"^(\d+\.\d+\.\d+) ([0-9a-f]{64})$")


def helper_files(install_home):
    """The repo paths install-home.sh fetches into the home helper folder, in its order."""
    return [path for _name, path in _FETCH_ONE.findall(install_home)]


def helper_digest(read, files):
    """sha256 over "<sha256 of file>  <path>" lines, one per helper file, in installer order."""
    lines = []
    for path in files:
        data = read(path)
        if data is None:
            return None
        lines.append("%s  %s\n" % (hashlib.sha256(data).hexdigest(), path))
    return hashlib.sha256("".join(lines).encode("ascii")).hexdigest()


def helper_problem(read):
    """None when the helper files match the last tools/helper-releases.txt line. Otherwise a one-line reason.

    read(path) returns the bytes of a repo file, or None when it is missing. Passes when the record or the
    installer is missing (a tree from before 0.13.29)."""
    record, installer, common = read(HELPER_RECORD), read("scripts/install-home.sh"), read("scripts/gs_seal_common.py")
    if record is None or installer is None or common is None:
        return None
    files = helper_files(installer.decode("utf-8", "replace"))
    if not files:
        return "scripts/install-home.sh fetches no helper files."
    m = _HELPER_VERSION.search(common.decode("utf-8", "replace"))
    if not m:
        return "scripts/gs_seal_common.py has no HELPER_VERSION."
    version = m.group(1)
    rows = []
    for line in record.decode("ascii", "replace").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        row = _RECORD_LINE.fullmatch(line)
        if not row:
            return f"{HELPER_RECORD}: bad line {line[:40]!r}."
        rows.append((semver_tuple(row.group(1)), row.group(1), row.group(2)))
    if not rows:
        return f"{HELPER_RECORD} has no helper release."
    for (a, va, _), (b, vb, _) in zip(rows, rows[1:]):
        if not b > a:
            return f"{HELPER_RECORD}: {vb} after {va}; each helper release goes up."
    digest = helper_digest(read, files)
    if digest is None:
        return "a helper file named by scripts/install-home.sh is missing."
    _, last_version, last_digest = rows[-1]
    if version != last_version:
        return (f"HELPER_VERSION {version} has no line in {HELPER_RECORD} (last is {last_version}); "
                f"add \"{version} {digest}\".")
    if digest != last_digest:
        return (f"helper files changed without a HELPER_VERSION bump (still {version}); bump HELPER_VERSION "
                f"and add a line to {HELPER_RECORD}.")
    return None


def _git_reader(repo, rev="HEAD"):
    def read(path):
        proc = subprocess.run(["git", "-C", repo, "show", f"{rev}:{path}"], capture_output=True)
        return proc.stdout if proc.returncode == 0 else None
    return read


def main(argv):
    repo = argv[1] if len(argv) > 1 else os.getcwd()
    msg = problem(repo) or reused_release(repo) or helper_problem(_git_reader(repo))
    if msg:
        print(msg, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
