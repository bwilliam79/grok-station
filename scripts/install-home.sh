#!/bin/sh
# Install grok-station home mode once, on this computer.
# macOS or Linux. Not Alpine (musl).
# The station never pushes code. You run this script yourself.
#
# Pins below are commit 0dc0537ea73d8b873d70a26ec942a091e63af953, the 0.11.4 scripts
# (macOS and Linux). A commit cannot name itself, so the pins name the commit
# before the pin commit. To re-pin after a release changes these files:
#   python3 tools/gen-deploy-pins.py "$(git rev-parse origin/main)"
# and put the new commit and hashes here.
#
# Files land in ~/.local/share/grok-station/bin (mode 0700).
# ~/.local/bin/gs-seal-reply is a two-line shell wrapper that execs the real
# file. It is not a symlink.
# Stdout is only the public key. Everything else goes to stderr.
#
#   sh scripts/install-home.sh \
#     --persona <slug> --station https://<addr> \
#     --pin-sha256 <fingerprint> --station-pub <b64>
#
# Set UPGRADE=1 to download and verify again before replacing existing files.
# An upgrade does not replace a key or config that is already there.
#
# Update an existing install (keeps the key, host binding and station pairing):
#   sh scripts/install-home.sh --update [--persona <slug>]
# It needs no station details: it reads which bot is installed from the
# existing config, refuses if there is no key and config, downloads the pinned
# files, checks every sha256, updates the vendored libraries as one swap, then
# replaces the helper files. Stdout is only the new helper version.
set -eu
umask 077

COMMIT=0dc0537ea73d8b873d70a26ec942a091e63af953
HASH_SETUP=a300ae170d4fccc716d301ea40ca0731f5b72dbeadf7548e61ec3d0b8a1e61c9
HASH_REPLY=edfffed995e846fcb3588a426644864a92694aaf2042c96acd6b215ccce2ad6b
HASH_COMMON=d231bf880e05c02384b750645a4e469393bc1794b9700fd7b328c27d7e04a8be
HASH_LOCK=2dcfee350776443e774da1ff0c6ab833cd26f3c0a629c8559d250ce07ad34426

die() {
  printf '%s\n' "$1" >&2
  exit 1
}

os=$(uname -s) || os=""
case "$os" in
  Darwin|Linux) ;;
  *) die "Home mode runs on macOS or Linux." ;;
esac

if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
  die "HOME is not set."
fi
case "$HOME" in
  /*) ;;
  *) die "HOME must be an absolute path." ;;
esac

persona=""
station=""
pin=""
pub=""
update=0
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--update" ]; then
    update=1
    shift
    continue
  fi
  if [ "$#" -lt 2 ]; then
    die "usage: sh install-home.sh --persona SLUG --station URL --pin-sha256 HEX --station-pub B64"
  fi
  case "$1" in
    --persona) persona=$2 ;;
    --station) station=$2 ;;
    --pin-sha256) pin=$2 ;;
    --station-pub) pub=$2 ;;
    *) die "usage: sh install-home.sh --persona SLUG --station URL --pin-sha256 HEX --station-pub B64" ;;
  esac
  shift 2
done

case "$persona$station$pin$pub" in
  *[\ \'\"\$\`]*)
    die "refusing unsafe characters in arguments"
    ;;
esac

CFG="$HOME/.config/grok-station"
if [ "$update" = "1" ]; then
  if [ -n "$station$pin$pub" ]; then
    die "--update keeps the existing pairing; don't pass --station, --pin-sha256 or --station-pub"
  fi
  if [ -z "$persona" ]; then
    found=""
    for f in "$CFG"/*.json; do
      [ -f "$f" ] || continue
      b=${f##*/}
      b=${b%.json}
      [ "$b" = "vendor-manifest" ] && continue
      if [ -n "$found" ]; then
        die "more than one bot is installed here; pass --persona SLUG"
      fi
      found=$b
    done
    persona=$found
  fi
  if [ -z "$persona" ]; then
    die "no home install found to update; run the full install"
  fi
fi

case "$persona" in
  grok) die "refusing persona" ;;
  [a-z]*)
    case "$persona" in
      *[!a-z0-9-]*) die "refusing persona" ;;
    esac
    ;;
  *) die "refusing persona" ;;
esac
npersona=$(printf '%s' "$persona" | wc -c | tr -cd '0-9')
if [ "$npersona" -lt 2 ] || [ "$npersona" -gt 24 ]; then
  die "refusing persona"
fi

if [ "$update" = "1" ]; then
  for f in "$CFG/keys/$persona.key" "$CFG/$persona.json"; do
    if [ -L "$f" ] || [ ! -f "$f" ]; then
      die "no home install for $persona here; run the full install"
    fi
  done
  UPGRADE=1
  # init is skipped in update mode; these placeholders only satisfy the checks below and are never used.
  station=https://update.invalid
  pin=0000000000000000000000000000000000000000000000000000000000000000
  pub=update
fi

case "$station" in
  https://*) ;;
  *) die "refusing station" ;;
esac
rest=${station#https://}
case "$rest" in
  */) rest=${rest%/} ;;
esac
case "$rest" in
  ""|*'/'*|*'@'*|*'\'*|*'?'*|*'#'*) die "refusing station" ;;
esac

case "$pin" in
  *[!0-9a-fA-F]*) die "refusing pin" ;;
esac
npin=$(printf '%s' "$pin" | wc -c | tr -cd '0-9')
if [ "$npin" -ne 64 ]; then
  die "refusing pin"
fi

case "$pub" in
  *[!A-Za-z0-9+/=]*) die "refusing station public key" ;;
esac
if [ -z "$pub" ]; then
  die "refusing station public key"
fi

if ! command -v curl >/dev/null 2>&1; then
  die "curl is required."
fi
if ! command -v python3 >/dev/null 2>&1; then
  die "python3 is required."
fi
if command -v sha256sum >/dev/null 2>&1; then
  HASHER=sha256sum
elif command -v shasum >/dev/null 2>&1; then
  HASHER=shasum
else
  die "sha256sum or shasum is required."
fi

BIN="$HOME/.local/share/grok-station/bin"
stage=""
phase=""
cleanup() {
  rc=$?
  if [ "$rc" -ne 0 ] && [ "$phase" = "setup" ]; then
    printf '%s\n' "partial install; re-run with UPGRADE=1" >&2
  fi
  if [ -n "${stage:-}" ] && [ -d "$stage" ]; then
    case "$stage" in
      "$BIN"/.stage.*) rm -rf "$stage" || true ;;
    esac
  fi
}
trap cleanup EXIT
trap 'cleanup; exit 1' INT TERM

sha256_of() {
  sumf="$stage/.sum.$$"
  if [ "$HASHER" = "sha256sum" ]; then
    sha256sum "$1" > "$sumf" || return 1
  else
    shasum -a 256 "$1" > "$sumf" || return 1
  fi
  hex=$(cut -d " " -f 1 "$sumf") || return 1
  rm -f "$sumf"
  printf '%s\n' "$hex"
}

make_private() {
  _rest=$1
  _acc=""
  case "$_rest" in
    /*) ;;
    *) die "bad path" ;;
  esac
  while [ -n "$_rest" ]; do
    _rest=${_rest#/}
    _seg=${_rest%%/*}
    if [ "$_rest" = "$_seg" ]; then
      _rest=""
    else
      _rest=${_rest#*/}
    fi
    if [ -z "$_seg" ]; then
      continue
    fi
    _acc="$_acc/$_seg"
    if [ -d "$_acc" ] && [ ! -L "$_acc" ]; then
      continue
    fi
    if [ -e "$_acc" ] || [ -L "$_acc" ]; then
      die "refusing $_acc"
    fi
    mkdir -m 700 "$_acc"
    chmod 700 "$_acc"
  done
}

refuse_exists() {
  dest=$1
  if [ -L "$dest" ]; then
    die "refusing symlink: $dest"
  fi
  if [ -e "$dest" ]; then
    if [ ! -f "$dest" ]; then
      die "not a regular file: $dest"
    fi
    if [ "${UPGRADE:-}" != "1" ]; then
      die "already exists (set UPGRADE=1 to replace): $dest"
    fi
  fi
}

fetch_one() {
  base=$1
  url=$2
  sum=$3
  mode=$4
  case "$base" in
    *[!A-Za-z0-9._-]*) die "bad name" ;;
  esac
  case "$url" in
    https://raw.githubusercontent.com/bwilliam79/grok-station/*/*) ;;
    *) die "refusing url" ;;
  esac
  case "$url" in
    *'?'*|*'#'*|*'@'*|*' '*) die "refusing url" ;;
  esac
  case "$sum" in
    *[!0-9a-f]*) die "bad checksum" ;;
  esac
  nsum=$(printf '%s' "$sum" | wc -c | tr -cd '0-9')
  if [ "$nsum" -ne 64 ]; then
    die "bad checksum"
  fi
  tmp="$stage/$base"
  code=$(curl --proto =https --tlsv1.2 --max-redirs 0 -sS -o "$tmp" -w "%{http_code}" "$url") || die "download failed: $base"
  case "$code" in
    200) ;;
    403|404) die "pinned file unavailable (repo private or commit gone): $base" ;;
    *) die "download failed (HTTP $code): $base" ;;
  esac
  got=$(sha256_of "$tmp") || exit 1
  if [ "$got" != "$sum" ]; then
    die "checksum mismatch: $base"
  fi
  chmod "$mode" "$tmp"
}

place_one() {
  base=$1
  dest="$BIN/$base"
  refuse_exists "$dest"
  mv -f "$stage/$base" "$dest"
}

RAW="https://raw.githubusercontent.com/bwilliam79/grok-station/${COMMIT}"
refuse_exists "$BIN/gs-seal-setup"
refuse_exists "$BIN/gs-seal-reply"
refuse_exists "$BIN/gs_seal_common.py"
refuse_exists "$BIN/vendor-lock.txt"
make_private "$BIN"
chmod 700 "$BIN"
python3 - "$BIN" <<'PY'
import os, stat, sys
cur = os.path.abspath(sys.argv[1])
uid = os.geteuid()
while True:
    try:
        st = os.lstat(cur)
    except OSError:
        sys.stderr.write("cannot stat %s\n" % cur)
        raise SystemExit(1)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        sys.stderr.write("refusing %s\n" % cur)
        raise SystemExit(1)
    if st.st_uid != uid and st.st_uid != 0:
        sys.stderr.write("not owned by you or root: %s\n" % cur)
        raise SystemExit(1)
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        sys.stderr.write("group or world writable: %s\n" % cur)
        raise SystemExit(1)
    if cur == "/":
        break
    parent = os.path.dirname(cur)
    if parent == cur:
        raise SystemExit(1)
    cur = parent
PY
stage=$(mktemp -d "$BIN/.stage.XXXXXX") || exit 1
chmod 700 "$stage"
CURL_HOME="$stage/nocurlrc"
mkdir -m 700 "$CURL_HOME"
export CURL_HOME

fetch_one gs-seal-setup "$RAW/scripts/gs-seal-setup" "$HASH_SETUP" 700
fetch_one gs-seal-reply "$RAW/scripts/gs-seal-reply" "$HASH_REPLY" 700
fetch_one gs_seal_common.py "$RAW/scripts/gs_seal_common.py" "$HASH_COMMON" 600
fetch_one vendor-lock.txt "$RAW/scripts/vendor-lock.txt" "$HASH_LOCK" 600

phase=setup
"$stage/gs-seal-setup" vendor-install --lock "$stage/vendor-lock.txt"
keyf="$HOME/.config/grok-station/keys/$persona.key"
cfgf="$HOME/.config/grok-station/$persona.json"
if [ -L "$keyf" ] || [ -L "$cfgf" ]; then
  die "refusing symlink in ~/.config/grok-station"
fi
if [ "$update" = "1" ]; then
  if [ ! -f "$keyf" ] || [ ! -f "$cfgf" ]; then
    die "key or config went missing during the update"
  fi
elif [ -e "$keyf" ] && [ -e "$cfgf" ]; then
  printf '%s\n' "Key and config are already in place. Not minting a new key." >&2
else
  if [ ! -e "$keyf" ]; then
    "$stage/gs-seal-setup" keygen --persona "$persona"
  fi
  if [ ! -e "$cfgf" ]; then
    "$stage/gs-seal-setup" init --persona "$persona" --station "$station" --pin-sha256 "$pin" --station-pub "$pub"
  fi
fi

place_one gs-seal-setup
place_one gs-seal-reply
place_one gs_seal_common.py
place_one vendor-lock.txt

wrapdir="$HOME/.local/bin"
refuse_exists "$wrapdir/gs-seal-reply"
make_private "$wrapdir"
real="$BIN/gs-seal-reply"
if [ -L "$real" ] || [ ! -f "$real" ]; then
  die "refusing the installed program"
fi
python3 - "$real" "$stage/wrapper" <<'PY'
import sys
real, dest = sys.argv[1], sys.argv[2]
quoted = "'" + real.replace("'", "'\\''") + "'"
line = "exec " + quoted + " \"$@\"\n"
with open(dest, "w", encoding="utf-8", newline="\n") as fh:
    fh.write("#!/bin/sh\n")
    fh.write(line)
PY
mv -f "$stage/wrapper" "$wrapdir/gs-seal-reply"
chmod 700 "$wrapdir/gs-seal-reply"
phase=done
if [ "$update" = "1" ]; then
  "$BIN/gs-seal-reply" version || die "updated, but the helper didn't report its version"
  printf '%s\n' "Updated $BIN" >&2
  exit 0
fi
printf '%s\n' "Installed into $BIN" >&2
