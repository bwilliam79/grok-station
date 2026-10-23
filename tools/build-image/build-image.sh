#!/usr/bin/env bash
# Build a bootable SD card image from the official Raspberry Pi OS Lite
# (64-bit) download. This script does not ship a prebuilt grok-station image.
#
# It downloads the image Raspberry Pi publishes, checks the published SHA256,
# loop-mounts the root filesystem, and copies this repository (git archive of
# HEAD) plus a first-boot unit. The unit runs install.sh once on the Pi.
# No SSH key, hostname, or password is written, and no Wi-Fi profile unless
# --wifi-from-env is given.
#
# Loop-mount needs root: losetup turns the image file into a block device and
# mount attaches its ext4 root. Only that file is attached. The computer's
# own disks are not scanned or written. --dry-run prints the commands and
# does not download, mount, or need root.
#
# See docs/sd-image.md.
set -euo pipefail

# Official index used by Raspberry Pi Imager. The checksum is read from this
# list (and from the sibling .sha256 file when Raspberry Pi publishes one).
# No checksum is stored in this script.
OS_LIST_URL=https://downloads.raspberrypi.com/os_list_imagingutility_v4.json

# Size ceilings. The Lite download is a few hundred MB and the uncompressed
# image is a few GB. Anything larger is refused before it is mounted.
MAX_OS_LIST=$((5 * 1024 * 1024))
MAX_SUMS=$((1024 * 1024))
MAX_XZ=$((2 * 1024 * 1024 * 1024))
MAX_IMG=$((8 * 1024 * 1024 * 1024))

# Archive member names that must not be copied onto the card.
SECRET_PATH_RE='(^|/)\.git(/|$)|(^|/)\.env$|(^|/)id_rsa$|(^|/)id_ed25519$|(^|/)\.ssh/|\.pem$|(^|/)credentials\.json$|(^|/)secrets\.json$|(^|/)\.netrc$'

DRY=0
XZ=0
WIFI=0
OUTPUT=
MODE=build
SELECT_ONLY=
CHECK_URL=
SUMS_FILE=
SUMS_NAME=
LOOP=
MNT=
WORKDIR=
REPO=
COMMIT=

die() { printf 'build-image: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
usage: sudo tools/build-image/build-image.sh [--dry-run] [--xz] [--output PATH]
       tools/build-image/build-image.sh --select-only OS-LIST.json
       tools/build-image/build-image.sh --check-url URL
       tools/build-image/build-image.sh --check-image-url URL
       tools/build-image/build-image.sh --parse-sums FILE BASENAME

Download the official Raspberry Pi OS Lite (64-bit) image, check its
published SHA256, and add this repository plus a first-boot unit.
Writes an .img (or .img.xz with --xz). Sets Wi-Fi only with --wifi-from-env; never SSH, a
hostname, or a password.

  --dry-run           print every action and change nothing (no root, no network)
  --xz                compress the finished image with xz
  --wifi-from-env     add one Wi-Fi network from GS_WIFI_SSID, GS_WIFI_PSK and
                      GS_WIFI_COUNTRY (never printed; the image then holds
                      that password, so do not share it)
  --output PATH       image file to write (refuses /dev and existing files)
  --select-only FILE  print the Lite arm64 entry in an OS-list JSON, then stop
  --check-url URL     exit 0 only if URL is https://downloads.raspberrypi.com/...
  --check-image-url   same, and the path must be a Lite arm64 .img.xz
  --parse-sums FILE NAME
                      print the single SHA256 that FILE publishes for NAME
  --help

See docs/sd-image.md.
EOF
}

usage_err() { usage >&2; exit 2; }

step() { printf 'step %s: %s\n' "$1" "$2"; }

# Print a command the way it would be run. Run it only when not in dry-run.
run() {
  printf '+'
  local arg
  for arg in "$@"; do
    printf ' %q' "$arg"
  done
  printf '\n'
  if [[ "$DRY" -eq 0 ]]; then
    "$@"
  fi
}

# Host allowlist. https only, no userinfo, no query, no dot-dot.
assert_official_url() {
  local url=$1
  [[ "$url" =~ ^https://downloads\.raspberrypi\.com/[A-Za-z0-9._~/-]+$ ]] \
    || die "refusing URL that is not https://downloads.raspberrypi.com/...: ${url}"
  [[ "$url" != *..* ]] || die "refusing URL that contains ..: ${url}"
}

# The Lite arm64 image layout published under downloads.raspberrypi.com:
#   /raspios_lite_arm64/images/<release-dir>/<file>.img.xz
# [[ == ]] globs let * match a slash, so the path shape is a regex: exactly
# one directory under images/, then a lite arm64 .img.xz file name.
assert_image_url() {
  local url=$1 rest file prefix
  assert_official_url "$url"
  prefix="https://downloads.raspberrypi.com/raspios_lite_arm64/images/"
  [[ "$url" == "$prefix"* ]] || die "refusing URL that is not a raspios_lite_arm64 image: ${url}"
  rest=${url#"$prefix"}
  [[ "$rest" =~ ^[A-Za-z0-9._~-]+/[A-Za-z0-9._~-]+\.img\.xz$ ]] \
    || die "refusing URL that is not <release>/<file>.img.xz: ${url}"
  file=${rest#*/}
  [[ "$file" == *arm64* && "$file" == *lite* ]] \
    || die "refusing URL whose file name is not lite arm64: ${url}"
}

cleanup() {
  local rc=$?
  set +e
  if [[ -n "$MNT" ]] && command -v mountpoint >/dev/null && mountpoint -q "$MNT"; then
    umount "$MNT"
  fi
  if [[ -n "$LOOP" ]]; then
    losetup -d "$LOOP"
  fi
  if [[ "$DRY" -eq 0 && -n "$WORKDIR" && -d "$WORKDIR" ]]; then
    rm -rf "$WORKDIR"
  fi
  exit "$rc"
}

# Print url, hashes, and sizes for the newest Raspberry Pi OS Lite (64-bit)
# entry. Checksums come from the file; this function does not fetch anything.
select_image() {
  local json=$1
  python3 - "$json" <<'PY'
import json
import sys

HOST = "https://downloads.raspberrypi.com/raspios_lite_arm64/"
HEX = set("0123456789abcdef")


def is_hex64(value):
    return isinstance(value, str) and len(value) == 64 and set(value.lower()) <= HEX


def pos_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return ""
    return str(value)


def lite_arm64(node):
    url = node.get("url") or ""
    name = node.get("name") or ""
    if not url.startswith(HOST) or not url.endswith(".img.xz"):
        return None
    if ".." in url or " " in url or "\\" in url:
        return None
    # One directory under images/, matching the bash allowlist.
    rest = url[len(HOST):]
    parts = rest.split("/")
    if len(parts) != 3 or parts[0] != "images" or not parts[1] or not parts[2]:
        return None
    if "Lite" not in name or "64-bit" not in name:
        return None
    if "Desktop" in name or "Full" in name:
        return None
    if "arm64" not in parts[2] or "lite" not in parts[2]:
        return None
    sha = (node.get("image_download_sha256") or "").lower()
    ext = (node.get("extract_sha256") or "").lower()
    # The current OS list publishes extract_sha256 but not always
    # image_download_sha256. A missing download hash is allowed; the bash side
    # then requires the <image>.img.xz.sha256 sibling instead. A present but
    # malformed hash still rejects the entry.
    if not is_hex64(ext) or (sha and not is_hex64(sha)):
        return None
    return {
        "url": url,
        "image_download_sha256": sha,
        "extract_sha256": ext,
        "release_date": str(node.get("release_date") or ""),
        "image_download_size": pos_int(node.get("image_download_size")),
        "extract_size": pos_int(node.get("extract_size")),
        "name": name,
    }


def walk(node, found):
    if isinstance(node, dict):
        chosen = lite_arm64(node)
        if chosen:
            found.append(chosen)
        for key in ("os_list", "subitems", "items"):
            if key in node:
                walk(node[key], found)
    elif isinstance(node, list):
        for item in node:
            walk(item, found)


def main():
    try:
        doc = json.load(open(sys.argv[1], encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write("build-image: cannot read OS list: %s\n" % type(exc).__name__)
        sys.exit(1)
    found = []
    walk(doc, found)
    if not found:
        sys.stderr.write("build-image: no Raspberry Pi OS Lite (64-bit) image in the official list\n")
        sys.exit(1)

    def date_key(text):
        parts = text.split("-")
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            return (int(parts[0]), int(parts[1]), int(parts[2]))
        return (0, 0, 0)

    found.sort(key=lambda item: date_key(item["release_date"]))
    best = found[-1]
    best_key = date_key(best["release_date"])
    tied = [item for item in found if date_key(item["release_date"]) == best_key]
    urls = {item["url"] for item in tied}
    if len(urls) != 1:
        sys.stderr.write("build-image: ambiguous Lite arm64 images share release_date %s\n" % best["release_date"])
        sys.exit(1)
    for key in (
        "url",
        "image_download_sha256",
        "extract_sha256",
        "release_date",
        "image_download_size",
        "extract_size",
        "name",
    ):
        sys.stdout.write("%s=%s\n" % (key, best[key]))


if __name__ == "__main__":
    main()
PY
}

# Print the one SHA256 a published checksum file gives for this basename.
# A line that names another file is ignored. More than one differing hash
# for this file is an error. A file that is only a hash applies to this file.
parse_sums() {
  local file=$1 base=$2
  python3 - "$file" "$base" <<'PY'
import re
import sys

HEX = re.compile(r"^[0-9a-f]{64}$")


def main():
    path, base = sys.argv[1], sys.argv[2]
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        sys.stderr.write("build-image: cannot read checksum file\n")
        sys.exit(1)
    found = []
    for raw in text.splitlines():
        parts = raw.split()
        if not parts:
            continue
        digest = parts[0].lower()
        if digest.startswith("\\"):
            digest = digest[1:]
        if not HEX.fullmatch(digest):
            continue
        names = [part.lstrip("*") for part in parts[1:]]
        if names and base not in names and not any(name.endswith("/" + base) for name in names):
            continue
        found.append(digest)
    uniq = sorted(set(found))
    if len(uniq) != 1:
        sys.stderr.write("build-image: checksum file has no single hash for this image\n")
        sys.exit(1)
    sys.stdout.write(uniq[0] + "\n")


if __name__ == "__main__":
    main()
PY
}

# Download url to dest. The final hop must stay on the allowlisted host.
# max is a byte ceiling (curl --max-filesize, then checked again on disk).
download_required() {
  local url=$1 dest=$2 max=$3
  local meta code final size
  assert_official_url "$url"
  if [[ "$DRY" -eq 1 ]]; then
    printf '+ curl --fail --silent --show-error --location --proto =https --tlsv1.2 --retry 3 --max-filesize %s --output %q -- %q\n' \
      "$max" "$dest" "$url"
    printf '+ reject the download unless the final URL is still on downloads.raspberrypi.com\n'
    return 0
  fi
  meta=$(curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --retry 3 --retry-delay 2 \
    --max-filesize "$max" --output "$dest" \
    --write-out '%{http_code} %{url_effective}' -- "$url") || die "download failed: ${url}"
  code=${meta%% *}
  final=${meta#* }
  [[ "$code" == "200" ]] || die "download HTTP ${code}: ${url}"
  assert_official_url "$final"
  size=$(stat -c '%s' "$dest")
  [[ "$size" -gt 0 && "$size" -le "$max" ]] || die "download size ${size} is outside 1..${max} bytes: ${url}"
}

# Like download_required, but a 404 means "Raspberry Pi did not publish this"
# and returns 1. Any other failure stops the build.
download_optional() {
  local url=$1 dest=$2 max=$3
  local meta code final size
  assert_official_url "$url"
  if [[ "$DRY" -eq 1 ]]; then
    printf '+ curl --silent --show-error --location --proto =https --tlsv1.2 --max-filesize %s --output %q -- %q\n' \
      "$max" "$dest" "$url"
    printf '+ if HTTP 404: this checksum was not published; keep going\n'
    return 0
  fi
  meta=$(curl --silent --show-error --location --proto '=https' --tlsv1.2 \
    --retry 3 --retry-delay 2 \
    --max-filesize "$max" --output "$dest" \
    --write-out '%{http_code} %{url_effective}' -- "$url") || {
    rm -f -- "$dest"
    die "checksum download failed: ${url}"
  }
  code=${meta%% *}
  final=${meta#* }
  if [[ "$code" == "404" ]]; then
    rm -f -- "$dest"
    return 1
  fi
  [[ "$code" == "200" ]] || { rm -f -- "$dest"; die "checksum HTTP ${code}: ${url}"; }
  assert_official_url "$final"
  size=$(stat -c '%s' "$dest")
  [[ "$size" -gt 0 && "$size" -le "$max" ]] || { rm -f -- "$dest"; die "checksum size rejected: ${url}"; }
}

# Every published hash for this label must be the same 64 hex digits.
agree() {
  local label=$1
  shift
  local first="" digest
  [[ "$#" -ge 1 ]] || die "no published checksum for ${label}"
  for digest in "$@"; do
    digest=${digest,,}
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || die "published checksum is not 64 hex digits (${label})"
    if [[ -z "$first" ]]; then
      first=$digest
    elif [[ "$digest" != "$first" ]]; then
      die "published checksums disagree (${label})"
    fi
  done
  printf '%s\n' "$first"
}

# sha256sum -c against a one-line sums file. expected is the published hash.
verify_sha256() {
  local file=$1 expected=$2 label=$3
  local dir name sums
  if [[ "$DRY" -eq 1 ]]; then
    printf '+ sha256sum -c  # %s must match the published checksum\n' "$label"
    return 0
  fi
  expected=${expected,,}
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || die "published checksum is not 64 hex digits (${label})"
  dir=$(dirname "$file")
  name=$(basename "$file")
  sums="${WORKDIR}/sha256-${label}.txt"
  printf '%s  %s\n' "$expected" "$name" > "$sums"
  printf '+ sha256sum -c %s  # %s\n' "$sums" "$label"
  (cd "$dir" && sha256sum -c "$sums") || die "SHA256 mismatch (${label})"
}

check_wifi_env() {
  [[ -n "${GS_WIFI_SSID:-}" ]] || die "--wifi-from-env needs GS_WIFI_SSID"
  [[ "${#GS_WIFI_SSID}" -le 32 ]] || die "GS_WIFI_SSID is longer than 32 bytes"
  [[ "${#GS_WIFI_PSK}" -ge 8 && "${#GS_WIFI_PSK}" -le 63 ]] || die "GS_WIFI_PSK must be 8 to 63 characters"
  [[ "${GS_WIFI_COUNTRY:-}" =~ ^[A-Z]{2}$ ]] || die "GS_WIFI_COUNTRY must be a two-letter code such as US"
  case "$GS_WIFI_SSID$GS_WIFI_PSK" in
    *$'\n'*|*$'\r'*) die "Wi-Fi values must be one line" ;;
  esac
}

# NetworkManager keyfile, root-only. Raspberry Pi OS keeps Wi-Fi blocked until
# a country is set, so firstboot.sh sets it from the country file.
write_wifi() {
  local mnt=$1 dir esc_ssid esc_psk
  dir="${mnt}/etc/NetworkManager/system-connections"
  install -d -m 0700 "$dir"
  esc_ssid=${GS_WIFI_SSID//\\/\\\\}
  esc_psk=${GS_WIFI_PSK//\\/\\\\}
  (umask 077
   printf '[connection]\nid=grok-station-wifi\ntype=wifi\nautoconnect=true\n\n[wifi]\nmode=infrastructure\nssid=%s\n\n[wifi-security]\nkey-mgmt=wpa-psk\npsk=%s\n\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n' \
     "$esc_ssid" "$esc_psk" > "${dir}/grok-station-wifi.nmconnection"
   printf '%s\n' "$GS_WIFI_COUNTRY" > "${mnt}/var/lib/grok-station-firstboot-wifi-country")
  chown root:root "${dir}/grok-station-wifi.nmconnection" "${mnt}/var/lib/grok-station-firstboot-wifi-country"
}

need_live_tools() {
  local cmd
  for cmd in curl sha256sum xz git python3 losetup mount umount mountpoint tar install stat; do
    command -v "$cmd" >/dev/null || die "missing command: ${cmd}"
  done
  [[ "$(id -u)" -eq 0 ]] || die "loop-mount needs root (losetup and mount). Re-run with sudo. --dry-run does not need root."
}

archive_is_clean() {
  local tarfile=$1 listing bad
  listing=$(tar -tf "$tarfile") || die "cannot list git archive"
  bad=$(printf '%s\n' "$listing" | grep -E "$SECRET_PATH_RE" || true)
  if [[ -n "$bad" ]]; then
    printf '%s\n' "$bad" >&2
    die "git archive lists a secret-shaped path; refusing to copy it"
  fi
}

build() {
  local p2_start p2_len selection url json_sha ext_sha rel dl_size ext_size base sib_url sib_hash="" published img uncomp part unit_src ver waited line
  local -a hashes

  if [[ "$WIFI" -eq 1 ]]; then
    check_wifi_env
  fi
  REPO=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)
  COMMIT=$(git -C "$REPO" -c "safe.directory=${REPO}" rev-parse HEAD)
  if [[ -n "$(git -C "$REPO" -c "safe.directory=${REPO}" status --porcelain)" ]]; then
    printf 'build-image: note: uncommitted changes are not copied; the image is git archive of HEAD %s\n' "$COMMIT" >&2
  fi

  if [[ -z "$OUTPUT" ]]; then
    if [[ "$XZ" -eq 1 ]]; then
      OUTPUT="${PWD}/grok-station-raspios-lite-arm64.img.xz"
    else
      OUTPUT="${PWD}/grok-station-raspios-lite-arm64.img"
    fi
  fi
  case "$OUTPUT" in
    /dev|/dev/*) die "refusing to write a device node (${OUTPUT}); pick a file path" ;;
  esac
  if [[ -e "$OUTPUT" ]]; then
    die "output already exists: ${OUTPUT}"
  fi

  # --- step 1. Official OS list. No checksum is baked into this script.
  step 1 "fetch the official OS list"
  printf '  %s\n' "$OS_LIST_URL"
  if [[ "$DRY" -eq 1 ]]; then
    download_required "$OS_LIST_URL" "<work-dir>/os-list.json" "$MAX_OS_LIST"
  else
    need_live_tools
    umask 022
    WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/grok-station-image.XXXXXX")
    trap cleanup EXIT
    download_required "$OS_LIST_URL" "${WORKDIR}/os-list.json" "$MAX_OS_LIST"
  fi

  # --- step 2. Pick Lite (64-bit) only. Newest release_date wins.
  step 2 "select Raspberry Pi OS Lite (64-bit) from that list"
  printf '  require host downloads.raspberrypi.com and path raspios_lite_arm64\n'
  printf '  require published extract_sha256, and image_download_sha256 or a .sha256 sibling\n'
  if [[ "$DRY" -eq 1 ]]; then
    printf '+ python3 select the newest name containing Lite and 64-bit\n'
  else
    selection=$(select_image "${WORKDIR}/os-list.json")
    url=
    json_sha=
    ext_sha=
    rel=
    dl_size=
    ext_size=
    while IFS= read -r line; do
      case "$line" in
        url=*) url=${line#url=} ;;
        image_download_sha256=*) json_sha=${line#image_download_sha256=} ;;
        extract_sha256=*) ext_sha=${line#extract_sha256=} ;;
        release_date=*) rel=${line#release_date=} ;;
        image_download_size=*) dl_size=${line#image_download_size=} ;;
        extract_size=*) ext_size=${line#extract_size=} ;;
      esac
    done <<< "$selection"
    [[ -n "$url" && -n "$ext_sha" ]] || die "OS list selection is incomplete"
    assert_image_url "$url"
    printf '  selected %s\n' "$url"
    printf '  release_date %s\n' "$rel"
  fi

  # --- step 3. Download the compressed image from the selected URL.
  step 3 "download the official .img.xz"
  if [[ "$DRY" -eq 1 ]]; then
    printf '+ curl the selected raspios_lite_arm64 URL (max %s bytes)\n' "$MAX_XZ"
  else
    base=$(basename "$url")
    download_required "$url" "${WORKDIR}/${base}" "$MAX_XZ"
    if [[ -n "$dl_size" ]]; then
      [[ "$(stat -c '%s' "${WORKDIR}/${base}")" -eq "$dl_size" ]] \
        || die "published image_download_size does not match the download"
    fi
  fi

  # --- step 4. Published checksums must agree, then sha256sum -c.
  step 4 "verify SHA256 against the official published checksum"
  if [[ "$DRY" -eq 1 ]]; then
    printf '+ curl <image>.img.xz.sha256 when Raspberry Pi publishes that sibling\n'
    printf '+ agree that sibling with image_download_sha256 from the OS list\n'
    verify_sha256 "<work-dir>/image.img.xz" "published" "image_download_sha256"
  else
    sib_url="${url}.sha256"
    if download_optional "$sib_url" "${WORKDIR}/image.sha256" "$MAX_SUMS"; then
      sib_hash=$(parse_sums "${WORKDIR}/image.sha256" "$base")
      printf '  sibling checksum published\n'
    else
      [[ -n "$json_sha" ]] || die "no published download checksum (no image_download_sha256 and no .sha256 sibling)"
      printf '  no sibling .sha256; using image_download_sha256 from the official OS list\n'
    fi
    # At least one download hash must exist (agree dies with none).
    hashes=()
    if [[ -n "$json_sha" ]]; then
      hashes+=("$json_sha")
    fi
    if [[ -n "$sib_hash" ]]; then
      hashes+=("$sib_hash")
    fi
    published=$(agree "image_download_sha256" "${hashes[@]}")
    verify_sha256 "${WORKDIR}/${base}" "$published" "image_download_sha256"
  fi

  # --- step 5. Refuse an uncompressed image over the ceiling, then decompress.
  step 5 "check the uncompressed size, then decompress xz"
  if [[ "$DRY" -eq 1 ]]; then
    printf '+ xz --robot --list <image.img.xz>\n'
    printf '+ refuse if the uncompressed size is above %s bytes\n' "$MAX_IMG"
    printf '+ xz --decompress --keep --stdout <image.img.xz> > <image.img>\n'
  else
    uncomp=$(xz --robot --list "${WORKDIR}/${base}" | awk -F '\t' '$1 == "totals" { print $5; exit }')
    [[ "$uncomp" =~ ^[0-9]+$ ]] || die "could not read the uncompressed size"
    [[ "$uncomp" -gt 0 && "$uncomp" -le "$MAX_IMG" ]] || die "uncompressed image is ${uncomp} bytes; ceiling is ${MAX_IMG}"
    if [[ -n "$ext_size" && "$uncomp" != "$ext_size" ]]; then
      die "published extract_size does not match the xz uncompressed size"
    fi
    img="${WORKDIR}/raspios.img"
    xz --decompress --keep --stdout "${WORKDIR}/${base}" > "$img"
    [[ "$(stat -c '%s' "$img")" -eq "$uncomp" ]] || die "decompressed size does not match xz --list"
  fi

  # --- step 6. The OS list's extract_sha256 covers the uncompressed .img.
  step 6 "verify the .img against the published extract_sha256"
  if [[ "$DRY" -eq 1 ]]; then
    verify_sha256 "<work-dir>/raspios.img" "published" "extract_sha256"
  else
    verify_sha256 "$img" "$ext_sha" "extract_sha256"
  fi

  # --- step 7. Loop-mount. Root only from here. Partition 2 is the ext4 root.
  step 7 "loop-mount the root partition (needs root)"
  printf '  losetup attaches this image file only. It does not write the computer'\''s disks.\n'
  printf '  Partition 1 is the FAT boot firmware and is not mounted or modified.\n'
  printf '  Partition 2 is the ext4 root. Mounting it needs root.\n'
  if [[ "$DRY" -eq 1 ]]; then
    printf '+ losetup --find --show --partscan <image.img>\n'
    printf '+ mount <loop>p2 <mnt>\n'
    printf '+ require /etc/rpi-issue or /usr/lib/raspberrypi-sys-mods in the mount\n'
  else
    LOOP=$(losetup --find --show --partscan "$img")
    part="${LOOP}p2"
    waited=0
    while [[ ! -b "$part" && "$waited" -lt 20 ]]; do
      if command -v partx >/dev/null; then
        partx --update "$LOOP" || true
      fi
      sleep 0.1
      waited=$((waited + 1))
    done
    if [[ ! -b "$part" ]]; then
      # No udev (containers, Docker Desktop): attach partition 2 by offset.
      losetup -d "$LOOP"
      LOOP=
      read -r p2_start p2_len < <(python3 - "$img" <<'PY'
import struct, sys
mbr = open(sys.argv[1], "rb").read(512)
if mbr[510:512] != b"\x55\xaa":
    sys.exit("no MBR")
e = mbr[446 + 16:446 + 32]
start, length = struct.unpack("<II", e[8:16])
if e[4] != 0x83 or not start or not length:
    sys.exit("partition 2 is not Linux")
print(start * 512, length * 512)
PY
) || die "cannot read partition 2 from the image"
      LOOP=$(losetup --find --show --offset "$p2_start" --sizelimit "$p2_len" "$img")
      part=$LOOP
      printf '  no partition nodes; attached partition 2 by offset\n'
    fi
    [[ -b "$part" ]] || die "partition 2 did not appear on ${LOOP}"
    MNT="${WORKDIR}/mnt"
    mkdir -p "$MNT"
    mount -o noatime "$part" "$MNT"
    if [[ ! -e "${MNT}/etc/rpi-issue" && ! -d "${MNT}/usr/lib/raspberrypi-sys-mods" ]]; then
      die "mounted filesystem is not Raspberry Pi OS; refusing to modify it"
    fi
  fi

  # --- step 8. git archive of HEAD. No .git, no untracked files, no secrets.
  step 8 "copy grok-station with git archive of HEAD (no .git, no untracked files)"
  printf '  commit %s\n' "$COMMIT"
  if [[ "$DRY" -eq 1 ]]; then
    printf '+ git -C %q -c safe.directory=%q archive --format=tar %s\n' "$REPO" "$REPO" "$COMMIT"
    printf '+ reject archive paths matching .git .env id_rsa id_ed25519 .ssh .pem credentials.json secrets.json .netrc\n'
    printf '+ tar -C <mnt>/usr/local/src/grok-station -xf -\n'
    printf '+ chown -R root:root <mnt>/usr/local/src/grok-station\n'
  else
    git -C "$REPO" -c "safe.directory=${REPO}" archive --format=tar HEAD > "${WORKDIR}/grok-station.tar"
    archive_is_clean "${WORKDIR}/grok-station.tar"
    install -d -m 0755 "${MNT}/usr/local/src/grok-station"
    tar -C "${MNT}/usr/local/src/grok-station" -xf "${WORKDIR}/grok-station.tar"
    chown -R root:root "${MNT}/usr/local/src/grok-station"
    ver=$(tr -d '[:space:]' < "${MNT}/usr/local/src/grok-station/VERSION")
    [[ "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "archived VERSION is not MAJOR.MINOR.PATCH"
    printf 'commit %s\nversion %s\n' "$COMMIT" "$ver" > "${MNT}/usr/local/src/grok-station-image-commit"
    chmod 0644 "${MNT}/usr/local/src/grok-station-image-commit"
  fi

  # --- step 9. First-boot unit, taken from the archive so it matches HEAD.
  step 9 "install grok-station-firstboot.service and the one-shot stamp"
  printf '  unit: /etc/systemd/system/grok-station-firstboot.service\n'
  printf '  stamp: /var/lib/grok-station-firstboot\n'
  printf '  enable symlink: multi-user.target.wants/grok-station-firstboot.service\n'
  if [[ "$DRY" -eq 1 ]]; then
    printf '+ install -m 0644 <archive>/tools/build-image/grok-station-firstboot.service /etc/systemd/system/\n'
    printf '+ ln -s ../grok-station-firstboot.service multi-user.target.wants/grok-station-firstboot.service\n'
    printf '+ create empty /var/lib/grok-station-firstboot\n'
  else
    unit_src="${MNT}/usr/local/src/grok-station/tools/build-image/grok-station-firstboot.service"
    [[ -f "$unit_src" ]] || die "archive has no first-boot unit"
    [[ -f "${MNT}/usr/local/src/grok-station/tools/build-image/firstboot.sh" ]] || die "archive has no firstboot.sh"
    install -m 0644 "$unit_src" "${MNT}/etc/systemd/system/grok-station-firstboot.service"
    install -d -m 0755 "${MNT}/etc/systemd/system/multi-user.target.wants"
    ln -s ../grok-station-firstboot.service \
      "${MNT}/etc/systemd/system/multi-user.target.wants/grok-station-firstboot.service"
    install -d -m 0755 "${MNT}/var/lib"
    : > "${MNT}/var/lib/grok-station-firstboot"
    chmod 0644 "${MNT}/var/lib/grok-station-firstboot"
  fi

  # --- optional Wi-Fi. Values come from the environment and are never printed.
  if [[ "$WIFI" -eq 1 ]]; then
    printf '  wifi: adding one NetworkManager profile (values not shown)\n'
    if [[ "$DRY" -eq 1 ]]; then
      printf '+ write /etc/NetworkManager/system-connections/grok-station-wifi.nmconnection (0600)\n'
      printf '+ write /var/lib/grok-station-firstboot-wifi-country (0600)\n'
    else
      write_wifi "$MNT"
    fi
  fi

  # --- step 10. Detach the loop device and write the output file.
  step 10 "write the image"
  printf '  output: %s\n' "$OUTPUT"
  if [[ "$DRY" -eq 1 ]]; then
    printf '+ sync\n'
    printf '+ umount <mnt>\n'
    printf '+ losetup -d <loop>\n'
    if [[ "$XZ" -eq 1 ]]; then
      printf '+ xz --compress --stdout <image.img> > %q\n' "$OUTPUT"
    else
      printf '+ mv <image.img> %q\n' "$OUTPUT"
    fi
    printf 'dry-run: nothing downloaded, mounted, or written\n'
    exit 0
  fi
  sync
  umount "$MNT"
  MNT=
  losetup -d "$LOOP"
  LOOP=
  if [[ "$XZ" -eq 1 ]]; then
    xz --compress --stdout "$img" > "${WORKDIR}/out.img.xz"
    mv "${WORKDIR}/out.img.xz" "$OUTPUT"
  else
    mv "$img" "$OUTPUT"
  fi
  chmod 0644 "$OUTPUT"
  if [[ -n "${SUDO_UID:-}" ]]; then
    chown "${SUDO_UID}:${SUDO_GID:-$SUDO_UID}" "$OUTPUT" || true
  fi
  printf 'wrote %s\n' "$OUTPUT"
  printf 'commit %s\n' "$COMMIT"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --xz) XZ=1; shift ;;
    --wifi-from-env) WIFI=1; shift ;;
    --output)
      [[ $# -ge 2 && "$2" != -* ]] || usage_err
      OUTPUT=$2
      shift 2
      ;;
    --select-only)
      [[ "$MODE" == build && $# -ge 2 && "$2" != -* ]] || usage_err
      MODE=select
      SELECT_ONLY=$2
      shift 2
      ;;
    --check-url)
      [[ "$MODE" == build && $# -ge 2 && "$2" != -* ]] || usage_err
      MODE=check-url
      CHECK_URL=$2
      shift 2
      ;;
    --check-image-url)
      [[ "$MODE" == build && $# -ge 2 && "$2" != -* ]] || usage_err
      MODE=check-image
      CHECK_URL=$2
      shift 2
      ;;
    --parse-sums)
      [[ "$MODE" == build && $# -ge 3 && "$2" != -* && "$3" != -* ]] || usage_err
      MODE=parse-sums
      SUMS_FILE=$2
      SUMS_NAME=$3
      shift 3
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage_err ;;
  esac
done

if [[ "$MODE" != build && ( "$DRY" -eq 1 || "$XZ" -eq 1 || -n "$OUTPUT" ) ]]; then
  usage_err
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

case "$MODE" in
  check-url)
    assert_official_url "$CHECK_URL"
    printf 'ok\n'
    ;;
  check-image)
    assert_image_url "$CHECK_URL"
    printf 'ok\n'
    ;;
  parse-sums)
    [[ -f "$SUMS_FILE" ]] || die "no such checksum file: ${SUMS_FILE}"
    parse_sums "$SUMS_FILE" "$SUMS_NAME"
    ;;
  select)
    [[ -f "$SELECT_ONLY" ]] || die "no such OS list file: ${SELECT_ONLY}"
    select_image "$SELECT_ONLY"
    ;;
  build)
    command -v git >/dev/null || die "missing command: git"
    build
    ;;
  *) usage_err ;;
esac
