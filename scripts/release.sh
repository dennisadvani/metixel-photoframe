#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# release.sh — Cut a new Metixel release from the dev branch.
#
# Usage:
#   ./scripts/release.sh minor-beta   # bump number + beta (0.2.0-beta.1 → 0.2.1-beta.2)
#   ./scripts/release.sh beta         # bump beta only (0.2.0-beta.1 → 0.2.0-beta.2)
#   ./scripts/release.sh rc           # bump rc number or create first rc
#   ./scripts/release.sh stable       # strip pre-release → stable (0.2.7-beta.8 → 0.2.7)
#   ./scripts/release.sh minor        # bump minor → stable (0.2.7 → 0.3.0)
#   ./scripts/release.sh major        # bump major → stable (0.2.7 → 1.0.0)
#   ./scripts/release.sh --version 1.2.7   # set an exact version instead of bumping
#   ./scripts/release.sh --finalize <version>  # tag main AFTER the PR is merged in GitHub
#   ./scripts/release.sh --dry-run minor-beta  # show what would happen, don't do it
#
# Flow:
#   1. Switch to dev, pull latest
#   2. Bump version via bump_version.py
#   3. Commit the version bump on dev
#   4. Create a release branch, push it, open a PR to main
#   5. Wait for CI checks to pass, then STOP
#   6. You merge the PR yourself in GitHub
#   7. Run --finalize <version> to tag main and push the tag
#
# Requires the GitHub CLI (gh) installed and authenticated (gh auth login).
# NOTE: this script deliberately does NOT merge the PR — you approve and
# merge it in the GitHub UI.  If the "main" ruleset also requires an
# approving review, set required_approving_review_count to 0 (solo
# maintainer) or have a collaborator approve it.
#
# After finalizing, go to GitHub Releases and create a release from the tag.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# -- Parse args -------------------------------------------------------------

DRY_RUN=false
FINALIZE=""
SET_VERSION=""
BUMP_ARGS=()

while [ $# -gt 0 ]; do
    arg="$1"
    case "$arg" in
        --dry-run)
            DRY_RUN=true
            ;;
        --finalize)
            FINALIZE="next"
            if [ $# -ge 2 ]; then
                shift
                BUMP_ARGS+=("$1")
            fi
            ;;
        --version|--set)
            if [ $# -lt 2 ]; then
                echo -e "${RED}ERROR: $arg requires a version.${NC}"
                echo "Usage: $0 --version <version>   (e.g. $0 --version 1.2.7)"
                exit 1
            fi
            shift
            SET_VERSION="$1"
            ;;
        --version=*|--set=*)
            SET_VERSION="${arg#*=}"
            ;;
        *)
            BUMP_ARGS+=("$arg")
            ;;
    esac
    shift
done

mapfile -t BUMP_ARGS < <(printf '%s\n' "${BUMP_ARGS[@]+"${BUMP_ARGS[@]}"}" | grep -v '^$' || true)

# -- Version input validation ------------------------------------------------
# Both an exact version and a release type are rejected when given together:
# one sets the version, the other derives it, so accepting both would make the
# outcome depend on argument order.
if [ -n "$SET_VERSION" ] && [ ${#BUMP_ARGS[@]} -gt 0 ]; then
    echo -e "${RED}ERROR: --version cannot be combined with a release type.${NC}"
    echo "Usage: $0 [--dry-run] --version <version>"
    exit 1
fi

if [ -n "$SET_VERSION" ]; then
    # Git tags are created as v<version>, so a leading v is tolerated; the
    # strict semver shape is validated by bump_version.py --set --dry-run.
    case "$SET_VERSION" in
        v*) SET_VERSION="${SET_VERSION#v}" ;;
    esac
fi

# -- Finalize mode (tag main after the PR has been merged in GitHub) ---------

if [ "$FINALIZE" = "next" ]; then
    if [ ${#BUMP_ARGS[@]} -ne 1 ]; then
        echo -e "${RED}ERROR: --finalize requires a version.${NC}"
        echo "Usage: $0 --finalize <version>   (e.g. $0 --finalize 0.2.0-beta.2)"
        exit 1
    fi
    NEW_VERSION="${BUMP_ARGS[0]}"
    TAG="v$NEW_VERSION"
    cd "$REPO_ROOT"
    echo -e "${GREEN}Finalizing release $TAG (tagging main)...${NC}"
    git checkout main
    git pull origin main

    MAIN_HEAD=$(git rev-parse main)

    # -- Guard: has this version already been released? ----------------------
    # If a "Release <version> (" commit is already an ancestor of main (an
    # earlier release PR was merged and this is a repeat run), refuse to tag
    # silently: the extra commits on main may not belong to this release at
    # all.  Re-tagging a published version requires a force-push.
    #
    # Anchor the pattern so it matches the squash-merge subject only:
    # --grep="Release 1.2.5" also matches "Release 1.2.5-beta.4".
    RELEASE_SUBJECT_RE="^Release ${NEW_VERSION} \\("
    mapfile -t RELEASE_COMMITS < <(git log --format="%H" -E --grep="$RELEASE_SUBJECT_RE" main)
    if [ ${#RELEASE_COMMITS[@]} -gt 1 ]; then
        EXISTING_RELEASE_COMMIT="${RELEASE_COMMITS[-1]}"
        if [ "$EXISTING_RELEASE_COMMIT" != "$MAIN_HEAD" ]; then
            echo ""
            echo -e "${YELLOW}WARNING: main already contains a release commit for $NEW_VERSION:${NC}"
            echo "  $EXISTING_RELEASE_COMMIT  $(git log -1 --format='%s' "$EXISTING_RELEASE_COMMIT")"
            echo "  main HEAD is $MAIN_HEAD  $(git log -1 --format='%s' "$MAIN_HEAD")"
            echo ""
            echo "  Commits on main since that release commit:"
            git log --oneline "$EXISTING_RELEASE_COMMIT..$MAIN_HEAD" | sed 's/^/    /'
            echo ""
            echo -e "${YELLOW}  This usually means TWO release PRs were merged for the same version,"
            echo -e "  or --finalize was run twice.  Continue only if those extra commits"
            echo -e "  genuinely belong to $NEW_VERSION.${NC}"
            echo ""
            read -r -p "Re-tag $TAG at main HEAD? (type 'yes' to confirm) " ANSWER
            if [ "$ANSWER" != "yes" ]; then
                echo -e "${YELLOW}Aborted — no tag changes made.${NC}"
                exit 1
            fi
        fi
    fi

    # -- Create or re-point the tag (idempotent) -----------------------------
    # Inspect local and remote state first so a repeat run reports what it
    # found instead of dying with a bare "tag already exists".
    LOCAL_TAG_COMMIT=$(git rev-list -n 1 "$TAG" 2>/dev/null || true)
    if [ -n "$LOCAL_TAG_COMMIT" ]; then
        if [ "$LOCAL_TAG_COMMIT" = "$MAIN_HEAD" ]; then
            echo -e "${CYAN}Local tag $TAG already points at main HEAD — nothing to re-point.${NC}"
        else
            echo -e "${YELLOW}Re-pointing local tag $TAG: $LOCAL_TAG_COMMIT -> $MAIN_HEAD${NC}"
            git tag -d "$TAG"
            git tag -a "$TAG" "$MAIN_HEAD" -m "Release $NEW_VERSION"
        fi
    else
        git tag -a "$TAG" "$MAIN_HEAD" -m "Release $NEW_VERSION"
    fi

    # A published tag can only be moved with a force-push.  Detect that rather
    # than emitting a confusing non-fast-forward error.
    REMOTE_TAG_COMMIT=$(git ls-remote --tags origin "refs/tags/$TAG^{}" 2>/dev/null | awk '{print $1}')
    if [ -z "$REMOTE_TAG_COMMIT" ]; then
        REMOTE_TAG_COMMIT=$(git ls-remote --tags origin "refs/tags/$TAG" 2>/dev/null | awk '{print $1}')
    fi

    if [ -n "$REMOTE_TAG_COMMIT" ] && [ "$REMOTE_TAG_COMMIT" != "$MAIN_HEAD" ]; then
        echo -e "${YELLOW}Remote tag $TAG already exists and points elsewhere — force-pushing.${NC}"
        git push origin "$TAG" --force
    elif [ -n "$REMOTE_TAG_COMMIT" ]; then
        echo -e "${CYAN}Remote tag $TAG already up to date.${NC}"
    else
        git push origin "$TAG"
    fi

    # -- Re-align dev to main ------------------------------------------------
    # The release PR is a squash-merge, so although main and dev now have
    # IDENTICAL content their histories differ.  That divergence grows every
    # release and causes spurious merge conflicts on the next release PR, so
    # point dev at main's just-released commit.
    echo -e "${GREEN}Re-aligning dev to main (identical history)...${NC}"
    git checkout -B dev main
    git push origin dev --force

    git checkout dev
    echo ""
    echo -e "${GREEN}═══ Release $TAG tagged on main ═══${NC}"
    echo ""
    echo "Next step: create a GitHub Release from the tag:"
    echo "  gh release create $TAG --prerelease --title \"$NEW_VERSION\" --notes \"See docs/CHANGELOG.md\""
    echo "  (use --prerelease for beta/rc, omit for stable)"
    exit 0
fi

if [ ${#BUMP_ARGS[@]} -eq 0 ] && [ -z "$SET_VERSION" ]; then
    echo -e "${RED}ERROR: No release type or version specified.${NC}"
    echo ""
    echo "Usage: $0 [--dry-run] <minor-beta|beta|rc|stable|minor|major>"
    echo "       $0 [--dry-run] --version <version>   (set an exact version)"
    echo "       $0 --finalize <version>              (after the PR is merged)"
    echo ""
    echo "Examples:"
    echo "  $0 minor-beta        # bump number + beta (0.2.0-beta.1 → 0.2.1-beta.2)"
    echo "  $0 beta              # bump beta only (0.2.0-beta.1 → 0.2.0-beta.2)"
    echo "  $0 rc                # 0.2.0-beta.1 → 0.2.0-rc.1"
    echo "  $0 stable            # 0.2.7-beta.8 → 0.2.7 (strip pre-release)"
    echo "  $0 minor             # bump minor → 0.3.0"
    echo "  $0 major             # bump major → 1.0.0"
    echo "  $0 --version 0.2.0-beta.2   # set an exact version"
    echo "  $0 --finalize 0.2.0-beta.2  # tag main after PR merge"
    echo "  $0 --dry-run minor-beta  # preview only"
    exit 1
fi

# Map friendly names to bump_version.py flags.  An exact --version bypasses the
# map entirely (the flag is only consumed when SET_VERSION is empty).
BUMP_TYPE="${BUMP_ARGS[0]:-}"
BUMP_FLAG=""
case "$BUMP_TYPE" in
    minor-beta) BUMP_FLAG="--beta" ;;
    beta)       BUMP_FLAG="--beta-only" ;;
    rc)         BUMP_FLAG="--rc" ;;
    stable)     BUMP_FLAG="--release" ;;
    minor)      BUMP_FLAG="--minor" ;;
    major)      BUMP_FLAG="--major" ;;
    "")
        : # --version was given instead of a type
        ;;
    *)
        echo -e "${RED}ERROR: Unknown release type '$BUMP_TYPE'.${NC}"
        echo "Valid: minor-beta, beta, rc, stable, minor, major"
        exit 1
        ;;
esac

# -- Pre-flight checks ------------------------------------------------------

cd "$REPO_ROOT"

# Must be on dev branch
CURRENT_BRANCH=$(git branch --show-current)
if [ "$CURRENT_BRANCH" != "dev" ]; then
    echo -e "${YELLOW}Switching to dev branch...${NC}"
    git checkout dev
fi

# Pull latest
echo -e "${GREEN}Pulling latest dev...${NC}"
git pull origin dev

# Check clean working tree
if ! git diff-index --quiet HEAD --; then
    echo -e "${RED}ERROR: Working tree is dirty. Commit or stash changes first.${NC}"
    exit 1
fi

# gh is required to open/merge the release PR
if ! command -v gh >/dev/null 2>&1; then
    echo -e "${RED}ERROR: GitHub CLI (gh) is not installed.${NC}"
    echo "  Install: sudo apt install gh   (Windows: winget install --id GitHub.cli)"
    echo "  Auth:    gh auth login"
    exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
    echo -e "${RED}ERROR: gh is not authenticated.${NC}"
    echo "  Run: gh auth login"
    exit 1
fi

# -- Bump version -----------------------------------------------------------

if [ -n "$SET_VERSION" ]; then
    echo -e "${GREEN}Setting version: ${YELLOW}${SET_VERSION}${NC}"
else
    echo -e "${GREEN}Bumping version: ${BUMP_TYPE}${NC}"
fi

# The arguments to hand bump_version.py.  An exact --version uses --set.
if [ -n "$SET_VERSION" ]; then
    BUMP_VERSION_ARGS=(--set "$SET_VERSION")
else
    BUMP_VERSION_ARGS=("$BUMP_FLAG")
fi

# Validate BEFORE writing anything, so an invalid version never leaves the tree
# dirty or creates a commit/branch from it.
set +e
VALIDATE_OUTPUT=$(python3 "$REPO_ROOT/scripts/bump_version.py" "${BUMP_VERSION_ARGS[@]}" --dry-run 2>&1)
VALIDATE_EXIT=$?
set -e

if [ $VALIDATE_EXIT -ne 0 ]; then
    echo -e "${RED}Version validation failed:${NC}"
    echo "$VALIDATE_OUTPUT"
    exit 1
fi

# bump_version.py prints TWO lines on success ("Bumped version: X" and
# "  File: ..."), so the raw output must not be used as the version.  Capture
# it with `set -e` suspended: under errexit a failing `$(...)` assignment
# aborts the script before `$?` can ever be inspected.
set +e
BUMP_OUTPUT=$(python3 "$REPO_ROOT/scripts/bump_version.py" "${BUMP_VERSION_ARGS[@]}" 2>&1)
BUMP_EXIT=$?
set -e

if [ $BUMP_EXIT -ne 0 ]; then
    echo -e "${RED}Version bump failed:${NC}"
    echo "$BUMP_OUTPUT"
    exit 1
fi

# Extract the bare semver (1.2.6 or 1.2.6-beta.1) the same way release.ps1
# does: the value after "version:" on the first matching line.
NEW_VERSION=$(printf '%s\n' "$BUMP_OUTPUT" \
    | sed -nE 's/^.*[Vv]ersion:[[:space:]]*([0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z]+\.?[0-9]+)?).*$/\1/p' \
    | head -n 1)
if [ -z "$NEW_VERSION" ]; then
    echo -e "${RED}Could not parse the new version from bump_version.py output:${NC}"
    echo "$BUMP_OUTPUT"
    git checkout -- src/metixel/__init__.py 2>/dev/null || true
    exit 1
fi

# If the requested version already matches the version COMMITTED on dev there
# is no bump commit to make — continue and skip the commit rather than aborting
# on an empty one.
#
# The committed value must come from git, NOT the working tree: bump_version.py
# has already written the file above, so reading the file would always equal
# $NEW_VERSION and the commit would be skipped every time — leaving the bump as
# an uncommitted change and building the release PR from stale dev.
CURRENT_VERSION=$(git show HEAD:src/metixel/__init__.py 2>/dev/null \
    | sed -nE 's/^__version__[[:space:]]*=[[:space:]]*"([^"]+)".*$/\1/p' \
    | head -n 1)
VERSION_CHANGED=true
if [ "$NEW_VERSION" = "$CURRENT_VERSION" ]; then
    VERSION_CHANGED=false
    echo -e "${YELLOW}Version $NEW_VERSION is already current on dev — no bump needed.${NC}"
fi

echo -e "${GREEN}New version: ${YELLOW}$NEW_VERSION${NC}"

if $DRY_RUN; then
    echo ""
    echo -e "${YELLOW}--- DRY RUN (no changes made) ---${NC}"
    if $VERSION_CHANGED; then
        echo "Would commit version bump on dev: v$NEW_VERSION"
    else
        echo "Would skip the bump commit (v$NEW_VERSION is already current on dev)"
    fi
    echo "Would push dev"
    echo "Would create + push release branch: release/$NEW_VERSION"
    echo "Would open PR release/$NEW_VERSION → main"
    echo "Would wait for CI checks to pass (you merge the PR yourself in GitHub)"
    echo "Would then tell you to run: --finalize $NEW_VERSION"
    # Revert the bump
    git checkout -- src/metixel/__init__.py
    exit 0
fi

# -- Commit version bump on dev (skipped when the version is already current) -

if $VERSION_CHANGED; then
    git add src/metixel/__init__.py
    git commit -m "Bump version to $NEW_VERSION"

    echo -e "${GREEN}Version bump committed on dev.${NC}"
else
    echo -e "${CYAN}Version already current on dev — skipping bump commit.${NC}"
fi

# -- Push dev --------------------------------------------------------------

echo -e "${GREEN}Pushing dev...${NC}"
git push origin dev

# -- Create release branch -------------------------------------------------

RELEASE_BRANCH="release/$NEW_VERSION"
echo -e "${GREEN}Creating release branch ${YELLOW}$RELEASE_BRANCH${GREEN}...${NC}"
git checkout -b "$RELEASE_BRANCH"

echo -e "${GREEN}Pushing release branch...${NC}"
git push -u origin "$RELEASE_BRANCH"

# -- Open pull request to main --------------------------------------------

echo -e "${GREEN}Opening pull request to main...${NC}"
PR_BODY=$(printf 'Release %s\n\nAutomated by scripts/release.sh. Once CI passes, this PR merges into main and the release tag v%s is created.' "$NEW_VERSION" "$NEW_VERSION")
PR_URL=$(gh pr create --base main --head "$RELEASE_BRANCH" --title "Release $NEW_VERSION" --body "$PR_BODY")
echo -e "${CYAN}PR opened: $PR_URL${NC}"

# -- Wait for CI checks ----------------------------------------------------

echo -e "${GREEN}Waiting for CI checks to pass...${NC}"
if ! gh pr checks "$RELEASE_BRANCH" --watch --interval 15; then
    echo ""
    echo -e "${RED}ERROR: CI checks failed. Fix the issues on the PR or close it:${NC}"
    echo "  $PR_URL"
    exit 1
fi

# -- Done (you merge in GitHub) ---------------------------------------------

echo -e "${GREEN}Switching back to dev...${NC}"
git checkout dev
git branch -D "$RELEASE_BRANCH" 2>/dev/null || true

echo ""
echo -e "${GREEN}═══ Release $NEW_VERSION PR ready — merge it yourself in GitHub ═══${NC}"
echo "  $PR_URL"
echo ""
echo -e "${YELLOW}CI checks passed. I have NOT merged the PR — please review and merge it"
echo "in GitHub yourself.${NC}"
echo ""
echo -e "${GREEN}After merging, finalise the release (tags main + pushes the tag):${NC}"
echo "  $0 --finalize <version>"
echo "  If you set the version with --version, use that same version:"
echo "  $0 --finalize ${NEW_VERSION}"
echo ""
echo "Then create a GitHub Release from the tag:"
echo "  gh release create v$NEW_VERSION --prerelease --title \"$NEW_VERSION\" --notes \"See docs/CHANGELOG.md\""
echo "  (use --prerelease for beta/rc, omit for stable)"
echo ""
echo "NOTE: --finalize re-aligns dev to main (force-push), so dev and main never"
echo "diverge across the squash-merged release PR."
