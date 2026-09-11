#!/usr/bin/env bash
#
# Metixel OTA — install steps.
#
# This script is invoked by scripts/update.sh AFTER the new release has been
# staged into releases/<version>. Because it lives IN the repository, it always
# reflects the NEW version being installed — a device upgrading from an older
# release therefore applies the current install logic (system packages + runtime
# pip dependencies), not the logic baked into the code that was already running.
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

# ── Require a valid Blue/Green layout ──────────────────────────────────────
# `ota_install.sh` relies on the atomic layout existing: it installs into the
# release `live` points at, and the runtime config/data tree is resolved
# relative to it.  This script used to bridge a pre-Blue/Green device by
# invoking scripts/migrate_to_atomic.sh, but that migration has been retired —
# the layout is now established by scripts/update.sh, which creates `live`
# BEFORE calling here (see the "Fresh install: establish 'live'" step), and the
# last monolithic release was 1.2.1.
#
# Fail CLOSED rather than guess.  If `live` is missing or dangling we are being
# run outside the supported flow; installing anyway would write packages
# against a path that systemd cannot resolve, and in strict mode a half-applied
# install is worse than a clean abort.  This mirrors update.sh's own
# "is not a Metixel checkout" validation below.
if ! { [ -L "${INSTALL_ROOT}/live" ] \
       && [ -d "$(readlink -f "${INSTALL_ROOT}/live" 2>/dev/null || true)" ]; }; then
    _fail "no valid ${INSTALL_ROOT}/live symlink — run scripts/update.sh (or bootstrap.sh) instead"
fi

echo "=== Metixel install steps (repo: $REPO, strict=$([ "${CONTINUE_ON_ERROR}" = yes ] && echo off || echo on)) ==="

# ── Refresh package lists ──
# Run once, up front, ONLY when something actually needs installing.  Without
# this, a device with stale lists can fail to resolve a package that has since
# been updated — and the failure would abort the whole update (strict mode).
# Skipped entirely when every requirement is already satisfied, so a normal
# upgrade does not pay for an apt update or touch the network.
_needs_apt=0
if [ -f "$REPO/requirements-system.txt" ]; then
    while IFS= read -r pkg; do
        [ -z "$pkg" ] && continue
        [[ "$pkg" =~ ^# ]] && continue
        if ! dpkg -s "$pkg" >/dev/null 2>&1; then
            _needs_apt=1
            break
        fi
    done < "$REPO/requirements-system.txt"
fi
if [ "${_needs_apt}" -eq 1 ]; then
    echo "Refreshing apt package lists…"
    # Deliberately non-fatal: a failing index refresh (one repo unreachable)
    # must not abort an update whose packages are already IN the local apt
    # cache.  The install below is the real test — if a package genuinely
    # cannot be resolved, that step fails loudly and aborts (strict mode).
    sudo -n env DEBIAN_FRONTEND=noninteractive apt-get update -qq \
        || echo "  WARNING: apt-get update failed — continuing with existing lists"
fi

# ── Install missing system packages ──
# New releases may require additional apt packages (e.g. python3-evdev).
# This is idempotent — already-installed packages are skipped.
#
# DEBIAN_FRONTEND=noninteractive is REQUIRED, not cosmetic: some packages ask
# debconf questions that BLOCK an unattended install.  `iptables-persistent`
# asks "Save current IPv4 rules?" and waits on a TUI prompt, which hangs the
# install forever with no timeout (worst on a headless first install).
# Mentioned in requirements-system.txt; keep this set for any future package.
if [ -f "$REPO/requirements-system.txt" ]; then
    echo "Checking system packages…"
    while IFS= read -r pkg; do
        [ -z "$pkg" ] && continue
        [[ "$pkg" =~ ^# ]] && continue
        if ! dpkg -s "$pkg" >/dev/null 2>&1; then
            echo "  Installing: $pkg"
            # `-o Dpkg::Options` forces existing conffiles to be kept, so a
            # package upgrade never prompts or silently replaces a config the
            # host already owns (e.g. a user's smb.conf or hostapd.conf).
            sudo -n env DEBIAN_FRONTEND=noninteractive \
                apt-get install -y -qq \
                -o Dpkg::Options::="--force-confold" \
                "$pkg" \
                || _fail "failed to install system package $pkg"
        fi
    done < "$REPO/requirements-system.txt"
fi

# ── Reinstall Python package ──
# `--ignore-installed` here too: `-e .` pulls in the same apt-provided runtime
# deps (numpy, Pillow), so the same "cannot uninstall an apt package" failure
# applies.  See the note on the requirements install below.
echo "Reinstalling Python package…"
pip install --break-system-packages --ignore-installed -e "$REPO" \
    || _fail "pip install -e failed"

# ── Install / update runtime pip dependencies ──
# `pip install -e .` above only installs the package itself — the runtime
# deps live in the phase1/phase2 optional extras, not main [project]
# dependencies — so it never applies new/changed deps (e.g. pillow-heif).
# Install the canonical requirements-pip.txt so upgrades also update deps.
#
# `--ignore-installed` is LOAD-BEARING, not a preference: several pip deps are
# ALSO provided by apt (numpy via python3-numpy, Pillow via python3-pil).  Those
# Debian packages have no RECORD file, so if a requirement ever conflicts with
# the apt version, pip tries to "uninstall" it and dies with
#   error: uninstall-no-record-file  ("The package's contents are unknown")
# which aborts the entire update.  With this flag pip leaves the apt copy alone
# and satisfies the requirement without attempting a removal.  It was present
# in the original setup script and was lost when pip moved here — restoring it.
if [ -f "$REPO/requirements-pip.txt" ]; then
    echo "Installing Python dependencies…"
    pip install --break-system-packages --ignore-installed \
        -r "$REPO/requirements-pip.txt" \
        || _fail "pip dependency install failed"
fi

# ── Install dev & testing tools (pytest, pytest-cov, ruff, mypy) ──────────
# Installed as part of the base install so no separate dev-env script is
# needed. Mirrors the [dev] extra in pyproject.toml.
# `--ignore-installed` for the same reason as above: these pull in deps that apt
# may already own, and pip must not try to uninstall a Debian package.
echo "Installing dev & testing tools…"
pip install --break-system-packages --ignore-installed ruff mypy pytest pytest-cov \
    || _fail "pip dev-tools install failed"

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
