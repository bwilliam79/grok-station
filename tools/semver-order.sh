#!/bin/sh
# Compare an installed version to an available one.
# usage: semver-order.sh <installed> <available>
# Prints newer, same, older, or unknown.
# unknown: available is not MAJOR.MINOR.PATCH (never an upgrade).
# A non-semver installed version (a short commit id, or "dev") is newer when
# available is semver, so an existing station can move onto semantic versions.
set -eu
# semver-order:start
semver_order() {
  awk -v inst="$1" -v avail="$2" '
function ok(s,    n, a, i) {
  if (s == "" || s ~ /[^0-9.]/) return 0
  n = split(s, a, ".")
  if (n != 3) return 0
  for (i = 1; i <= 3; i++) if (a[i] !~ /^[0-9]+$/) return 0
  return 1
}
BEGIN {
  if (!ok(avail)) { print "unknown"; exit }
  if (!ok(inst)) { print "newer"; exit }
  split(inst, I, ".")
  split(avail, A, ".")
  for (i = 1; i <= 3; i++) {
    if (A[i] + 0 > I[i] + 0) { print "newer"; exit }
    if (A[i] + 0 < I[i] + 0) { print "older"; exit }
  }
  print "same"
}'
}
# semver-order:end
inst=$(printf '%s' "${1:-}" | tr -d '[:space:]')
avail=$(printf '%s' "${2:-}" | tr -d '[:space:]')
semver_order "$inst" "$avail"
