# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
<#
.SYNOPSIS
    Cut a new Metixel release from the dev branch.

.DESCRIPTION
    Switches to dev, pulls latest, bumps the version, commits on dev,
    merges dev into main (fast-forward), tags on main, and pushes everything.

.PARAMETER Type
    Release type: beta, rc, stable, minor, or major.

.PARAMETER DryRun
    Preview what would happen without making any changes.

.EXAMPLE
    .\scripts\release.ps1 beta
    .\scripts\release.ps1 stable
    .\scripts\release.ps1 minor
    .\scripts\release.ps1 -DryRun beta
#>

param(
    [Parameter(Mandatory=$false, Position=0)]
    [ValidateSet("beta", "rc", "stable", "minor", "major")]
    [string]$Type,

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path "$ScriptDir\.."

# -- Validate ---------------------------------------------------------------

if (-not $Type) {
    Write-Host "ERROR: No release type specified." -ForegroundColor Red
    Write-Host ""
    Write-Host "Usage: .\scripts\release.ps1 [-DryRun] <beta|rc|stable|minor|major>"
    Write-Host ""
    Write-Host "Examples:"
    Write-Host "  .\scripts\release.ps1 beta       # bump beta number"
    Write-Host "  .\scripts\release.ps1 rc         # bump rc number"
    Write-Host "  .\scripts\release.ps1 stable     # strip pre-release -> stable"
    Write-Host "  .\scripts\release.ps1 minor      # bump minor -> 0.3.0"
    Write-Host "  .\scripts\release.ps1 major      # bump major -> 1.0.0"
    Write-Host "  .\scripts\release.ps1 -DryRun beta"
    exit 1
}

# Map friendly names to bump_version.py flags
$BumpFlag = switch ($Type) {
    "beta"   { "--beta" }
    "rc"     { "--rc" }
    "stable" { "--release" }
    "minor"  { "--minor" }
    "major"  { "--major" }
}

# -- Pre-flight checks ------------------------------------------------------

Set-Location $RepoRoot

# Must be on dev branch
$CurrentBranch = git branch --show-current
if ($CurrentBranch -ne "dev") {
    Write-Host "Switching to dev branch..." -ForegroundColor Yellow
    git checkout dev
    if ($LASTEXITCODE -ne 0) { throw "Failed to switch to dev" }
}

# Pull latest
Write-Host "Pulling latest dev..." -ForegroundColor Green
git pull origin dev
if ($LASTEXITCODE -ne 0) { throw "git pull failed" }

# Check clean working tree
$Dirty = git diff-index --quiet HEAD --
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Working tree is dirty. Commit or stash changes first." -ForegroundColor Red
    exit 1
}

# -- Bump version -----------------------------------------------------------

Write-Host "Bumping version: $Type" -ForegroundColor Green
$BumpScript = Join-Path $RepoRoot "scripts\bump_version.py"
$BumpOutput = & python $BumpScript $BumpFlag 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Version bump failed:" -ForegroundColor Red
    Write-Host ($BumpOutput -join "`n")
    exit 1
}

# bump_version.py prints a multi-line success message like:
#   Bumped version: 0.2.8-beta.9
#     File: C:\...\metixel\__init__.py
# PowerShell may capture this as a string array; join first, then
# extract just the version number from the first line.
$BumpText = if ($BumpOutput -is [array]) { $BumpOutput -join "`n" } else { "$BumpOutput" }
$NewVersion = if ($BumpText -match 'Bumped version:\s*(\S+)') {
    $Matches[1]
} else {
    # Fallback: take the first non-empty line
    ($BumpText -split "`n" | Where-Object { $_.Trim() -ne "" } | Select-Object -First 1).Trim()
}

Write-Host "New version: " -ForegroundColor Green -NoNewline
Write-Host $NewVersion -ForegroundColor Yellow

if ($DryRun) {
    Write-Host ""
    Write-Host "--- DRY RUN (no changes made) ---" -ForegroundColor Yellow
    Write-Host "Would commit version bump on dev: v$NewVersion"
    Write-Host "Would merge dev -> main (fast-forward)"
    Write-Host "Would tag: v$NewVersion"
    Write-Host "Would push: dev + main + tags"
    # Revert the bump
    git checkout -- metixel/__init__.py
    exit 0
}

# -- Commit version bump on dev ---------------------------------------------

git add metixel/__init__.py
git commit -m "Bump version to $NewVersion"
if ($LASTEXITCODE -ne 0) { throw "git commit failed" }
Write-Host "Version bump committed on dev." -ForegroundColor Green

# -- Merge dev -> main -------------------------------------------------------

Write-Host "Switching to main..." -ForegroundColor Green
git checkout main
git pull origin main

Write-Host "Merging dev into main..." -ForegroundColor Green
git merge dev --no-ff -m "Release $NewVersion"
if ($LASTEXITCODE -ne 0) { throw "git merge failed" }

# -- Tag on main ------------------------------------------------------------

$Tag = "v$NewVersion"
Write-Host "Tagging " -ForegroundColor Green -NoNewline
Write-Host $Tag -ForegroundColor Yellow -NoNewline
Write-Host " on main..." -ForegroundColor Green
git tag -a $Tag -m "Release $NewVersion"

# -- Push everything --------------------------------------------------------

Write-Host "Pushing dev, main, and tags..." -ForegroundColor Green
git push origin dev
git push origin main
git push origin $Tag

# -- Done -------------------------------------------------------------------

Write-Host "Switching back to dev..." -ForegroundColor Green
git checkout dev

Write-Host ""
Write-Host "=== Release $NewVersion ready ===" -ForegroundColor Green
Write-Host ""
Write-Host "Next step: create a GitHub Release from the tag:"
Write-Host "  gh release create $Tag --prerelease --title `"$NewVersion`" --notes `"See CHANGELOG.md`""
Write-Host "  (use --prerelease for beta/rc, omit for stable)"
