# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
<#
.SYNOPSIS
    Cut a new Metixel release from the dev branch.

.DESCRIPTION
    Switches to dev, pulls latest, bumps the version, commits on dev,
    pushes a release branch, opens a pull request to main, waits for CI
    checks, merges the PR, then tags main and pushes the tag.

    Requires the GitHub CLI (gh) installed and authenticated:
      gh auth login

    NOTE: the "main" branch ruleset requires a pull request before merging.
    If the ruleset also requires an approving review, the PR cannot be
    self-approved — set required_approving_review_count to 0 (solo
    maintainer) or have a collaborator approve the PR.

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
git diff-index --quiet HEAD --
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Working tree is dirty. Commit or stash changes first." -ForegroundColor Red
    exit 1
}

# gh is required to open/merge the release PR
$Gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $Gh) {
    Write-Host "ERROR: GitHub CLI (gh) is not installed." -ForegroundColor Red
    Write-Host "  Install: winget install --id GitHub.cli"
    Write-Host "  Auth:    gh auth login"
    exit 1
}
gh auth status *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: gh is not authenticated." -ForegroundColor Red
    Write-Host "  Run: gh auth login"
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
#     File: C:\...\src\metixel\__init__.py
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
    Write-Host "Would push dev"
    Write-Host "Would create + push release branch: release/$NewVersion"
    Write-Host "Would open PR release/$NewVersion -> main"
    Write-Host "Would wait for CI checks and merge the PR"
    Write-Host "Would tag main: v$NewVersion"
    Write-Host "Would push tag: v$NewVersion"
    # Revert the bump
    git checkout -- src/metixel/__init__.py
    exit 0
}

# -- Commit version bump on dev ---------------------------------------------

git add src/metixel/__init__.py
git commit -m "Bump version to $NewVersion"
if ($LASTEXITCODE -ne 0) { throw "git commit failed" }
Write-Host "Version bump committed on dev." -ForegroundColor Green

# -- Push dev ----------------------------------------------------------------

Write-Host "Pushing dev..." -ForegroundColor Green
git push origin dev
if ($LASTEXITCODE -ne 0) { throw "git push dev failed" }

# -- Create release branch ---------------------------------------------------

$ReleaseBranch = "release/$NewVersion"
Write-Host "Creating release branch $ReleaseBranch..." -ForegroundColor Green
git checkout -b $ReleaseBranch
if ($LASTEXITCODE -ne 0) { throw "Failed to create release branch" }

Write-Host "Pushing release branch..." -ForegroundColor Green
git push -u origin $ReleaseBranch
if ($LASTEXITCODE -ne 0) { throw "git push release branch failed" }

# -- Open pull request to main ----------------------------------------------

Write-Host "Opening pull request to main..." -ForegroundColor Green
$PrBody = @"
Release $NewVersion

Automated by scripts/release.ps1. Once CI passes, this PR merges into
main and the release tag v$NewVersion is created.
"@
$PrUrl = gh pr create --base main --head $ReleaseBranch --title "Release $NewVersion" --body $PrBody
if ($LASTEXITCODE -ne 0) { throw "gh pr create failed" }
Write-Host "PR opened: $PrUrl" -ForegroundColor Cyan

# -- Wait for CI checks ------------------------------------------------------

Write-Host "Waiting for CI checks to pass..." -ForegroundColor Green
gh pr checks $ReleaseBranch --watch --interval 15
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "ERROR: CI checks failed. Fix the issues on the PR or close it:" -ForegroundColor Red
    Write-Host "  $PrUrl"
    throw "CI checks failed"
}

# -- Merge the PR ------------------------------------------------------------

Write-Host "Merging PR into main..." -ForegroundColor Green
gh pr merge $ReleaseBranch --merge --delete-branch
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "ERROR: Could not merge the PR automatically." -ForegroundColor Red
    Write-Host "If the ruleset requires an approving review, have a collaborator" -ForegroundColor Yellow
    Write-Host "approve it (or set required_approving_review_count to 0), then merge:" -ForegroundColor Yellow
    Write-Host "  $PrUrl"
    throw "gh pr merge failed"
}

# -- Tag on main -------------------------------------------------------------

$Tag = "v$NewVersion"
Write-Host "Fetching main after merge..." -ForegroundColor Green
git checkout main
if ($LASTEXITCODE -ne 0) { throw "Failed to switch to main" }
git pull origin main
if ($LASTEXITCODE -ne 0) { throw "git pull main failed" }

Write-Host "Tagging " -ForegroundColor Green -NoNewline
Write-Host $Tag -ForegroundColor Yellow -NoNewline
Write-Host " on main..." -ForegroundColor Green
git tag -a $Tag -m "Release $NewVersion"
git push origin $Tag
if ($LASTEXITCODE -ne 0) { throw "git push tag failed" }

# -- Done -------------------------------------------------------------------

Write-Host "Switching back to dev..." -ForegroundColor Green
git checkout dev
git branch -D $ReleaseBranch 2>$null

Write-Host ""
Write-Host "=== Release $NewVersion ready ===" -ForegroundColor Green
Write-Host ""
Write-Host "Next step: create a GitHub Release from the tag:"
Write-Host "  gh release create $Tag --prerelease --title `"$NewVersion`" --notes `"See CHANGELOG.md`""
Write-Host "  (use --prerelease for beta/rc, omit for stable)"
