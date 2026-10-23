# Build the card image inside a Linux container on Windows (Docker Desktop).
# Usage, from the repository root in PowerShell:
#   .\tools\build-image\docker-build.ps1 [-Name grok-station.img] [-Xz] [-WifiFromEnv]
# For Wi-Fi, set $env:GS_WIFI_SSID, $env:GS_WIFI_PSK and $env:GS_WIFI_COUNTRY
# first; only the variable names are passed to docker.
param(
  [string]$Name = "grok-station.img",
  [switch]$Xz,
  [switch]$WifiFromEnv
)
$ErrorActionPreference = "Stop"
if ($Name -match '[\\/]' -or $Name.StartsWith('..')) { throw "Name must be a plain file name" }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker Desktop is not installed or not running" }
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$extra = @()
if ($Xz) { $extra += "--xz" }
if ($WifiFromEnv) { $extra += "--wifi-from-env" }
$inner = 'set -e; apt-get update -qq >/dev/null; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends ca-certificates curl xz-utils git python3 util-linux mount >/dev/null; git config --global --add safe.directory /src; exec /src/tools/build-image/build-image.sh --output "/out/$0" "$@"'
docker run --rm --privileged --platform linux/amd64 `
  -e GS_WIFI_SSID -e GS_WIFI_PSK -e GS_WIFI_COUNTRY `
  -v "${Repo}:/src:ro" -v "${Repo}:/out" `
  debian:trixie bash -c $inner $Name @extra
exit $LASTEXITCODE
