# Versioning & Releases

Metixel Photoframe follows [Semantic Versioning](https://semver.org/) with
pre-release labels for beta/RC builds.  Releases are published via
[GitHub Releases](https://github.com/dennisadvani/metixel-photoframe/releases)
and discovered automatically by the OTA update system.

---

## Table of Contents

1. [Version Numbering](#version-numbering)
2. [Update Channels](#update-channels)
3. [How OTA Discovery Works](#how-ota-discovery-works)
4. [Bumping the Version](#bumping-the-version)
5. [Release Workflow](#release-workflow)
6. [Quick Reference](#quick-reference)

---

## Version Numbering

```
MAJOR.MINOR.PATCH[-PRERELEASE.N]

Examples:
  0.1.3            → stable release
  0.2.0-beta.1     → first beta of a minor release
  0.2.0-beta.2     → second beta
  0.2.0-rc.1       → release candidate
  0.2.0-rc.2       → second RC
  1.0.0-alpha.1    → alpha preview of a major release
```

| Segment | When to bump |
|---|---|
| **MAJOR** | Breaking changes, Phase 2 hardware support, API removals |
| **MINOR** | New features, new widgets, new backend capabilities |
| **PATCH** | Bug fixes, performance improvements, minor tweaks |
| **-beta.N** | Feature-complete preview, ready for wider testing |
| **-rc.N** | Final stabilisation before a stable release |
| **-alpha.N** | Early internal previews, not generally recommended |

Pre-release labels sort in this order: `alpha` < `beta` < `pre` < `rc` < (no label / stable).
So `0.2.0-beta.5` is considered newer than `0.2.0-beta.4` but older than `0.2.0-rc.1`.

---

## Update Channels

The OTA update system (`metixel.backend.update_manager`) exposes
three channels in the Web UI's **Advanced → Updates** card:

| Channel | GitHub Release Type | Git Ref | Typical Audience |
|---|---|---|---|
| **stable** | Full release (not prerelease, not draft) | Latest non-prerelease tag | Everyone |
| **beta** | **Pre-release** (tick "Set as a pre-release") | Latest prerelease tag | Testers, early adopters |
| **dev** | N/A — uses branch HEAD | `origin/dev` | Contributors only |

Users switch channels from the dashboard.  The backend polls
GitHub periodically (default every 6 hours) and caches results
for 5 minutes to stay within rate limits.

---

## How OTA Discovery Works

```
┌──────────────┐     GET /repos/{repo}/releases      ┌──────────────┐
│ UpdateManager │ ──────────────────────────────────▶ │  GitHub API  │
│  (backend)    │ ◀────────────────────────────────── │              │
└──────┬───────┘     JSON: [{tag_name, prerelease,    └──────────────┘
       │                   draft, html_url, …}, …]
       │
       │  Categorises releases:
       │    • prerelease=false, draft=false  → stable channel
       │    • prerelease=true,  draft=false  → beta channel
       │    • origin/dev HEAD                → dev channel
       │
       ▼
┌──────────────┐
│  Web UI card │  Shows available version per channel + [Install] button
└──────────────┘
```

When the user clicks **Install**, the backend:

1. Stops `metixel-cage.service` (frontend renderer)
2. Runs `git fetch --tags --force origin`
3. Runs `git reset --hard refs/tags/vX.Y.Z`
4. Runs `pip install -e .` (pick up dependency changes)
5. Restarts `metixel-backend.service` + `metixel-cage.service`

> **Important:** The `github_repo` field in `config.json` must match the
> actual GitHub repository.  The default is `dennisadvani/metixel-photoframe`.
> Users self-hosting a fork must update this value.

---

## Bumping the Version

Use the `scripts/bump_version.py` script.  It reads and writes
`src/metixel/__init__.py` only — tags and changelogs are separate steps.

### Basic bumps

```bash
python scripts/bump_version.py              # patch  (0.1.3 → 0.1.4)
python scripts/bump_version.py --minor      # minor  (0.1.3 → 0.2.0)
python scripts/bump_version.py --major      # major  (0.1.3 → 1.0.0)
```

### Pre-release labels

```bash
python scripts/bump_version.py --beta       # add/inc beta  (0.1.3 → 0.1.3-beta.1)
python scripts/bump_version.py --beta 3     # explicit beta number
python scripts/bump_version.py --rc         # add/inc rc
python scripts/bump_version.py --alpha      # add/inc alpha
python scripts/bump_version.py --release    # strip pre-release suffix
```

### Combined bumps

```bash
python scripts/bump_version.py --minor --beta 1   # 0.1.3 → 0.2.0-beta.1
python scripts/bump_version.py --major --alpha 1  # 0.1.3 → 1.0.0-alpha.1
```

### Explicit set

```bash
python scripts/bump_version.py --set 0.2.0-beta.1
```

### Dry-run (preview only, do not write)

```bash
python scripts/bump_version.py --dry-run
python scripts/bump_version.py --minor --beta 1 --dry-run
```

### Behaviour rules

| Scenario | What happens to pre-release label |
|---|---|
| Patch bump only (no other flags) | **Preserved** — `0.1.3-beta.1` → `0.1.4-beta.1` |
| `--minor` or `--major` (no pre-release flag) | **Stripped** — `0.1.3-beta.1` → `0.2.0` |
| `--beta` with no number, already `-beta.N` | **Auto-incremented** — `0.1.3-beta.1` → `0.1.4-beta.2` |
| `--beta` with no number, not yet beta | **Starts at 1** — `0.1.3` → `0.1.4-beta.1` |
| `--beta 3` (explicit number) | **Exact** — sets `-beta.3` regardless of current |
| `--rc` switching from `-beta.N` | **Resets to 1** — `0.2.0-beta.3` → `0.2.1-rc.1` |

---

## Release Workflow

### Beta Release (Pre-release)

A beta is a **pre-release** — it appears on the **beta** channel in the
Updates card.  Use this for wider testing before a stable release.

```bash
# 1. Bump the version
python scripts/bump_version.py --minor --beta 1

# 2. Update CHANGELOG.md
#    Move [Unreleased] items into a new [0.2.0-beta.1] section:
#
#    ## [0.2.0-beta.1] — 2026-08-01
#    ### Added
#    - Feature X
#    ### Fixed
#    - Bug Y

# 3. Commit the version bump + changelog
git add src/metixel/__init__.py CHANGELOG.md
git commit -m "chore: bump version to 0.2.0-beta.1"

# 4. Create an annotated tag (MUST match the version)
git tag -a v0.2.0-beta.1 -m "Beta 1: short summary of what's new"

# 5. Push
git push origin main
git push origin v0.2.0-beta.1

# 6. Create the GitHub Release at:
#    https://github.com/dennisadvani/metixel-photoframe/releases/new
#
#    • Tag:        v0.2.0-beta.1
#    • Title:      v0.2.0-beta.1 — Descriptive Summary
#    • Description: Paste the CHANGELOG.md entries for this version
#    • [x] Set as a pre-release        <- THIS IS THE KEY CHECKBOX
#    • [ ] Set as the latest release   <- leave unchecked (GitHub default)
#    • Click "Publish release"
```

### Stable Release

A stable release appears on the **stable** channel.  Do this after
betas/RCs have been tested.

```bash
# 1. If coming from a beta/rc, strip the pre-release label:
python scripts/bump_version.py --release

#    Or if you want to bump + release in one step:
python scripts/bump_version.py --minor   # strips pre-release automatically

# 2. Finalise CHANGELOG.md
#    Move [Unreleased] items into the release section, set the date.

# 3. Commit
git add src/metixel/__init__.py CHANGELOG.md
git commit -m "chore: release v0.2.0"

# 4. Tag
git tag -a v0.2.0 -m "Release v0.2.0: summary of changes"

# 5. Push
git push origin main
git push origin v0.2.0

# 6. Create the GitHub Release:
#
#    • Tag:        v0.2.0
#    • Title:      v0.2.0
#    • Description: Paste CHANGELOG.md entries
#    • [ ] Set as a pre-release          <- UNCHECKED
#    • [x] Set as the latest release     <- CHECKED
#    • Click "Publish release"
```

### Post-Release

After publishing, the `[Unreleased]` section in `CHANGELOG.md` should
be empty.  Add a new empty `[Unreleased]` section for future work:

```markdown
## [Unreleased]

### Added

### Changed

### Fixed
```

---

## Quick Reference

### Tag Naming

| Tag | Valid? | Channel |
|---|---|---|
| `v0.2.0` | Yes | stable |
| `v0.2.0-beta.1` | Yes | beta |
| `v0.2.0-beta1` | Yes | beta |
| `v0.2.0-rc.2` | Yes | beta |
| `v0.2.0-alpha.1` | Yes | beta |
| `beta-1` | No | (won't parse) |
| `v0.2.0beta1` | No | (won't parse — needs `.` or `-` separator) |

The tag name after stripping the leading `v` must match the
`__version__` string in `src/metixel/__init__.py` **exactly**.

### Config Reference

```json
{
  "update": {
    "channel": "stable",
    "auto_check": true,
    "check_interval_hours": 6,
    "github_repo": "dennisadvani/metixel-photoframe",
    "last_check": null,
    "last_update": null
  }
}
```

### Files Involved in a Release

| File | Role |
|---|---|
| `src/metixel/__init__.py` | Canonical version string (`__version__`) |
| `scripts/bump_version.py` | Bump the version programmatically |
| `CHANGELOG.md` | Human-readable release notes |
| `pyproject.toml` | Package metadata (version is dynamic, reads `__version__`) |
| `ARCHITECTURE.md` | Top-of-file version badge (update manually) |
| GitHub Release | OTA discovery source + downloadable tarball |
| Git tag (`vX.Y.Z`) | The ref the update manager checks out |
