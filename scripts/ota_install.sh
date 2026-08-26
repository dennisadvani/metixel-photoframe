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
# A device still on the pre-Blue/Green layout has no working live symlink.
# This is the FIRST upgrade that carries the new scripts, so we migrate in
# place BEFORE installing: the code moves into releases/<ver>, the live symlink
# is created, and the systemd units are rewritten. Only then do we install —
# so `pip install -e` targets the NEW (post-migration) repo location.
#
# Detection matches migrate_to_atomic.sh's guard: migrate only if there is no
# VALID live symlink. Both a clean monolithic install AND a partial/aborted
# migration (data/ present but no live) are bridged here.
ALREADY_LIVE="no"
if [ -L "${INSTALL_ROOT}/live" ] && [ -d "$(readlink -f "${INSTALL_ROOT}/live" 2>/dev/null || true)" ]; then
    ALREADY_LIVE="yes"
fi
if [ "${ALREADY_LIVE}" = "no" ]; then
    echo "No valid live symlink — self-migrating to Blue/Green…"
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

# ── Run versioned device fixups (exactly once per device) ──────────────────
# Fixups repair device-level issues that aren't packages or config files
# (e.g. gpu_mem in /boot/firmware/config.txt). They are warn-and-continue:
# a failure is logged but does NOT abort the update. Each runs once, tracked
# in data/installed_fixups.json. See scripts/fixups/README.md.
FIXUP_MANIFEST="$REPO/scripts/fixups/manifest.txt"
FIXUP_STATE="${INSTALL_ROOT}/data/installed_fixups.json"
if [ -f "$FIXUP_MANIFEST" ]; then
    echo "Running device fixups…"
    # Load the set of already-applied fixups.
    DONE=""
    if [ -f "$FIXUP_STATE" ]; then
        DONE="$(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))))' "$FIXUP_STATE" 2>/dev/null || true)"
    fi
    while IFS= read -r fixup; do
        [ -z "$fixup" ] && continue
        [[ "$fixup" =~ ^# ]] && continue
        FIXUP_SCRIPT="$REPO/scripts/fixups/$fixup"
        [ -f "$FIXUP_SCRIPT" ] || continue
        if printf '%s\n' "$DONE" | grep -qx "$fixup"; then
            echo "  fixup already applied: $fixup"
            continue
        fi
        echo "  applying fixup: $fixup"
        if bash "$FIXUP_SCRIPT"; then
            # Record as applied (append to the JSON list).
            python3 -c '
import json, os, sys
p = sys.argv[1]; name = sys.argv[2]
data = []
if os.path.isfile(p):
    try: data = json.load(open(p))
    except Exception: data = []
if name not in data:
    data.append(name)
os.makedirs(os.path.dirname(p), exist_ok=True)
json.dump(data, open(p, "w"), indent=2)
' "$FIXUP_STATE" "$fixup"
        else
            echo "  WARNING: fixup $fixup failed (continuing)"
        fi
    done < "$FIXUP_MANIFEST"
fi

echo "=== Metixel install steps complete ==="
