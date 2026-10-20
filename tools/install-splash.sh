#!/bin/sh
# Install or refresh the Echo Plymouth theme. Root only. Safe to re-run.
# Copies the theme into a temp dir, checks the files, then installs them under
# /usr/share/plymouth/themes/echo. Without --prepare it switches the default
# (plymouth-set-default-theme echo) and rebuilds the initramfs
# (plymouth-set-default-theme -R echo).
# --prepare only installs the files; install.sh then runs plymouth-set-default-theme -R echo.
# If the echo files are missing, a standalone run falls back to the text theme.
# This script never deletes another theme directory (older Pis may still have themes/grok).
set -eu

PREPARE=0
ROOT=
for arg in "$@"; do
  case $arg in
    --prepare) PREPARE=1 ;;
    *) ROOT=$arg ;;
  esac
done
if [ -z "$ROOT" ]; then
  ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
fi

die() { echo "install-splash.sh: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "must run as root"

THEME=/usr/share/plymouth/themes/echo
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

src_ok() {
  [ -s "$ROOT/splash/echo.plymouth" ] && [ -s "$ROOT/splash/echo.script" ] \
    && [ -s "$ROOT/assets/brand/splash-1920x1080.png" ] && [ -s "$ROOT/assets/brand/splash-800x480.png" ]
}

png_ok() {
  sig=$(od -An -t x1 -N 4 "$1" | tr -d ' \n')
  [ "$sig" = "89504e47" ]
}

verify_dir() {
  d=$1
  [ -s "$d/echo.plymouth" ] && [ -s "$d/echo.script" ] || return 1
  grep -q 'ModuleName=script' "$d/echo.plymouth" || return 1
  grep -q '/usr/share/plymouth/themes/echo/echo.script' "$d/echo.plymouth" || return 1
  grep -q 'splash-1920x1080.png' "$d/echo.script" || return 1
  grep -q 'splash-800x480.png' "$d/echo.script" || return 1
  png_ok "$d/splash-1920x1080.png" || return 1
  png_ok "$d/splash-800x480.png" || return 1
}

fallback_text() {
  echo "install-splash.sh: echo theme files missing; Plymouth text theme (other theme dirs kept)" >&2
  if command -v plymouth-set-default-theme >/dev/null 2>&1; then
    plymouth-set-default-theme text || true
    plymouth-set-default-theme -R text || true
  fi
}

if ! src_ok; then
  if [ "$PREPARE" -eq 0 ]; then
    fallback_text
    exit 0
  fi
  echo "install-splash.sh: echo theme files missing" >&2
  exit 1
fi

install -m644 "$ROOT/splash/echo.plymouth" "$ROOT/splash/echo.script" "$STAGE/"
install -m644 "$ROOT/assets/brand/splash-1920x1080.png" "$ROOT/assets/brand/splash-800x480.png" "$STAGE/"
verify_dir "$STAGE" || die "staged echo theme failed verification"

install -d -m755 "$THEME"
install -m644 "$STAGE/echo.plymouth" "$STAGE/echo.script" \
  "$STAGE/splash-1920x1080.png" "$STAGE/splash-800x480.png" "$THEME/"
verify_dir "$THEME" || die "installed echo theme failed verification"

if [ "$PREPARE" -eq 1 ]; then
  exit 0
fi

command -v plymouth-set-default-theme >/dev/null 2>&1 || die "plymouth-set-default-theme not found"
# Switch the default first, then rebuild the initramfs (-R). Re-running does the same thing.
plymouth-set-default-theme echo
plymouth-set-default-theme -R echo
