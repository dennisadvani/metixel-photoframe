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
BUMP_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --dry-run)
            DRY_RUN=true
            ;;
        --finalize)
            FINALIZE="next"
            ;;
        *)
            BUMP_ARGS+=("$arg")
            ;;
    esac
done

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
    git tag -a "$TAG" -m "Release $NEW_VERSION"
    git push origin "$TAG"
    echo -e "${GREEN}Switching back to dev...${NC}"
    git checkout dev
    echo ""
    echo -e "${GREEN}═══ Release $TAG tagged on main ═══${NC}"
    echo ""
    echo "Next step: create a GitHub Release from the tag:"
    echo "  gh release create $TAG --prerelease --title \"$NEW_VERSION\" --notes \"See CHANGELOG.md\""
    echo "  (use --prerelease for beta/rc, omit for stable)"
    exit 0
fi

if [ ${#BUMP_ARGS[@]} -eq 0 ]; then
    echo -e "${RED}ERROR: No release type specified.${NC}"
    echo ""
    echo "Usage: $0 [--dry-run] <minor-beta|beta|rc|stable|minor|major>"
    echo ""
    echo "Examples:"
    echo "  $0 minor-beta        # bump number + beta (0.2.0-beta.1 → 0.2.1-beta.2)"
    echo "  $0 beta              # bump beta only (0.2.0-beta.1 → 0.2.0-beta.2)"
    echo "  $0 rc                # 0.2.0-beta.1 → 0.2.0-rc.1"
    echo "  $0 stable            # 0.2.7-beta.8 → 0.2.7 (strip pre-release)"
    echo "  $0 minor             # bump minor → 0.3.0"
    echo "  $0 major             # bump major → 1.0.0"
    echo "  $0 --finalize 0.2.0-beta.2  # tag main after PR merge"
    echo "  $0 --dry-run minor-beta  # preview only"
    exit 1
fi

# Map friendly names to bump_version.py flags
BUMP_TYPE="${BUMP_ARGS[0]}"
case "$BUMP_TYPE" in
    minor-beta) BUMP_FLAG="--beta" ;;
    beta)       BUMP_FLAG="--beta-only" ;;
    rc)         BUMP_FLAG="--rc" ;;
    stable)     BUMP_FLAG="--release" ;;
    minor)      BUMP_FLAG="--minor" ;;
    major)      BUMP_FLAG="--major" ;;
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

echo -e "${GREEN}Bumping version: ${BUMP_TYPE}${NC}"
NEW_VERSION=$(python3 "$REPO_ROOT/scripts/bump_version.py" $BUMP_FLAG 2>&1)
BUMP_EXIT=$?

if [ $BUMP_EXIT -ne 0 ]; then
    echo -e "${RED}Version bump failed:${NC}"
    echo "$NEW_VERSION"
    exit 1
fi

echo -e "${GREEN}New version: ${YELLOW}$NEW_VERSION${NC}"

if $DRY_RUN; then
    echo ""
    echo -e "${YELLOW}--- DRY RUN (no changes made) ---${NC}"
    echo "Would commit version bump on dev: v$NEW_VERSION"
    echo "Would push dev"
    echo "Would create + push release branch: release/$NEW_VERSION"
    echo "Would open PR release/$NEW_VERSION → main"
    echo "Would wait for CI checks to pass (you merge the PR yourself in GitHub)"
    echo "Would then tell you to run: --finalize $NEW_VERSION"
    # Revert the bump
    git checkout -- src/metixel/__init__.py
    exit 0
fi

# -- Commit version bump on dev ---------------------------------------------

git add src/metixel/__init__.py
git commit -m "Bump version to $NEW_VERSION"

echo -e "${GREEN}Version bump committed on dev.${NC}"

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
echo "  $0 --finalize $NEW_VERSION"
echo ""
echo "Then create a GitHub Release from the tag:"
echo "  gh release create v$NEW_VERSION --prerelease --title \"$NEW_VERSION\" --notes \"See CHANGELOG.md\""
echo "  (use --prerelease for beta/rc, omit for stable)"
