# Install the voice relay on Windows.
# Download this file from GitHub releases over HTTPS. Never from the station.
# The xAI key is read with Read-Host -AsSecureString and stored for the
# grok-relay user only. It is never a process argument or an environment variable.
# Run it as a normal script. Do not relax the host script policy.
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$Prefix = ""
)

$ErrorActionPreference = "Stop"
$UserName = "grok-relay"
$Root = $Prefix
if ($Root) {
    $ConfigDir = Join-Path $Root "grok-relay"
    $Shim = Join-Path $Root "grok-relay.cmd"
} else {
    $ConfigDir = Join-Path $env:ProgramData "grok-relay"
    $Shim = Join-Path $env:ProgramFiles "grok-relay\grok-relay.cmd"
}

if ($DryRun) {
    Write-Output "plan: fetch manifest from GitHub releases over HTTPS (not from the station)"
    Write-Output "plan: validate manifest (not expired) with release.py"
    Write-Output "plan: create user grok-relay and add it to group docker"
    Write-Output "plan: pull images by digest from the manifest"
    Write-Output "plan: read the xAI key with Read-Host -AsSecureString and store it mode 600"
    Write-Output "plan: ask for the station address and the pairing code"
    Write-Output "plan: pair over TLS: read the certificate without verifying, then pin it"
    Write-Output "plan: print the station fingerprint and the relay fingerprint"
    Write-Output "plan: install the relay service"
    Write-Output "plan: install the updater timer (every minute)"
    Write-Output "plan: install the daily check with autocheck off"
    Write-Output ("plan: install shim " + $Shim)
    exit 0
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Run as Administrator."
    exit 1
}

New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) { $python = @("py", "-3") } else { $python = @("python") }
& $python[0] @($python[1..($python.Length - 1)] + @((Join-Path $here "install_support.py"), "fetch", (Join-Path $ConfigDir "manifest.json")))
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Get-Content -LiteralPath (Join-Path $ConfigDir "manifest.json") | Out-Null
$refs = & $python[0] @($python[1..($python.Length - 1)] + @("-c", "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8'));
[print(n+'@'+d['components']['relay']['images'][n]) for n in sorted(d['components']['relay']['images'])]", (Join-Path $ConfigDir "manifest.json")))
foreach ($ref in $refs) {
    if ($ref) { & docker pull $ref }
}

# Hidden prompt. The secure string is written to one file and then cleared.
$secure = Read-Host -AsSecureString -Prompt "xAI key"
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $keyPath = Join-Path $ConfigDir "xai_api_key"
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($keyPath, $plain + "`n", $utf8)
    icacls $keyPath /inheritance:r /grant:r "${UserName}:(R)" | Out-Null
} finally {
    if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $plain = $null
    $secure = $null
}

$stationAddr = Read-Host -Prompt "Station address (host:port)"
$stationCode = Read-Host -Prompt "Pairing code"
$pairFile = Join-Path $ConfigDir "pair-input.tmp"
try {
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($pairFile, $stationAddr + "`n" + $stationCode + "`n", $utf8)
    Get-Content -LiteralPath $pairFile | & $python[0] @($python[1..($python.Length - 1)] + @((Join-Path $here "install_support.py"), "pair", $ConfigDir))
} finally {
    $stationAddr = $null
    $stationCode = $null
    if (Test-Path -LiteralPath $pairFile) { Remove-Item -LiteralPath $pairFile -Force }
}

$settings = Join-Path $ConfigDir "update.json"
[System.IO.File]::WriteAllText($settings, "{`"autocheck`":false}`n")

$shimDir = Split-Path -Parent $Shim
if ($shimDir) { New-Item -ItemType Directory -Force -Path $shimDir | Out-Null }
$lib = Join-Path $ConfigDir "lib"
New-Item -ItemType Directory -Force -Path $lib | Out-Null
foreach ($name in @("grok_relay_cli.py", "release.py", "release_fetch.py", "host_update.py", "control.py", "pair_client.py", "pair_transport.py", "install_support.py")) {
    Copy-Item -LiteralPath (Join-Path $here $name) -Destination (Join-Path $lib $name) -Force
}
$cmd = "@echo off`r`n" + ($python -join " ") + " `"$lib\grok_relay_cli.py`" %*`r`n"
[System.IO.File]::WriteAllText($Shim, $cmd)

$update = New-ScheduledTaskAction -Execute $Shim -Argument "update-run"
$updateTrig = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration ([TimeSpan]::MaxValue)
Register-ScheduledTask -TaskName "grok-relay-update" -Action $update -Trigger $updateTrig -User $UserName -Force | Out-Null
$check = New-ScheduledTaskAction -Execute $Shim -Argument "check-update"
$checkTrig = New-ScheduledTaskTrigger -Daily -At 4am
Register-ScheduledTask -TaskName "grok-relay-check" -Action $check -Trigger $checkTrig -User $UserName -Force | Out-Null
Write-Output "installed"
