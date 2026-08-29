# sync-to-pi.ps1
# Efficiently syncs the metixel package (src/metixel) to a dev Pi.
# Workflow: robocopy to a local temp dir (excluding caches) -> scp to
# /tmp/metixel-sync/ on the Pi -> sudo rsync into <RemotePath>/metixel.
# The sudo rsync step is required because some installed files under
# /opt/metixel are root-owned; the pi user cannot overwrite them directly.
# (Requires passwordless sudo on the Pi — the default for the pi user.)

param(
    [string]$PiHost = "192.168.222.122",
    [string]$PiUser = "pi",
    [string]$RemotePath = "/opt/metixel/live/src/"
)

$ErrorActionPreference = "Stop"

# Resolve paths (package lives under src/ in the new layout)
$LocalMetixel = Resolve-Path "$PSScriptRoot\..\src\metixel"
$TempSync = "$env:TEMP\metixel-sync\metixel"
$RemoteTemp = "/tmp/metixel-sync"
$ScpExe = "C:\Windows\System32\OpenSSH\scp.exe"
$SshExe = "C:\Windows\System32\OpenSSH\ssh.exe"

# SSH options shared by every remote call:
#  - StrictHostKeyChecking=accept-new: no interactive host-key prompt
#  - ConnectTimeout: bounds the TCP handshake (not the whole command)
#  - ServerAliveInterval/CountMax: detect a dead connection ~60s and fail
#    instead of hanging forever on a dropped link.
$SshBase = @(
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4"
)

Write-Host "=== Syncing metixel/ to ${PiUser}@${PiHost}:${RemotePath} ===" -ForegroundColor Cyan

# Pre-flight: accept host key silently and verify SSH works
Write-Host "[0/4] Checking SSH connectivity..." -ForegroundColor Gray
$sshResult = & $SshExe @SshBase -o "ConnectTimeout=5" "${PiUser}@${PiHost}" "echo ok" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Cannot SSH to ${PiUser}@${PiHost}. Is the Pi online?" -ForegroundColor Red
    Write-Host "  Details: $sshResult" -ForegroundColor Red
    exit 1
}
Write-Host "  SSH connection OK." -ForegroundColor Gray

# Verify passwordless sudo is available (needed for the final rsync into
# the root-owned install tree).  Redirect with >$null (NOT | Out-Null, which
# can hang on native commands) so $LASTEXITCODE stays reliable.
& $SshExe @SshBase "${PiUser}@${PiHost}" "sudo -n true" >$null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Passwordless sudo is required on the Pi (sudo -n true failed)." -ForegroundColor Red
    Write-Host "  The pi user on Raspberry Pi OS has this by default. Fix with: sudo visudo" -ForegroundColor Red
    exit 1
}

# Prepare the remote staging dir (pi-writable; cleaned before each sync).
& $SshExe @SshBase "${PiUser}@${PiHost}" "rm -rf ${RemoteTemp} && mkdir -p ${RemoteTemp}" >$null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Could not prepare remote staging dir ${RemoteTemp}." -ForegroundColor Red
    exit 1
}

# Step 1: Clean previous temp sync dir
if (Test-Path $TempSync) {
    Remove-Item -Recurse -Force $TempSync -ErrorAction SilentlyContinue
}

# Step 2: Robocopy mirror to temp, excluding cache/git files
Write-Host "[1/4] Mirroring to temp (excluding caches)..." -ForegroundColor Gray
$robocopyArgs = @(
    $LocalMetixel,
    $TempSync,
    "/MIR",          # Mirror directory tree
    "/NJH",          # No job header
    "/NJS",          # No job summary
    "/NDL",          # No directory list
    "/NP",           # No progress (cleaner output)
    "/XF",           # Exclude files:
    "*.pyc",
    "*.pyo",
    "/XD",           # Exclude directories:
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    "*.egg-info",
    ".pytest_cache"
)

$robocopyResult = & robocopy @robocopyArgs
$robocopyExit = $LASTEXITCODE

# Robocopy exit codes: 0=no changes, 1=files copied, 2=extra files, 3=2+1
# Anything >= 8 is an error
if ($robocopyExit -ge 8) {
    Write-Host "ERROR: robocopy failed with exit code $robocopyExit" -ForegroundColor Red
    exit $robocopyExit
}

$filesCopied = ($robocopyResult | Select-String -Pattern "^\s*Files\s*:\s*(\d+)" | ForEach-Object { $_.Matches.Groups[1].Value }) -replace '\s',''
Write-Host "  Done. Files synced to temp: $filesCopied" -ForegroundColor Gray

# Step 3: scp to the Pi's staging dir (with host-key auto-accept)
Write-Host "[2/4] Copying to Pi staging dir..." -ForegroundColor Gray
$scpArgs = @(
    "-r",
    "-q",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
    "$TempSync",
    "${PiUser}@${PiHost}:${RemoteTemp}/"
)
& $ScpExe @scpArgs

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: scp failed with exit code $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

# Step 4: sudo rsync staging -> install dir (handles root-owned files)
# Output is streamed (not silenced) so progress is visible.  ServerAlive
# ensures a dropped connection fails rather than hanging.
Write-Host "[3/4] Installing to ${RemotePath}metixel/ (sudo rsync)..." -ForegroundColor Gray
& $SshExe @SshBase "${PiUser}@${PiHost}" "sudo rsync -a --delete ${RemoteTemp}/metixel/ ${RemotePath}metixel/"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: remote rsync failed with exit code $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

# Step 5: Cleanup local + remote temp
Remove-Item -Recurse -Force $TempSync -ErrorAction SilentlyContinue
& $SshExe @SshBase "${PiUser}@${PiHost}" "rm -rf ${RemoteTemp}" >$null 2>&1

Write-Host "[4/4] Sync complete!" -ForegroundColor Green
