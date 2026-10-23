#!/usr/bin/env bash
# Build the card image on macOS. macOS cannot loop-mount ext4 or run an arm64
# Linux chroot natively, so this runs the Linux build-image.sh inside a small
# Linux VM managed by Lima (https://lima-vm.io). On Apple silicon the VM is
# arm64, so no emulation is needed. Untested on macOS.
#
# One-time setup:  brew install lima
# Usage, from the repository folder:
#   tools/build-image/build-image-macos.sh [OUTPUT-NAME] [--xz] [--wifi-from-env]
# The image is written to the repository folder. For Wi-Fi, export
# GS_WIFI_SSID, GS_WIFI_PSK and GS_WIFI_COUNTRY first; values are never printed.
set -euo pipefail
VM=grok-station-build
REPO=$(cd "$(dirname "$0")/../.." && pwd)
NAME=grok-station.img
ARGS=()
for a in "$@"; do
  case "$a" in
    --xz|--wifi-from-env) ARGS+=("$a") ;;
    -*) echo "usage: $0 [OUTPUT-NAME] [--xz] [--wifi-from-env]" >&2; exit 2 ;;
    *) NAME=$a ;;
  esac
done
case "$NAME" in */*|..*) echo "OUTPUT-NAME must be a plain file name" >&2; exit 2 ;; esac
[[ "$(uname -s)" == Darwin ]] || { echo "This script is for macOS. On Linux run build-image.sh." >&2; exit 2; }
command -v limactl >/dev/null 2>&1 || { echo "Lima is not installed: brew install lima" >&2; exit 1; }
if ! limactl list -q 2>/dev/null | grep -qx "$VM"; then
  # Debian VM with the repository folder mounted writable at the same path.
  limactl create --name="$VM" --tty=false --mount-writable --mount="$REPO" --disk=20 template://debian
fi
limactl start "$VM" >/dev/null 2>&1 || true
limactl shell "$VM" sudo apt-get update -qq
limactl shell "$VM" sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  ca-certificates curl xz-utils git python3 util-linux mount e2fsprogs qemu-user-static binfmt-support >/dev/null
# Lima mounts are not ext4, so build in the VM's own disk and copy the result out.
ENVS=()
for v in GS_WIFI_SSID GS_WIFI_PSK GS_WIFI_COUNTRY; do
  if [[ -n "${!v:-}" ]]; then ENVS+=("$v=${!v}"); fi
done
limactl shell "$VM" sudo env ${ENVS[@]+"${ENVS[@]}"} bash "$REPO/tools/build-image/build-image.sh" \
  --output "/var/tmp/$NAME" ${ARGS[@]+"${ARGS[@]}"}
limactl shell "$VM" sudo mv "/var/tmp/$NAME" "$REPO/$NAME"
echo "wrote $REPO/$NAME"
