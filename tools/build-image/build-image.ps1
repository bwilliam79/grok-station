# Build the card image on Windows. Windows has no native way to loop-mount an
# ext4 filesystem or run an arm64 Linux chroot, so this runs the Linux
# build-image.sh inside WSL 2 (Microsoft's built-in Linux). Untested on Windows.
#
# One-time setup, in an administrator PowerShell:
#   wsl --install -d Ubuntu
# then in the Ubuntu window:
#   sudo apt-get update && sudo apt-get install -y git curl xz-utils python3 qemu-user-static binfmt-support e2fsprogs
#
# Usage, from the repository folder in PowerShell:
#   .\tools\build-image\build-image.ps1 [-Name grok-station.img] [-Xz] [-WifiFromEnv]
# For Wi-Fi, set $env:GS_WIFI_SSID, $env:GS_WIFI_PSK and $env:GS_WIFI_COUNTRY first.
# Their values are handed to WSL through WSLENV and never printed.
param(
  [string]$Name = "grok-station.img",
  [switch]$Xz,
  [switch]$WifiFromEnv
)
$ErrorActionPreference = "Stop"
if ($Name -match '[\\/:]' -or $Name.StartsWith('..')) { throw "Name must be a plain file name" }
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { throw "WSL is not installed. Run: wsl --install -d Ubuntu" }
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$LinuxRepo = (& wsl.exe wslpath -a "$Repo").Trim()
if (-not $LinuxRepo) { throw "Could not map $Repo into WSL" }
$args2 = @("--output", "$LinuxRepo/$Name")
if ($Xz) { $args2 += "--xz" }
if ($WifiFromEnv) {
  $args2 += "--wifi-from-env"
  $env:WSLENV = "GS_WIFI_SSID/u:GS_WIFI_PSK/u:GS_WIFI_COUNTRY/u"
}
# sudo -E keeps the Wi-Fi variables; the build needs root for losetup and mount.
& wsl.exe -- sudo -E bash "$LinuxRepo/tools/build-image/build-image.sh" @args2
exit $LASTEXITCODE
