# Uninstall the relay on Windows. Run from an elevated PowerShell.
# The command continues only after you type uninstall. Nothing skips that prompt.
# Secrets, socket lines, and the station address are never printed.
[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
$UserName = "grok-relay"
$Project = "grok-relay"
$Label = "label=com.docker.compose.project=grok-relay"
$ConfigDir = Join-Path $env:ProgramData "grok-relay"
$StateDir = Join-Path $ConfigDir "state"
$LogDir = Join-Path $ConfigDir "log"
$CacheDir = Join-Path $ConfigDir "cache"
$programs = $env:ProgramFiles
if (-not $programs) { $programs = "C:\Program Files" }
# Outside the config directory, so wiping config does not remove this command early.
$Shim = Join-Path $programs "grok-relay\grok-relay.cmd"
$script:StationBase = $null
$script:StationCert = $null
$script:StationUp = $false

function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-Serial8 {
    $path = Join-Path $ConfigDir "client.crt"
    if (-not (Test-Path -LiteralPath $path)) { return "00000000" }
    try {
        $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 $path
        $hex = ([string]$cert.SerialNumber).ToLower()
        if ($hex.Length -lt 8) { return $hex.PadLeft(8, "0") }
        return $hex.Substring($hex.Length - 8, 8)
    } catch {
        return "00000000"
    }
}

function Write-AuditLine([string]$Side, [string]$EventName, [string]$Detail) {
    if ($Detail -notmatch '^[!-~]{1,64}$' -and $Detail -ne "-") { return }
    if ($EventName -notmatch '^[a-z0-9-]{1,64}$') { return }
    if ($Side -ne "local" -and $Side -ne "station") { return }
    $ts = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    $line = "$ts relay $Side $EventName $Detail"
    if (-not (Test-Path -LiteralPath $LogDir)) { return }
    $name = "audit-" + [DateTime]::UtcNow.ToString("yyyy-MM") + ".log"
    try {
        Add-Content -LiteralPath (Join-Path $LogDir $name) -Value $line -Encoding ascii -ErrorAction Stop
    } catch {
        return
    }
}

function Initialize-StationClient {
    # Keep the URL and certificate in memory. The files are deleted before the done line is sent.
    $urlFile = Join-Path $ConfigDir "station.url"
    if (-not (Test-Path -LiteralPath $urlFile)) { return }
    try {
        $base = (Get-Content -LiteralPath $urlFile -Raw).Trim().TrimEnd("/")
        if ($base -notmatch '^https://[^/\s]+$') { return }
        $certPath = Join-Path $ConfigDir "client.crt"
        $script:StationCert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 $certPath
        $script:StationBase = $base
    } catch {
        $script:StationBase = $null
        $script:StationCert = $null
    }
}

function Send-Station([string]$Path, [string]$Body) {
    if (-not $script:StationBase -or -not $script:StationCert) { throw "unreachable" }
    try {
        $bytes = [Text.Encoding]::ASCII.GetBytes($Body)
        Invoke-WebRequest -Method POST -Uri ($script:StationBase + $Path) -Certificate $script:StationCert -Body $bytes -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop | Out-Null
    } catch {
        throw "unreachable"
    }
}

function Clear-DockerProject {
    $failed = $false
    try {
        & docker compose -p $Project down --volumes --rmi all | Out-Null
        if ($LASTEXITCODE -ne 0) { $failed = $true }
    } catch { $failed = $true }
    $lists = @(
        @("ps", "-aq", "--filter", $Label),
        @("images", "-q", "--filter", $Label),
        @("volume", "ls", "-q", "--filter", $Label)
    )
    $removes = @(
        @("rm", "-f"),
        @("rmi", "-f"),
        @("volume", "rm", "-f")
    )
    for ($i = 0; $i -lt $lists.Count; $i++) {
        try {
            $raw = @(& docker @($lists[$i]))
            if ($LASTEXITCODE -ne 0) { $failed = $true; continue }
            $ids = @($raw | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
            if ($ids.Count -gt 0) {
                & docker @($removes[$i] + $ids) | Out-Null
                if ($LASTEXITCODE -ne 0) { $failed = $true }
            }
        } catch { $failed = $true }
    }
    if ($failed) { throw "docker" }
}

function Remove-RelayAccount {
    foreach ($group in @("docker-users", "docker")) {
        try { Remove-LocalGroupMember -Group $group -Member $UserName -ErrorAction Stop } catch { }
    }
    try { Remove-LocalUser -Name $UserName -ErrorAction Stop } catch { }
    try { Remove-LocalGroup -Name $UserName -ErrorAction Stop } catch { }
}

function Remove-RelaySecrets {
    foreach ($name in @("client.crt", "client.key", "ca.crt", "xai_api_key")) {
        $path = Join-Path $ConfigDir $name
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
    }
    $minisign = Join-Path $ConfigDir "minisign"
    if (Test-Path -LiteralPath $minisign) {
        Remove-Item -LiteralPath $minisign -Recurse -Force -ErrorAction SilentlyContinue
    }
    # Deletes the stored target. The value is not printed.
    & cmdkey /delete:grok-relay | Out-Null
}

function Remove-RelayFiles {
    # Cache, log, and state sit under the config directory. Remove the parent last.
    foreach ($dir in @($CacheDir, $LogDir, $StateDir, $ConfigDir)) {
        if (Test-Path -LiteralPath $dir) {
            Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Remove-RelayFirewall {
    $rules = @(Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object {
        $_.DisplayName -like "grok-relay*" -or $_.Description -like "*grok-relay*"
    })
    foreach ($rule in $rules) {
        try { Remove-NetFirewallRule -Name $rule.Name -ErrorAction Stop } catch { }
    }
}

function Remove-RelayServices {
    foreach ($svc in @(Get-Service -Name "grok-relay*" -ErrorAction SilentlyContinue)) {
        try { Stop-Service -Name $svc.Name -Force -ErrorAction SilentlyContinue } catch { }
        & sc.exe delete $svc.Name | Out-Null
    }
    foreach ($task in @(Get-ScheduledTask -TaskName "grok-relay*" -ErrorAction SilentlyContinue)) {
        try { Unregister-ScheduledTask -TaskName $task.TaskName -Confirm:$false -ErrorAction Stop } catch { }
    }
    # Self last: the shim is removed after service entries.
    if (Test-Path -LiteralPath $Shim) {
        Remove-Item -LiteralPath $Shim -Force -ErrorAction SilentlyContinue
    }
    $shimDir = Split-Path -Parent $Shim
    if ($shimDir -and (Test-Path -LiteralPath $shimDir)) {
        $left = @(Get-ChildItem -LiteralPath $shimDir -Force -ErrorAction SilentlyContinue)
        if ($left.Count -eq 0) {
            Remove-Item -LiteralPath $shimDir -Force -ErrorAction SilentlyContinue
        }
    }
}

$script:Hand = @()

function Add-Hand([string]$Label, [string[]]$Items) {
    # Write-Host stays off the function output. An empty `return` would unroll into $null.
    if ($null -eq $Items -or $Items.Count -eq 0) {
        Write-Host ($Label + ": PASS")
        return
    }
    Write-Host ($Label + ": FAIL")
    foreach ($item in $Items) { $script:Hand += $item }
}

if (-not (Test-Elevated)) {
    Write-Error "Run as Administrator."
    exit 1
}

Write-Host "This will remove the relay and cannot be undone."
Write-Host "Accounts: user grok-relay, group grok-relay, docker group membership"
Write-Host "Docker project grok-relay: containers, images, and volumes (--volumes --rmi all)"
Write-Host "Credentials (values are not shown): client certificate, private key, pinned CA, xAI key, minisign public keys"
Write-Host ("Directories: " + $ConfigDir + ", " + $StateDir + ", " + $LogDir + ", " + $CacheDir)
Write-Host "Firewall: rules whose name or description is grok-relay"
Write-Host "Services: services and scheduled tasks named grok-relay"
Write-Host ("PATH shim: " + $Shim)
Write-Host "Station pairing will be revoked."
$typed = Read-Host "Type uninstall to continue"
if ($null -eq $typed -or $typed.Trim() -cne "uninstall") {
    Write-Host "aborted: confirmation did not match"
    exit 1
}

Initialize-StationClient
$serial = Get-Serial8
if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}
$startTs = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
$startLine = "$startTs relay local relay-uninstall-start $serial"
Write-AuditLine "local" "relay-uninstall-start" $serial
try {
    Send-Station "/admin/relay/audit" $startLine
    $script:StationUp = $true
} catch {
    Write-AuditLine "local" "relay-uninstall-fail" "notify"
    $script:StationUp = $false
}

try {
    Send-Station "/admin/relay/revoke" ""
    $script:StationUp = $true
} catch {
    Write-AuditLine "local" "relay-uninstall-fail" "unpair"
    $script:StationUp = $false
    Write-Host "Station not notified: remove relay from admin page"
}

$steps = @(
    @{ Name = "docker"; Action = { Clear-DockerProject } },
    @{ Name = "account"; Action = { Remove-RelayAccount } },
    @{ Name = "secrets"; Action = { Remove-RelaySecrets } },
    @{ Name = "files"; Action = { Remove-RelayFiles } },
    @{ Name = "firewall"; Action = { Remove-RelayFirewall } },
    @{ Name = "services"; Action = { Remove-RelayServices } }
)
foreach ($step in $steps) {
    try {
        & $step.Action
    } catch {
        Write-AuditLine "local" "relay-uninstall-fail" $step.Name
        Write-Host ("step " + $step.Name + " failed")
    }
}

# Done goes to the station only, and only when it answered earlier. The local log dir is already gone.
if ($script:StationUp) {
    try {
        $doneTs = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
        Send-Station "/admin/relay/audit" "$doneTs relay station relay-uninstall-done -"
    } catch {
        $script:StationUp = $false
    }
}

$filesLeft = @()
foreach ($dir in @($ConfigDir, $StateDir, $LogDir, $CacheDir)) {
    if (Test-Path -LiteralPath $dir) { $filesLeft += $dir }
}
Add-Hand "files" $filesLeft

$accountLeft = @()
try { Get-LocalUser -Name $UserName -ErrorAction Stop | Out-Null; $accountLeft += "user grok-relay" } catch { }
try { Get-LocalGroup -Name $UserName -ErrorAction Stop | Out-Null; $accountLeft += "group grok-relay" } catch { }
foreach ($groupName in @("docker-users", "docker")) {
    try {
        $members = @(Get-LocalGroupMember -Group $groupName -ErrorAction Stop | Where-Object {
            $_.Name -like "*\$UserName" -or $_.Name -eq $UserName
        })
        if ($members.Count -gt 0) { $accountLeft += "docker group membership" }
    } catch { }
}
Add-Hand "account" $accountLeft

$serviceLeft = @()
foreach ($svc in @(Get-Service -Name "grok-relay*" -ErrorAction SilentlyContinue)) { $serviceLeft += ("service " + $svc.Name) }
foreach ($task in @(Get-ScheduledTask -TaskName "grok-relay*" -ErrorAction SilentlyContinue)) { $serviceLeft += ("task " + $task.TaskName) }
Add-Hand "services" $serviceLeft

$portLeft = @()
foreach ($port in @(443, 8443)) {
    try {
        $conns = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
        $owned = $false
        foreach ($conn in $conns) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            $name = ""
            if ($proc) { $name = [string]$proc.ProcessName }
            $owner = ""
            try {
                $cim = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $conn.OwningProcess) -ErrorAction Stop
                $owner = [string]$cim.GetOwner().User
            } catch { }
            if ($owner -eq $UserName -or $name -like "*grok-relay*") { $owned = $true }
        }
        if ($owned) { $portLeft += ("port " + $port) }
    } catch {
        $portLeft += ("port " + $port + " (could not check)")
    }
}
Add-Hand "listening ports 443/8443" $portLeft

$fwLeft = @()
$rulesLeft = @(Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object {
    $_.DisplayName -like "grok-relay*" -or $_.Description -like "*grok-relay*"
})
if ($rulesLeft.Count -gt 0) { $fwLeft += "firewall rules" }
Add-Hand "firewall rules" $fwLeft

$credLeft = @()
$keyFile = Join-Path $ConfigDir "xai_api_key"
if (Test-Path -LiteralPath $keyFile) { $credLeft += $keyFile }
$listed = @(& cmdkey /list:grok-relay 2>$null)
if ($LASTEXITCODE -eq 0) { $credLeft += "credential store entry" }
Add-Hand "credential store" $credLeft

$dockerLeft = @()
foreach ($pair in @(
    @{ Kind = "container"; Args = @("ps", "-aq", "--filter", $Label) },
    @{ Kind = "image"; Args = @("images", "-q", "--filter", $Label) },
    @{ Kind = "volume"; Args = @("volume", "ls", "-q", "--filter", $Label) }
)) {
    try {
        $ids = @(& docker @($pair.Args))
        if ($LASTEXITCODE -ne 0) { $dockerLeft += ($pair.Kind + " (could not list)"); continue }
        foreach ($id in $ids) {
            if (-not [string]::IsNullOrWhiteSpace([string]$id)) { $dockerLeft += ($pair.Kind + " " + $id) }
        }
    } catch {
        $dockerLeft += ($pair.Kind + " (could not list)")
    }
}
Add-Hand "containers/images/volumes" $dockerLeft

$shimLeft = @()
if (Test-Path -LiteralPath $Shim) { $shimLeft += $Shim }
Add-Hand "PATH shim" $shimLeft

if ($script:Hand.Count -gt 0) {
    Write-Host "Remove by hand:"
    foreach ($item in $script:Hand) { Write-Host ("- " + $item) }
    exit 1
}
exit 0
