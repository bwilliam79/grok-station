#!/bin/sh
# Pre-push gate for a grok-station release (docs/bot-lifecycle.md). Run it on the exact commit you will push:
#   sh tools/release-check.sh
# It runs the release ritual checks, the installed-tree import checks and the bot Update and Set up lifecycle
# scenarios against a real tools/install-files.sh install into a temporary root. Any failure, a pending re-pin,
# uncommitted changes, or a scenario that could not run (no `unshare -rm`) exits nonzero.
# Do not push unless the last line is "release-check: PASS".
set -u
cd "$(dirname "$0")/.." || exit 2
fail=0
out=$(mktemp)
trap 'rm -f "$out"' EXIT
echo "# release-check $(git rev-parse HEAD 2>/dev/null || echo no-git) VERSION $(cat VERSION) $(date)"
if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  echo "FAIL: uncommitted changes; run this on the exact commit you will push"; fail=1
fi

echo "## check-version"
if python3 tools/check-version.py; then echo "ok: check-version"; else echo "FAIL: check-version"; fail=1; fi

echo "## check-deploy-pins"
python3 tools/check-deploy-pins.py >"$out" 2>&1; rc=$?
cat "$out"
if [ "$rc" -ne 0 ] || grep -q '^pending:' "$out"; then echo "FAIL: check-deploy-pins (a release tip has no pending re-pin)"; fail=1
else echo "ok: check-deploy-pins"; fi

echo "## installed tree and lifecycle scenarios"
(cd tests && GS_RELEASE_CHECK=1 python3 -m unittest -v test_gs1334_admin_install test_lifecycle_scenarios \
  test_gs1335_main_exact_release test_gs1335_release_hook) >"$out" 2>&1; rc=$?
grep -v 'ResourceWarning\|tracemalloc' "$out"
if [ "$rc" -ne 0 ] || grep -q '^OK (.*skipped' "$out"; then echo "FAIL: scenarios (failed or skipped)"; fail=1
else echo "ok: scenarios"; fi

if [ "$fail" -ne 0 ]; then echo "release-check: FAIL"; exit 1; fi
echo "release-check: PASS"
