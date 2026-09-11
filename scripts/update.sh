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
#   3. Units:     reconcile /etc/systemd/system with the units shipped by the
#                 release (backed up first).  /etc/systemd/system is NOT part
#                 of the Blue/Green swap, so without this step an upgrade would
#                 run NEW code under OLD unit files.
#   4. Remove:    uninstall Metixel-managed packages no longer required by the
#                 new manifests (apt remove + pip uninstall).
#   5. Config:    back up the live config (and the unit files) before the swap
#                 for rollback safety.
#   6. Swap:      ln -sfn releases/<version> live  (atomic flip).
#   7. Restart + health-check: restart services, poll the health endpoint.
#                 On failure, flip live back to the previous release, restore
#                 the config AND the unit files, and restart.
#   8. Record:    update installed_packages.json to the new manifest set.
#
# Rollback (crucial): if any step before the symlink swap fails, the staging
# folder is deleted and the live system remains on the OLD (working) release.
# After the swap, a failed health-check restores both the config and units.
#
# Usage: sudo bash scripts/update.sh <version|git-ref> [REPO_URL] [--dry-run] [--staged-dir DIR]
#   <version|git-ref>   Release folder name + tag/branch/commit (e.g. v2.0.0)
#   REPO_URL            Git remote to clone from (default: origin of live repo)
#
# Flags:
#   --dry-run           Print what would happen and exit without touching the
#                       device.  Nothing is cloned, installed, swapped or
#                       restarted — safe to run anywhere to inspect the plan.
#   --staged-dir DIR    Use an ALREADY-CLONED checkout at DIR instead of
#                       cloning.  Used by scripts/bootstrap.sh, which must
#                       clone anyway (it needs this script to exist before it
#                       can run), and by local development to install a
#                       working tree without pushing.  DIR is moved into
#                       ${RELEASES_DIR}/<name>, so it must be on the same
#                       filesystem as the install root.
#
# NOTE ON CLONE DEPTH: the clone is deliberately FULL, not shallow.  The
# application relies on a real git repository on the device:
#   * update_manager._resolve_repo_root() looks for .git to locate the repo;
#   * `git reset --hard` / `git fetch` are used for in-place operations; and
#   * a shallow clone cannot be used for local debugging patches (`git push`).
# A shallow clone would silently break all three, so depth is not constrained.
set -euo pipefail

INSTALL_ROOT="${METIXEL_INSTALL_ROOT:-/opt/metixel}"
DATA_DIR="${INSTALL_ROOT}/data"
RELEASES_DIR="${INSTALL_ROOT}/releases"
LIVE_LINK="${INSTALL_ROOT}/live"
PACKAGE_STATE="${DATA_DIR}/installed_packages.json"
BACKUP_DIR="${DATA_DIR}/backups"
CONFIG_FILE="${DATA_DIR}/config.json"
LOG_FILE="${DATA_DIR}/cache/metixel-update.log"
# Backups of the systemd units replaced by this update.  Lives OUTSIDE the
# install root on purpose: /opt/metixel is recreated on a re-image/reinstall,
# whereas /etc/systemd/system survives — and it is exactly the directory whose
# units must be restorable when a rollback is needed.
UNIT_BACKUP_DIR="${METIXEL_UNIT_BACKUP_DIR:-/etc/systemd/system/.metixel-backup}"

# Health-check tuning (override via env)
HEALTH_URL="${METIXEL_HEALTH_URL:-http://127.0.0.1:8080/api/health}"
HEALTH_TIMEOUT="${METIXEL_HEALTH_TIMEOUT:-60}"   # seconds to wait for healthy boot
HEALTH_INTERVAL="${METIXEL_HEALTH_INTERVAL:-3}"  # poll interval

# safedir helper
_die() {
    echo "ERROR: $*" >&2
    exit 1
}

# Echo a command instead of running it while in dry-run mode.
_run() {
    if [ "${DRY_RUN:-no}" = "yes" ]; then
        printf '      [dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}

if [ $# -lt 1 ]; then
    echo "Usage: $0 <version|git-ref> [REPO_URL] [--dry-run] [--staged-dir DIR]" >&2
    exit 1
fi
VERSION="$1"
shift

# Parse the remaining arguments (order-independent).
REPO_URL=""
DRY_RUN="no"
STAGED_DIR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN="yes" ;;
        --staged-dir)
            [ $# -ge 2 ] || _die "--staged-dir requires a directory"
            STAGED_DIR="$2"
            shift
            ;;
        --staged-dir=*) STAGED_DIR="${1#*=}" ;;
        --*) _die "unknown flag: $1" ;;
        *)
            # First positional after the ref is the repo URL.
            [ -z "${REPO_URL}" ] || _die "unexpected argument: $1"
            REPO_URL="$1"
            ;;
    esac
    shift
 done
export DRY_RUN

# In dry-run mode nothing is written, so the root check is relaxed and the log
# tee is skipped (there may be no data dir to write it to yet).
if [ "${DRY_RUN}" = "no" ]; then
    [ "$(id -u)" -eq 0 ] || _die "Must run as root"
    # The log lives under data/cache, which does NOT exist yet on a fresh
    # device (reconcile.sh creates the data tree, but only much later in this
    # script).  Create it BEFORE the tee, or `tee` fails and the whole run
    # loses its log — including any error that caused the failure.
    mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null || true
    exec > >(tee -a "${LOG_FILE}") 2>&1
fi

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

# ── systemd unit helpers ───────────────────────────────────────────────────
# /etc/systemd/system is NOT covered by the Blue/Green symlink swap, so units
# must be reconciled explicitly on every update.  Without this an upgrade runs
# NEW code against OLD unit files (e.g. a cage unit still launching python
# directly instead of scripts/cage_launch.sh).
#
# The units themselves are installed by scripts/reconcile.sh — the single
# source of truth for host configuration, shared with fresh installs.  Only
# backup/restore live here, because they are specific to this update's rollback.

# Restore the units replaced earlier in this run (rollback path).
_restore_units() {
    [ -d "${UNIT_BACKUP_DIR}" ] || return 0
    local restored=0
    local bak
    for bak in "${UNIT_BACKUP_DIR}"/*.service; do
        [ -f "${bak}" ] || continue
        cp -a "${bak}" "/etc/systemd/system/$(basename "${bak}")"
        restored=$((restored + 1))
    done
    if [ "${restored}" -gt 0 ]; then
        echo "  Restored ${restored} systemd unit(s) from ${UNIT_BACKUP_DIR}"
    fi
}

# ── Guard: not already present ─────────────────────────────────────────────
if [ -e "${RELEASE_DIR}" ]; then
    _die "Release already exists at ${RELEASE_DIR} — aborting"
fi

echo "=== Metixel OTA Update (Blue/Green) ==="
echo "Target : ${VERSION}"
if [ "${DRY_RUN}" = "yes" ]; then
    echo "Mode   : DRY RUN (nothing will be changed)"
fi
if [ -n "${STAGED_DIR}" ]; then
    echo "Staged : ${STAGED_DIR}"
fi
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

# A fresh install has no live symlink and no releases dir.  Track it so the
# messaging and the rollback decision are honest (there is nothing to restore).
FRESH_INSTALL="no"
if [ -z "${PREV_LIVE}" ] || [ ! -d "${PREV_LIVE}" ]; then
    FRESH_INSTALL="yes"
fi

# ── Ensure the Blue/Green layout exists ────────────────────────────────
# On a fresh install /opt/metixel may be absent or empty, so there is no
# releases/ dir to stage into.  reconcile.sh creates the data tree, but that
# runs much later — staging needs releases/ NOW.  mkdir -p is idempotent.
# Skipped in dry-run: nothing is written and the root may not even be writable.
if [ "${DRY_RUN}" = "no" ]; then
    mkdir -p "${RELEASES_DIR}" "${DATA_DIR}" "${BACKUP_DIR}"
fi

# ── 1) STAGING ─────────────────────────────────────────────────────────────
echo ""
echo "[1/8] Staging ${VERSION}…"
if [ -n "${STAGED_DIR}" ]; then
    # An already-cloned checkout supplied by the caller (bootstrap.sh, or a
    # local working tree).  Validate it before trusting it.
    [ -d "${STAGED_DIR}" ] || _die "--staged-dir ${STAGED_DIR} does not exist"
    [ -f "${STAGED_DIR}/scripts/ota_install.sh" ] \
        || _die "--staged-dir ${STAGED_DIR} is not a Metixel checkout"
    if [ "${DRY_RUN}" = "yes" ]; then
        echo "  [dry-run] would move ${STAGED_DIR} → ${RELEASE_DIR}"
    else
        rm -rf "${STAGING_DIR}"
        mv "${STAGED_DIR}" "${STAGING_DIR}"
    fi
elif [ "${DRY_RUN}" = "yes" ]; then
    echo "  [dry-run] would clone ${REPO_URL} @ ${VERSION} → ${STAGING_DIR}"
else
    rm -rf "${STAGING_DIR}"
    # FULL clone on purpose — see the note in the header.  A shallow clone
    # breaks the app's .git-based update checks and local debugging patches.
    git clone --branch "${VERSION}" "${REPO_URL}" "${STAGING_DIR}"
fi
# For the dev branch there is no single tag, so name the release folder after
# the checked-out commit id. Every dev upgrade then gets its own folder (no
# overlap with a previous dev release), while stable/beta keep their tag names.
if [ "${VERSION}" = "dev" ] && [ "${DRY_RUN}" = "no" ]; then
    COMMIT="$(git -C "${STAGING_DIR}" rev-parse --short HEAD)"
    STAGING_VERSION="${COMMIT}"
    RELEASE_DIR="${RELEASES_DIR}/${STAGING_VERSION}"
    echo "  dev staging → release folder: ${STAGING_VERSION}"
fi
if [ "${DRY_RUN}" = "no" ]; then
    mv "${STAGING_DIR}" "${RELEASE_DIR}"
    git config --system --add safe.directory "${RELEASE_DIR}" 2>/dev/null || true
fi

# ── Cleanup trap: on ANY failure before swap, delete the staging release ───
# Fires on ERR (a command fails) AND on EXIT while still pre-swap, so an
# interrupted update never leaves a half-staged release behind. Cleared after
# the swap (trap - ERR / trap - EXIT) so the rollback path owns the outcome.
# Disarmed entirely in dry-run mode: nothing was created, so nothing may be
# deleted.  Guarded against running TWICE: a failing command fires the ERR trap
# and then the EXIT trap, which produced confusing duplicate output.
_CLEANUP_DONE="no"
_cleanup_staging() {
    [ "${_CLEANUP_DONE}" = "yes" ] && return 0
    _CLEANUP_DONE="yes"
    echo ""
    echo "--- Update failed — removing staging release ${RELEASE_DIR} ---"
    rm -rf "${RELEASE_DIR}"
    # On a fresh install we point `live` at the staged release BEFORE running
    # the installer (so ota_install.sh skips its legacy migration).  If we now
    # remove that release, `live` would be left DANGLING — systemd units resolve
    # /opt/metixel/live, so remove the symlink too and leave the device clean.
    if [ "${FRESH_INSTALL}" = "yes" ] && [ -L "${LIVE_LINK}" ]; then
        rm -f "${LIVE_LINK}"
        echo "Removed dangling ${LIVE_LINK} (fresh install did not complete)."
    fi
    echo "Live release (${PREV_LIVE:-none}) left untouched."
}
if [ "${DRY_RUN}" = "no" ]; then
    trap _cleanup_staging ERR EXIT
else
    echo ""
    echo "=== dry-run complete: no changes were made ==="
    echo "Would have: cloned ${VERSION}, installed packages, reconciled host"
    echo "            config, swapped ${LIVE_LINK} → ${RELEASE_DIR}, restarted +"
    echo "            health-checked services (rolling back on failure)."
    exit 0
fi

# ── Fresh install: establish 'live' BEFORE installing ──────────────────────
# ota_install.sh self-migrates whenever there is no valid `live` symlink, and
# that migration targets the OLD flat monolithic layout.  On a fresh device it
# would create an EMPTY release and re-point the install at it, breaking the
# pip step.  Pointing `live` at the staged release first makes ota_install see
# a normal Blue/Green device, so migration is correctly skipped.
#
# This is safe: no service is started until the restart step below, and
# PREV_LIVE is empty, so a later health failure reports "no previous release to
# roll back to" rather than pretending to restore one.
if [ "${FRESH_INSTALL}" = "yes" ]; then
    echo "  Fresh install — establishing 'live' at ${RELEASE_DIR}"
    ln -sfn "${RELEASE_DIR}" "${LIVE_LINK}"
    chown -h pi:pi "${LIVE_LINK}" 2>/dev/null || true
fi

# ── 2) INSTALL (strict) ────────────────────────────────────────────────────
echo "[2/8] Running install steps for ${VERSION} (system + pip)…"
# 'set -e' is active: any apt/pip failure aborts before the swap.
bash "${RELEASE_DIR}/scripts/ota_install.sh" "${RELEASE_DIR}"

# ── 3) REMOVE obsolete Metixel-managed packages ────────────────────────────
echo "[3/8] Removing obsolete managed packages…"
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
prev_sys = set(prev.get("apt", []) or [])
prev_pip = set(prev.get("pip", []) or [])
for pkg in sorted(prev_sys - new_sys):
    print(f"  removing system pkg: {pkg}")
    subprocess.run(["apt-get", "remove", "-y", "--purge", pkg],
                   check=False, capture_output=True)

for pkg in sorted(prev_pip - new_pip):
    print(f"  removing pip pkg: {pkg}")
    subprocess.run(["pip", "uninstall", "-y", pkg], check=False,
                   capture_output=True)
PYEOF

# ── 4) RECONCILE HOST CONFIGURATION (pre-swap) ─────────────────────────────
# Host state (systemd units, I²C/ddcutil, networking, Samba values, boot
# config) is NOT covered by the Blue/Green swap: the symlink flip changes the
# code, but /etc stays behind.  Without this step a release that changes a unit
# would run NEW code under OLD units indefinitely.
#
# scripts/reconcile.sh is the single source of truth for host configuration and
# is executed from THIS release, so an upgrade always applies the new release's
# definition of a correct host.  It is idempotent and only touches
# Metixel-owned files/values, so a converged device is a silent no-op.
#
# Runs BEFORE the swap so a failure aborts while the live release is still the
# old, working one.  Replaced units are backed up first so the rollback path
# (step 7) can restore them.
echo "[4/8] Reconciling host configuration…"
rm -rf "${UNIT_BACKUP_DIR}"
mkdir -p "${UNIT_BACKUP_DIR}"
if ! bash "${RELEASE_DIR}/scripts/reconcile.sh" --unit-backup-dir="${UNIT_BACKUP_DIR}"; then
    _die "host configuration reconciliation failed — refusing to swap"
fi

# ── 5) CONFIG BACKUP (pre-swap) ────────────────────────────────────────────
echo "[5/8] Backing up config before swap…"
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

# ── 6) ATOMIC SWAP ────────────────────────────────────────────────────────
echo "[6/8] Swapping live symlink → ${RELEASE_DIR}…"
ln -sfn "${RELEASE_DIR}" "${LIVE_LINK}"
chown -h pi:pi "${LIVE_LINK}" 2>/dev/null || true
# From here failures are handled by the rollback path, not the staging cleanup.
trap - ERR EXIT

# ── 7) RESTART + HEALTH-CHECK ─────────────────────────────────────────────
echo "[7/8] Restarting services…"
systemctl restart metixel-backend 2>/dev/null || true
systemctl restart metixel-cage 2>/dev/null || true
systemctl restart metixel-cursor-hider 2>/dev/null || true

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
        # Restore the units replaced in step [4/8] — otherwise the OLD release
        # would run against NEW unit files (e.g. a launcher script the old
        # release does not ship).
        _restore_units
        systemctl daemon-reload
        systemctl restart metixel-backend 2>/dev/null || true
        systemctl restart metixel-cage 2>/dev/null || true
        echo "  Rollback complete — live point to ${PREV_LIVE}"
        # Keep the failed release on disk for diagnosis (do NOT delete).
        exit 1
    else
        echo "  No previous release to roll back to — leaving as-is (may be broken)."
        exit 1
    fi
fi

# The hider parks the cursor off-screen.  It was enabled in step [4/8], but
# cage may have started before the service existed; start it and fire an
# explicit trigger so the cursor is hidden immediately rather than at the next
# reboot.  Best-effort — a failure here must not fail an otherwise healthy
# update (the service is enabled and will run on the next boot regardless).
if systemctl is-enabled --quiet metixel-cursor-hider.service 2>/dev/null; then
    systemctl start metixel-cursor-hider.service 2>/dev/null || true
    /usr/bin/env python3 "${RELEASE_DIR}/scripts/trigger_cursor_hider.py" 2>/dev/null || true
fi

# ── Fresh install only: seed the sample media ──────────────────────────────
# The repo ships a small demo gallery (data/media/sample_media, ~48 MB) so a new
# frame has something to show immediately.  Only relevant on a fresh install:
# an EXISTING device must never have it re-added, or a user who deliberately
# deleted the samples would find them back after every update.
#
# Seeded from the staged release (it travels with the clone), never
# overwritten, and best-effort — a failure must not fail a healthy install.
if [ "${FRESH_INSTALL}" = "yes" ]; then
    SAMPLE_SRC="${RELEASE_DIR}/data/media/sample_media"
    SAMPLE_DST="${DATA_DIR}/media/sample_media"
    if [ -d "${SAMPLE_SRC}" ]; then
        if [ -e "${SAMPLE_DST}" ]; then
            echo "  = sample media already present — leaving untouched"
        else
            echo "  Seeding sample media into ${SAMPLE_DST}…"
            mkdir -p "${SAMPLE_DST}"
            # -n: never overwrite; the user may have replaced these files.
            cp -rn "${SAMPLE_SRC}/." "${SAMPLE_DST}/" 2>/dev/null || true
            chown -R pi:pi "${SAMPLE_DST}" 2>/dev/null || true
            echo "  + sample media seeded ($(find "${SAMPLE_DST}" -type f 2>/dev/null | wc -l) files)"
        fi
    else
        echo "  ! no sample media shipped in this release — skipping"
    fi
fi

# ── 8) RECORD installed packages for future removal ─────────────────────────
echo "[8/8] Recording installed package manifest…"
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