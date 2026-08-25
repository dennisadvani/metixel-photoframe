#!/usr/bin/env bash
#
# Metixel OTA — install steps.
#
# This script is invoked by the (thin) OTA bootstrap AFTER the new code has
# been checked out (git reset --hard). Because it lives IN the repository, it
# always reflects the NEW version being installed — a device upgrading from an
# older release therefore applies the current install logic (system packages +
# runtime pip dependencies), not the logic baked into the old bootstrap.
#
# Usage: bash scripts/ota_install.sh [REPO] [--continue-on-error]
#   REPO                  Path to the repository checkout (default: /opt/metixel/live)
#   --continue-on-error   If set, log failures and keep going (legacy behaviour —
#                         used for non-OTA contexts).  Otherwise ANY failure
#                         (e.g. no internet) aborts so the Blue/Green swap never
#                         happens on an incomplete install.
#
# Must be idempotent — it runs on every upgrade.
set -uo pipefail

REPO="${1:-/opt/metixel/live}"
CONTINUE_ON_ERROR="no"

# Parse optional flags (allow REPO to be omitted when only flags given).
if [ "${1:-}" = "--continue-on-error" ]; then
    CONTINUE_ON_ERROR="yes"
    REPO="/opt/metixel/live"
elif [ "${2:-}" = "--continue-on-error" ]; then
    CONTINUE_ON_ERROR="yes"
fi

if [ "${CONTINUE_ON_ERROR}" = "yes" ]; then
    # Legacy tolerant mode — log failures, keep going.
    _fail() { echo "  WARNING: $* (continuing)"; }
else
    # STRICT mode (default, used by update.sh): any failure aborts so the
    # atomic swap never runs on an incomplete install (no-internet safe).
    _fail() { echo "  ERROR: $* — aborting install" >&2; exit 1; }
fi

INSTALL_ROOT="${METIXEL_INSTALL_ROOT:-/opt/metixel}"

# ── Self-migrate from the old monolithic layout (first Blue/Green upgrade) ──
# A device still on the pre-Blue/Green layout has no /data and no live symlink.
# This is the FIRST upgrade that carries the new scripts, so we migrate in
# place BEFORE installing: the code moves into releases/<ver>, the live symlink
# is created, and the systemd units are rewritten. Only then do we install —
# so `pip install -e` targets the NEW (post-migration) repo location.
if [ ! -d "${INSTALL_ROOT}/data" ] && [ ! -L "${INSTALL_ROOT}/live" ]; then
    echo "Old monolithic layout detected (no /data, no /live) — self-migrating to Blue/Green…"
    MIG_OUT="$(bash "${INSTALL_ROOT}/scripts/migrate_to_atomic.sh" --no-restart --no-backup 2>&1)"
    MIG_RC=$?
    printf '%s\n' "$MIG_OUT"
    if [ "${MIG_RC}" -ne 0 ]; then
        _fail "auto-migration to Blue/Green layout failed"
    fi
    # Migration moved the code into releases/<ver> and created the live symlink.
    # Re-point the repo at the migrated release so the install steps run against
    # the moved code (the old flat path is now empty).
    MIG_REL="$(printf '%s\n' "$MIG_OUT" | sed -n 's/^MIGRATED_RELEASE_DIR=//p' | tail -n1)"
    if [ -n "${MIG_REL}" ] && [ -d "${MIG_REL}" ]; then
        REPO="${MIG_REL}"
    else
        REPO="${INSTALL_ROOT}/live"
    fi
fi

echo "=== Metixel install steps (repo: $REPO, strict=$([ "${CONTINUE_ON_ERROR}" = yes ] && echo off || echo on)) ==="

# ── Install missing system packages ──
# New releases may require additional apt packages (e.g. python3-evdev).
# This is idempotent — already-installed packages are skipped.
if [ -f "$REPO/requirements-system.txt" ]; then
    echo "Checking system packages…"
    while IFS= read -r pkg; do
        [ -z "$pkg" ] && continue
        [[ "$pkg" =~ ^# ]] && continue
        if ! dpkg -s "$pkg" >/dev/null 2>&1; then
            echo "  Installing: $pkg"
            sudo -n apt-get install -y -qq "$pkg" \
                || _fail "failed to install system package $pkg"
        fi
    done < "$REPO/requirements-system.txt"
fi

# ── Reinstall Python package ──
echo "Reinstalling Python package…"
pip install --break-system-packages -e "$REPO" \
    || _fail "pip install -e failed"

# ── Install / update runtime pip dependencies ──
# `pip install -e .` above only installs the package itself — the runtime
# deps live in the phase1/phase2 optional extras, not main [project]
# dependencies — so it never applies new/changed deps (e.g. pillow-heif).
# Install the canonical requirements-pip.txt so upgrades also update deps.
if [ -f "$REPO/requirements-pip.txt" ]; then
    echo "Installing Python dependencies…"
    pip install --break-system-packages -r "$REPO/requirements-pip.txt" \
        || _fail "pip dependency install failed"
fi

echo "=== Metixel install steps complete ==="
