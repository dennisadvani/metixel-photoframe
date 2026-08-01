#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# release.sh — Cut a new Metixel release from the dev branch.
#
# Usage:
#   ./scripts/release.sh beta          # bump beta number (0.2.0-beta.1 → 0.2.0-beta.2)
#   ./scripts/release.sh rc            # bump rc number or create first rc
#   ./scripts/release.sh stable        # strip pre-release → stable (0.2.7-beta.8 → 0.2.7)
#   ./scripts/release.sh minor         # bump minor → stable (0.2.7 → 0.3.0)
#   ./scripts/release.sh major         # bump major → stable (0.2.7 → 1.0.0)
#   ./scripts/release.sh --dry-run beta  # show what would happen, don't do it
#
# Flow:
#   1. Switch to dev, pull latest
#   2. Bump version via bump_version.py
#   3. Commit the version bump on dev
#   4. Merge dev → main (fast-forward)
#   5. Tag on main
#   6. Push everything
#
# After running this, go to GitHub Releases and create a release from the tag.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# -- Parse args -------------------------------------------------------------

DRY_RUN=false
BUMP_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --dry-run)
            DRY_RUN=true
            ;;
        *)
            BUMP_ARGS+=("$arg")
            ;;
    esac
done

if [ ${#BUMP_ARGS[@]} -eq 0 ]; then
    echo -e "${RED}ERROR: No release type specified.${NC}"
    echo ""
    echo "Usage: $0 [--dry-run] <beta|rc|stable|minor|major>"
    echo ""
    echo "Examples:"
    echo "  $0 beta              # 0.2.0 → 0.2.0-beta.1 (or bump existing beta)"
    echo "  $0 rc                # 0.2.0-beta.1 → 0.2.0-rc.1"
    echo "  $0 stable            # 0.2.7-beta.8 → 0.2.7 (strip pre-release)"
    echo "  $0 minor             # bump minor → 0.3.0"
    echo "  $0 major             # bump major → 1.0.0"
    echo "  $0 --dry-run beta    # preview only"
    exit 1
fi

# Map friendly names to bump_version.py flags
BUMP_TYPE="${BUMP_ARGS[0]}"
case "$BUMP_TYPE" in
    beta)   BUMP_FLAG="--beta" ;;
    rc)     BUMP_FLAG="--rc" ;;
    stable) BUMP_FLAG="--release" ;;
    minor)  BUMP_FLAG="--minor" ;;
    major)  BUMP_FLAG="--major" ;;
    *)
        echo -e "${RED}ERROR: Unknown release type '$BUMP_TYPE'.${NC}"
        echo "Valid: beta, rc, stable, minor, major"
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
    echo "Would merge dev → main (fast-forward)"
    echo "Would tag: v$NEW_VERSION"
    echo "Would push: dev + main + tags"
    # Revert the bump
    git checkout -- metixel/__init__.py
    exit 0
fi

# -- Commit version bump on dev ---------------------------------------------

git add metixel/__init__.py
git commit -m "Bump version to $NEW_VERSION"

echo -e "${GREEN}Version bump committed on dev.${NC}"

# -- Merge dev → main -------------------------------------------------------

echo -e "${GREEN}Switching to main...${NC}"
git checkout main
git pull origin main

echo -e "${GREEN}Merging dev into main...${NC}"
git merge dev --ff-only -m "Release $NEW_VERSION"

# -- Tag on main ------------------------------------------------------------

TAG="v$NEW_VERSION"
echo -e "${GREEN}Tagging ${YELLOW}$TAG${GREEN} on main...${NC}"
git tag -a "$TAG" -m "Release $NEW_VERSION"

# -- Push everything --------------------------------------------------------

echo -e "${GREEN}Pushing dev, main, and tags...${NC}"
git push origin dev
git push origin main
git push origin "$TAG"

# -- Done -------------------------------------------------------------------

echo ""
echo -e "${GREEN}═══ Release $NEW_VERSION ready ═══${NC}"
echo ""
echo "Next steps:"
echo "  1. Create a GitHub Release from the tag:"
echo "     gh release create $TAG --prerelease --title \"$NEW_VERSION\" --notes \"See CHANGELOG.md\""
echo "     (use --prerelease for beta/rc, omit for stable)"
echo ""
echo "  2. Switch back to dev:"
echo "     git checkout dev"
