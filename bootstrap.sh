#!/bin/sh
# grok-station bootstrap: install or update in one line (safe to re-run; this is also how updates work).
#   wget -qO- https://raw.githubusercontent.com/bwilliam79/grok-station/main/bootstrap.sh | sudo sh
# Extra arguments go to install.sh, e.g. ... | sudo sh -s -- --verbose
# Never reads stdin (it is this script when piped), so there are no prompts.
set -eu

REPO_URL=https://github.com/bwilliam79/grok-station
BRANCH=main

main() {
  if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    G="$(printf '\033[32m')"; R="$(printf '\033[31m')"; B="$(printf '\033[1m')"; Z="$(printf '\033[0m')"
  else G=; R=; B=; Z=; fi
  die() { echo "${R}✗${Z} $*" >&2; exit 1; }
  ok() { echo "${G}✓${Z} $*"; }

  if [ "$(id -u)" != 0 ]; then
    if [ -f "$0" ] && command -v sudo >/dev/null 2>&1; then exec sudo sh "$0" "$@"; fi
    die "Run as root: wget -qO- $REPO_URL/raw/$BRANCH/bootstrap.sh | sudo sh"
  fi
  [ "$(uname -m)" = aarch64 ] || die "Needs 64-bit Raspberry Pi OS (aarch64); this system is $(uname -m)."

  LOG=/var/log/grok-station-install.log
  : > "$LOG"; chmod 0600 "$LOG"
  export GROK_STATION_LOG_APPEND=1   # install.sh keeps these lines in the same log
  echo "${B}grok-station${Z} bootstrap"

  if ! command -v git >/dev/null 2>&1 || [ ! -e /etc/ssl/certs/ca-certificates.crt ]; then
    echo "  ... Installing git"
    { apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git ca-certificates; } \
      < /dev/null >> "$LOG" 2>&1 || { tail -n 20 "$LOG" >&2; die "Couldn't install git (log: $LOG)"; }
    ok "Installing git"
  fi

  # Reuse an existing clone in the invoking user's home (how earlier installs were done), else a system location.
  DIR=/usr/local/src/grok-station
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    home=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    [ -n "$home" ] && [ -d "$home/grok-station/.git" ] && DIR="$home/grok-station"
  fi
  [ -n "${GROK_STATION_DIR:-}" ] && DIR=$GROK_STATION_DIR

  if [ -d "$DIR/.git" ]; then
    owner=$(stat -c %U "$DIR")
    g() { if [ "$owner" != root ]; then sudo -u "$owner" git -C "$DIR" "$@"; else git -C "$DIR" "$@"; fi; }
    echo "  ... Downloading grok-station (update in $DIR)"
    { g fetch --quiet origin && { [ "$(g rev-parse --abbrev-ref HEAD)" = "$BRANCH" ] || g checkout --quiet "$BRANCH"; } \
      && g pull --quiet --ff-only origin "$BRANCH"; } < /dev/null >> "$LOG" 2>&1 \
      || { tail -n 20 "$LOG" >&2; die "Couldn't update $DIR (local changes? log: $LOG)"; }
  else
    echo "  ... Downloading grok-station (into $DIR)"
    mkdir -p "$(dirname "$DIR")"
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$DIR" < /dev/null >> "$LOG" 2>&1 \
      || { tail -n 20 "$LOG" >&2; die "Couldn't clone $REPO_URL (log: $LOG)"; }
  fi
  ok "Downloading grok-station ($(git -c safe.directory="$DIR" -C "$DIR" rev-parse --short HEAD 2>/dev/null || echo '?'))"

  exec sh "$DIR/install.sh" "$@" < /dev/null
}

main "$@"
