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

echo "## chrome render"
chrome_bin=""
if command -v google-chrome-stable >/dev/null 2>&1; then chrome_bin=$(command -v google-chrome-stable)
elif command -v chromium >/dev/null 2>&1; then chrome_bin=$(command -v chromium)
elif command -v chromium-browser >/dev/null 2>&1; then chrome_bin=$(command -v chromium-browser)
elif command -v google-chrome >/dev/null 2>&1; then chrome_bin=$(command -v google-chrome)
fi
if [ -n "$chrome_bin" ]; then
  echo "chrome: $chrome_bin"
  (cd tests && GS_REQUIRE_CHROME=1 python3 -m unittest -v \
    test_gs1326_bots_align \
    test_gs1336_bots_repaint \
    test_gs1337_accept_and_replace \
    test_gs1337_bots_menu \
    test_gs1337_connected_after_test \
    test_gs1337_menu_gap \
    test_gs1337_repin_fingerprint \
    test_gs138_p1_pendpop_scrim \
    test_gs138_p2_pendpop_focus_return \
    test_gs138_p3_pendpop_trap \
    test_gs138_p4_pop_heading_focus \
    test_gs138_p6_sheet_meets_tabbar \
    test_gs138_p9_sheet_button_focus_ring \
    test_gs139_avatar_csp \
    test_gs139_recheck \
    test_gs139_voice_tip \
    test_gs1340_updates_align \
    test_gs1340_chrome_timeout) >"$out" 2>&1; rc=$?
  grep -v 'ResourceWarning\|tracemalloc' "$out"
  if [ "$rc" -ne 0 ]; then echo "FAIL: chrome render"; fail=1
  else echo "ok: chrome render"; fi
else
  echo "chrome not installed; running the timeout stand-in"
  (cd tests && python3 -m unittest -v test_gs1340_chrome_timeout) >"$out" 2>&1; rc=$?
  grep -v 'ResourceWarning\|tracemalloc' "$out"
  if [ "$rc" -ne 0 ]; then echo "FAIL: chrome timeout"; fail=1
  else echo "ok: chrome timeout (chrome not installed)"; fi
fi

echo "## installed tree and lifecycle scenarios"
(cd tests && GS_RELEASE_CHECK=1 python3 -m unittest -v test_gs1334_admin_install test_lifecycle_scenarios \
  test_gs1335_main_exact_release test_gs1335_release_hook) >"$out" 2>&1; rc=$?
grep -v 'ResourceWarning\|tracemalloc' "$out"
if [ "$rc" -ne 0 ] || grep -q '^OK (.*skipped' "$out"; then echo "FAIL: scenarios (failed or skipped)"; fail=1
else echo "ok: scenarios"; fi

if [ "$fail" -ne 0 ]; then echo "release-check: FAIL"; exit 1; fi
echo "release-check: PASS"
