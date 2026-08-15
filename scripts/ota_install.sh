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
# Usage: bash scripts/ota_install.sh [REPO]
#   REPO   Path to the repository checkout (default: /opt/metixel)
#
# Must be idempotent — it runs on every upgrade.
set -uo pipefail

REPO="${1:-/opt/metixel}"

echo "=== Metixel install steps (repo: $REPO) ==="

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
            sudo -n apt-get install -y -qq "$pkg" 2>/dev/null \
                || echo "  WARNING: failed to install $pkg"
        fi
    done < "$REPO/requirements-system.txt"
fi

# ── Reinstall Python package ──
echo "Reinstalling Python package…"
pip install --break-system-packages -e "$REPO" \
    || echo "WARNING: pip install failed (continuing)"

# ── Install / update runtime pip dependencies ──
# `pip install -e .` above only installs the package itself — the runtime
# deps live in the phase1/phase2 optional extras, not main [project]
# dependencies — so it never applies new/changed deps (e.g. pillow-heif).
# Install the canonical requirements-pip.txt so upgrades also update deps.
if [ -f "$REPO/requirements-pip.txt" ]; then
    echo "Installing Python dependencies…"
    pip install --break-system-packages -r "$REPO/requirements-pip.txt" \
        || echo "WARNING: pip dependency install failed (continuing)"
fi

echo "=== Metixel install steps complete ==="
