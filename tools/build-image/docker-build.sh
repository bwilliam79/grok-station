#!/usr/bin/env bash
# Build the card image inside a Linux container, for macOS, Linux, or WSL.
# Needs Docker (Docker Desktop on macOS). The container is --privileged only
# because the build attaches the image file with losetup; it does not touch
# this computer's disks. Usage:
#   tools/build-image/docker-build.sh [OUTPUT-NAME] [--xz] [--wifi-from-env]
# The image is written next to this repository's root as OUTPUT-NAME
# (default grok-station.img). For Wi-Fi, export GS_WIFI_SSID, GS_WIFI_PSK and
# GS_WIFI_COUNTRY first; only the variable names are passed to docker.
set -euo pipefail
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
command -v docker >/dev/null 2>&1 || { echo "Docker is not installed or not running" >&2; exit 1; }
exec docker run --rm --privileged --platform linux/amd64 \
  -e GS_WIFI_SSID -e GS_WIFI_PSK -e GS_WIFI_COUNTRY \
  -v "${REPO}:/src:ro" -v "${REPO}:/out" \
  debian:trixie bash -c '
    set -e
    apt-get update -qq >/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      ca-certificates curl xz-utils git python3 util-linux mount >/dev/null
    git config --global --add safe.directory /src
    exec /src/tools/build-image/build-image.sh --output "/out/$0" "$@"
  ' "$NAME" "${ARGS[@]}"
