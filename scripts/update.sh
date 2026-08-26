#!/usr/bin/env bash
#
# Metixel Photoframe — atomic Blue/Green OTA updater.
#
# Stages a new release into /opt/metixel/releases/<version>, installs its
# system + pip packages, swaps the /opt/metixel/live symlink atomically,
# restarts services, verifies the new release boots (health-check), and
# rolls back to the previous release if it doesn't.
#
# WORKFLOW
#   1. Staging:   clone the target tag into a temp dir, then rename to
#                 releases/<version>.
#   2. Install:   install system + pip packages (NEW deps). STRICT — any
#                 failure (e.g. no internet) ABORTS here, deletes the staging
#                 dir, and leaves the live release untouched.
#   3. Remove:    uninstall Metixel-managed packages no longer required by the
#                 new manifests (apt remove + pip uninstall).
#   4. Config:    back up the live config before the swap (rollback safety).
#   5. Swap:      ln -sfn releases/<version> live  (atomic flip).
#   6. Restart + health-check: restart services, poll the health endpoint.
#                 On failure, flip live back to the previous release, restore
#                 the config, and restart.
#   7. Record:    update installed_packages.json to the new manifest set.
#
# Rollback (crucial): if any step before the symlink swap fails, the staging
# folder is deleted and the live system remains on the OLD (working) release.
#
# Usage: sudo bash scripts/update.sh <version|git-ref> [REPO_URL]
#   <version|git-ref>   Release folder name + tag/branch (e.g. v2.0.0)
#   REPO_URL            Git remote to clone from (default: origin of live repo)
set -euo pipefail

INSTALL_ROOT="${METIXEL_INSTALL_ROOT:-/opt/metixel}"
DATA_DIR="${INSTALL_ROOT}/data"
RELEASES_DIR="${INSTALL_ROOT}/releases"
LIVE_LINK="${INSTALL_ROOT}/live"
PACKAGE_STATE="${DATA_DIR}/installed_packages.json"
BACKUP_DIR="${DATA_DIR}/backups"
CONFIG_FILE="${DATA_DIR}/config.json"
LOG_FILE="${DATA_DIR}/cache/metixel-update.log"

# Health-check tuning (override via env)
HEALTH_URL="${METIXEL_HEALTH_URL:-http://127.0.0.1:8080/api/health}"
HEALTH_TIMEOUT="${METIXEL_HEALTH_TIMEOUT:-60}"   # seconds to wait for healthy boot
HEALTH_INTERVAL="${METIXEL_HEALTH_INTERVAL:-3}"  # poll interval

exec > >(tee -a "${LOG_FILE}") 2>&1

if [ $# -lt 1 ]; then
    echo "Usage: $0 <version|git-ref> [REPO_URL]" >&2
    exit 1
fi
VERSION="$1"
REPO_URL="${2:-}"

# Normalise a git ref (`refs/tags/v1.2.0`, `origin/main`, …) to a bare tag or
# branch name that `git clone --branch` accepts. Raw SHAs are passed through
# (update.sh will name the release folder after them).
_REF="${VERSION}"
case "${_REF}" in
    refs/tags/*) VERSION="${_REF#refs/tags/}" ;;
    origin/*)    VERSION="${_REF#origin/}" ;;
esac

STAGING_VERSION="${VERSION}"
RELEASE_DIR="${RELEASES_DIR}/${STAGING_VERSION}"
STAGING_DIR="${RELEASES_DIR}/.staging-${STAGING_VERSION}"

# safedir helper
_die() {
    echo "ERROR: $*" >&2
    exit 1
}

# ── Guard: root + not already present ──────────────────────────────────────
[ "$(id -u)" -eq 0 ] || _die "Must run as root"
if [ -e "${RELEASE_DIR}" ]; then
    _die "Release already exists at ${RELEASE_DIR} — aborting"
fi

echo "=== Metixel OTA Update (Blue/Green) ==="
echo "Target : ${VERSION}"
echo "Started: $(date)"
echo ""

# ── Resolve the git remote to clone from ───────────────────────────────────
if [ -z "${REPO_URL}" ] && [ -d "${RELEASES_DIR}" ]; then
    # Prefer the live release's origin remote.
    if [ -d "$(readlink -f "${LIVE_LINK}" 2>/dev/null || true)/.git" ]; then
        REPO_URL="$(git -C "$(readlink -f "${LIVE_LINK}")" config --get remote.origin.url 2>/dev/null || true)"
    fi
fi
REPO_URL="${REPO_URL:-https://github.com/dennisadvani/metixel-photoframe.git}"
echo "Repo   : ${REPO_URL}"

# ── Capture the PREVIOUS (live) release before we touch anything ───────────
PREV_LIVE=""
if [ -L "${LIVE_LINK}" ]; then
    PREV_LIVE="$(readlink -f "${LIVE_LINK}")"
fi
echo "Previous live: ${PREV_LIVE:-<none>}"

# ── 1) STAGING ─────────────────────────────────────────────────────────────
echo ""
echo "[1/7] Staging ${VERSION}…"
rm -rf "${STAGING_DIR}"
git clone --branch "${VERSION}" --depth 1 "${REPO_URL}" "${STAGING_DIR}"
mv "${STAGING_DIR}" "${RELEASE_DIR}"
git config --system --add safe.directory "${RELEASE_DIR}" 2>/dev/null || true

# ── Cleanup trap: on ANY failure before swap, delete the staging release ───
# Fires on ERR (a command fails) AND on EXIT while still pre-swap, so an
# interrupted update never leaves a half-staged release behind. Cleared after
# the swap (trap - ERR / trap - EXIT) so the rollback path owns the outcome.
_cleanup_staging() {
    echo ""
    echo "--- Update failed — removing staging release ${RELEASE_DIR} ---"
    rm -rf "${RELEASE_DIR}"
    echo "Live release (${PREV_LIVE:-none}) left untouched."
}
trap _cleanup_staging ERR EXIT

# ── 2) INSTALL (strict) ────────────────────────────────────────────────────
echo "[2/7] Running install steps for ${VERSION} (system + pip)…"
# 'set -e' is active: any apt/pip failure aborts before the swap.
bash "${RELEASE_DIR}/scripts/ota_install.sh" "${RELEASE_DIR}"

# ── 3) REMOVE obsolete Metixel-managed packages ────────────────────────────
echo "[3/7] Removing obsolete managed packages…"
APPS_SYS="${RELEASE_DIR}/requirements-system.txt"
APPS_PIP="${RELEASE_DIR}/requirements-pip.txt"
python3 - "${PACKAGE_STATE}" "${APPS_SYS}" "${APPS_PIP}" <<'PYEOF'
import json, os, sys, subprocess

state_path, req_sys, req_pip = sys.argv[1], sys.argv[2], sys.argv[3]

def names(path):
    out = []
    if not path or not os.path.isfile(path):
        return out
    for ln in open(path):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        nm = ln.split(";", 1)[0].strip().split("[", 1)[0].strip()
        for ch in "=<>~! \t":
            nm = nm.split(ch, 1)[0].strip()
        if nm:
            out.append(nm)
    return out

if not os.path.isfile(state_path):
    sys.exit(0)  # nothing recorded to remove

with open(state_path) as f:
    prev = json.load(f)

new_sys = set(names(req_sys))
new_pip = set(names(req_pip))

# Only remove packages Metixel previously recorded as installing.
for pkg in sorted(prev.get("apt", []) - new_sys):
    print(f"  removing system pkg: {pkg}")
    subprocess.run(["apt-get", "remove", "-y", "--purge", pkg],
                   check=False, capture_output=True)

for pkg in sorted(prev.get("pip", []) - new_pip):
    print(f"  removing pip pkg: {pkg}")
    subprocess.run(["pip", "uninstall", "-y", pkg], check=False,
                   capture_output=True)
PYEOF

# ── 4) CONFIG BACKUP (pre-swap) ────────────────────────────────────────────
echo "[4/7] Backing up config before swap…"
mkdir -p "${BACKUP_DIR}"
if [ -f "${CONFIG_FILE}" ]; then
    CFG_BACKUP="${BACKUP_DIR}/config-${VERSION}-$(date +%Y%m%d%H%M%S).json"
    cp "${CONFIG_FILE}" "${CFG_BACKUP}"
    echo "  config backed up to ${CFG_BACKUP}"
else
    CFG_BACKUP=""
    echo "  (no existing config to back up)"
fi
# Keep only the newest N config backups. Use a plain glob + sort (no fragile
# find|sort|tail|cut pipeline that can trip `set -e`/`pipefail` when empty).
KEEP=$(( ${METIXEL_KEEP_CONFIG_BACKUPS:-5} ))
mapfile -t OLD_BACKUPS < <(ls -1t "${BACKUP_DIR}"/config-*.json 2>/dev/null | tail -n +$((KEEP+1)))
for old in "${OLD_BACKUPS[@]:-}"; do
    [ -n "${old}" ] && rm -f -- "${old}"
done

# ── 5) ATOMIC SWAP ─────────────────────────────────────────────────────────
echo "[5/7] Swapping live symlink → ${RELEASE_DIR}…"
ln -sfn "${RELEASE_DIR}" "${LIVE_LINK}"
chown -h pi:pi "${LIVE_LINK}" 2>/dev/null || true
# From here failures are handled by the rollback path, not the staging cleanup.
trap - ERR EXIT

# ── 6) RESTART + HEALTH-CHECK ──────────────────────────────────────────────
echo "[6/7] Restarting services…"
systemctl restart metixel-backend 2>/dev/null || true
systemctl restart metixel-cage 2>/dev/null || true
systemctl restart metixel-frontend 2>/dev/null || true

echo "  Waiting up to ${HEALTH_TIMEOUT}s for health endpoint…"
elapsed=0
healthy=""
while [ "${elapsed}" -lt "${HEALTH_TIMEOUT}" ]; do
    # --fail-silent + -o /dev/null: only exit code matters.
    if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
        healthy="yes"
        break
    fi
    sleep "${HEALTH_INTERVAL}"
    elapsed=$((elapsed + HEALTH_INTERVAL))
done

if [ "${healthy}" = "yes" ]; then
    echo "  New release is healthy ✓"
else
    echo "  New release did not come up healthy after ${HEALTH_TIMEOUT}s ✗"
    if [ -n "${PREV_LIVE}" ] && [ -d "${PREV_LIVE}" ]; then
        echo "  ROLLING BACK to ${PREV_LIVE}…"
        ln -sfn "${PREV_LIVE}" "${LIVE_LINK}"
        if [ -n "${CFG_BACKUP}" ] && [ -f "${CFG_BACKUP}" ]; then
            echo "  Restoring config from ${CFG_BACKUP}"
            cp "${CFG_BACKUP}" "${CONFIG_FILE}"
        fi
        systemctl restart metixel-backend 2>/dev/null || true
        systemctl restart metixel-cage 2>/dev/null || true
        systemctl restart metixel-frontend 2>/dev/null || true
        echo "  Rollback complete — live point to ${PREV_LIVE}"
        # Keep the failed release on disk for diagnosis (do NOT delete).
        exit 1
    else
        echo "  No previous release to roll back to — leaving as-is (may be broken)."
        exit 1
    fi
fi

# ── 7) RECORD installed packages for future removal ─────────────────────────
echo "[7/7] Recording installed package manifest…"
python3 - "${PACKAGE_STATE}" "${APPS_SYS}" "${APPS_PIP}" <<'PYEOF'
import json, os, sys

state_path, req_sys, req_pip = sys.argv[1], sys.argv[2], sys.argv[3]

def names(path):
    out = []
    if not path or not os.path.isfile(path):
        return out
    for ln in open(path):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        nm = ln.split(";", 1)[0].strip().split("[", 1)[0].strip()
        for ch in "=<>~! \t":
            nm = nm.split(ch, 1)[0].strip()
        if nm:
            out.append(nm)
    return out

data = {"apt": names(req_sys), "pip": names(req_pip)}
os.makedirs(os.path.dirname(state_path), exist_ok=True)
with open(state_path, "w") as f:
    json.dump(data, f, indent=2)
print("  recorded", len(data["apt"]), "apt and", len(data["pip"]), "pip packages")
PYEOF

echo ""
echo "=== Update complete: ${VERSION} is now live. ==="
echo "End: $(date)"
exit 0