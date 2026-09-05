# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
<#
.SYNOPSIS
    Cut a new Metixel release from the dev branch.

.DESCRIPTION
    Switches to dev, pulls latest, bumps the version, commits on dev,
    pushes a release branch, opens a pull request to main, waits for CI
    checks to pass, then STOPS - you review and merge the PR yourself in
    GitHub.  Afterwards run with -Finalize <version> to tag main and push
    the tag.

    Requires the GitHub CLI (gh) installed and authenticated:
      gh auth login

    NOTE: the "main" branch ruleset requires a pull request before merging.
    This script deliberately does NOT merge the PR - you approve and merge
    it in the GitHub UI.  If the ruleset also requires an approving review,
    set required_approving_review_count to 0 (solo maintainer) or have a
    collaborator approve the PR.

.PARAMETER Type
    Release type: minor-beta (bump minor + beta), beta (beta only),
    rc, stable, minor, or major.

.PARAMETER Version
    Set an exact version string instead of deriving it from a release type
    (e.g. -Version 0.2.0-beta.2).  Mutually exclusive with Type.

.PARAMETER Finalize
    Tag main with the given version and push the tag, then RE-ALIGN dev to
    main (identical history) so the branches never drift apart (which causes
    spurious merge conflicts on the next release PR).  Run this AFTER the
    release PR has been merged in GitHub:  .\scripts\release.ps1 -Finalize <version>

.PARAMETER DryRun
    Preview what would happen without making any changes.

.EXAMPLE
    .\scripts\release.ps1 minor-beta
    .\scripts\release.ps1 -Version 0.2.0-beta.2
    .\scripts\release.ps1 stable
    .\scripts\release.ps1 minor
    .\scripts\release.ps1 -DryRun beta
#>

param(
    [Parameter(Mandatory=$false, Position=0)]
    [ValidateSet("minor-beta", "beta", "rc", "stable", "minor", "major")]
    [string]$Type,

    [Parameter(Mandatory=$false)]
    [string]$Version,

    [Parameter(Mandatory=$false)]
    [string]$Finalize,

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path "$ScriptDir\.."

# -- Validate ---------------------------------------------------------------

if (-not $Type -and -not $Version -and -not $Finalize) {
    Write-Host "ERROR: No release type, version, or -Finalize specified." -ForegroundColor Red
    Write-Host ""
    Write-Host "Usage: .\scripts\release.ps1 [-DryRun] <minor-beta|beta|rc|stable|minor|major>"
    Write-Host "       .\scripts\release.ps1 -Version <version>       (set an exact version)"
    Write-Host "       .\scripts\release.ps1 -Finalize <version>   (after the PR is merged)"
    Write-Host ""
    Write-Host "Examples:"
    Write-Host "  .\scripts\release.ps1 minor-beta # bump number + beta (1.1.9-beta.9 -> 1.1.10-beta.10)"
    Write-Host "  .\scripts\release.ps1 -Version 0.2.0-beta.2"
    Write-Host "  .\scripts\release.ps1 -Finalize 0.2.0-beta.2   (after the PR is merged)"
    Write-Host "  .\scripts\release.ps1 -DryRun beta"
    exit 1
}

# Map friendly names to bump_version.py flags
$BumpFlag = switch ($Type) {
    "minor-beta" { "--beta" }
    "beta"       { "--beta-only" }
    "rc"         { "--rc" }
    "stable"     { "--release" }
    "minor"      { "--minor" }
    "major"      { "--major" }
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

# Check clean working tree.
# Refresh the index FIRST: on Windows (core.autocrlf) `git diff-index` can
# spuriously report a clean tree as dirty right after a pull/checkout due to
# the "racy git" stat-cache problem (low-resolution filesystem timestamps).
# git update-index --refresh updates the stat cache so the check is reliable.
git update-index --refresh *> $null
git diff-index --quiet HEAD --
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Working tree is dirty. Commit or stash changes first." -ForegroundColor Red
    Write-Host "  Modified/untracked:"
    git status --porcelain
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

# -- Finalize mode (tag main after the PR has been merged in GitHub) --------

if ($Finalize) {
    $Tag = "v$Finalize"
    Write-Host "Finalizing release $Tag (tagging main)..." -ForegroundColor Green
    git checkout main
    if ($LASTEXITCODE -ne 0) { throw "Failed to switch to main" }
    git pull origin main
    if ($LASTEXITCODE -ne 0) { throw "git pull main failed" }
    git tag -a $Tag -m "Release $Finalize"
    if ($LASTEXITCODE -ne 0) { throw "git tag failed" }
    git push origin $Tag
    if ($LASTEXITCODE -ne 0) { throw "git push tag failed" }

    # -- Re-align dev to main ------------------------------------------------
    # The release PR is a squash-merge, so even though main and dev now have
    # IDENTICAL content, their commit histories differ.  Every release, this
    # divergence grows and causes spurious merge conflicts on the next release
    # PR.  Point dev at main's (just-released) commit so both branches are
    # byte-identical - the next version bump starts fresh from the release.
    Write-Host "Re-aligning dev to main (identical history)..." -ForegroundColor Green
    git checkout -B dev main
    if ($LASTEXITCODE -ne 0) { throw "Failed to land dev on main" }
    git push origin dev --force
    if ($LASTEXITCODE -ne 0) { throw "git push dev --force failed" }

    Write-Host "Switching to dev..." -ForegroundColor Green
    git checkout dev
    Write-Host ""
    Write-Host "=== Release $Tag tagged on main ===" -ForegroundColor Green
    Write-Host ""
    Write-Host "Next step: create a GitHub Release from the tag:"
    Write-Host "  gh release create $Tag --prerelease --title `"$Finalize`" --notes `"See docs/CHANGELOG.md`""
    Write-Host "  (use --prerelease for beta/rc, omit for stable)"
    exit 0
}

# -- Bump version -----------------------------------------------------------

# When given an explicit -Version, set it exactly; otherwise bump from the
# current version using the release-type flag.
if ($Version) {
    Write-Host "Setting version: $Version" -ForegroundColor Green
} else {
    Write-Host "Bumping version: $Type" -ForegroundColor Green
}

$BumpScript = Join-Path $RepoRoot "scripts\bump_version.py"
# Validate the version (dry-run) BEFORE making any changes, so an invalid
# version never leaves the tree dirty or commits anything.
$ValidateArgs = @($BumpFlag, "--dry-run")
if ($Version) { $ValidateArgs = @("--set", $Version, "--dry-run") }
$ValidateOutput = & python $BumpScript @ValidateArgs 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Version validation failed:" -ForegroundColor Red
    Write-Host ($ValidateOutput -join "`n")
    exit 1
}

# Run the real bump (writes src/metixel/__init__.py).
# Without --dry-run, bump_version.py writes the file.
$BumpArgs = if ($Version) { @("--set", $Version) } else { @($BumpFlag) }
$BumpOutput = & python $BumpScript @BumpArgs 2>&1
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
$NewVersion = if ($BumpText -match 'version:\s*(\S+)') {
    $Matches[1]
} else {
    # Fallback: take the first non-empty line
    ($BumpText -split "`n" | Where-Object { $_.Trim() -ne "" } | Select-Object -First 1).Trim()
}

# If the requested version already matches the version that is COMMITTED on
# the current branch, there's no bump commit to make - continue (skip the
# commit) rather than abort.
#
# IMPORTANT: read the current version from git (the committed __version__),
# NOT from the working-tree file.  bump_version.py has already written the
# file to $NewVersion just above, so reading the file here would ALWAYS equal
# $NewVersion and the commit below would be skipped every time - leaving the
# bump as an uncommitted change and creating the release branch/PR from stale
# dev.  git show gives the pre-bump committed value regardless.
$CurrentVersion = (git show HEAD:src/metixel/__init__.py | Select-String '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
$VersionChanged = ($NewVersion -ne $CurrentVersion)
if (-not $VersionChanged) {
    Write-Host "Version $NewVersion is already current on dev - no bump needed." -ForegroundColor Yellow
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
    Write-Host "Would wait for CI checks to pass (you merge the PR yourself in GitHub)"
    Write-Host "Would then tell you to run: -Finalize $NewVersion"
    # Revert the bump
    git checkout -- src/metixel/__init__.py
    exit 0
}

# -- Commit version bump on dev (skipped if the version was already current) --

if ($VersionChanged) {
    git add src/metixel/__init__.py
    git commit -m "Bump version to $NewVersion"
    if ($LASTEXITCODE -ne 0) { throw "git commit failed" }
    Write-Host "Version bump committed on dev." -ForegroundColor Green
} else {
    Write-Host "Version already current on dev - skipping bump commit." -ForegroundColor Cyan
}

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

# -- Done (you merge in GitHub) ----------------------------------------------

Write-Host "Switching back to dev..." -ForegroundColor Green
git checkout dev
git branch -D $ReleaseBranch 2>$null

Write-Host ""
Write-Host "=== Release $NewVersion PR ready - merge it yourself in GitHub ===" -ForegroundColor Green
Write-Host "  $PrUrl"
Write-Host ""
Write-Host "CI checks passed. I have NOT merged the PR - please review and merge it" -ForegroundColor Yellow
Write-Host "in GitHub yourself." -ForegroundColor Yellow
Write-Host ""
Write-Host "After merging, finalise the release (tags main + pushes the tag):" -ForegroundColor Green
Write-Host "  .\scripts\release.ps1 -Finalize $NewVersion"
Write-Host ""
Write-Host "Then create a GitHub Release from the tag:"
Write-Host "  gh release create v$NewVersion --prerelease --title `"$NewVersion`" --notes `"See docs/CHANGELOG.md`""
Write-Host "  (use --prerelease for beta/rc, omit for stable)"
