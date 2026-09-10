#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# Metixel Photoframe — one-line bootstrap installer.
#
# This is the ONLY Metixel script intended to be downloaded and run directly.
# It is deliberately tiny and stable so it almost never needs changing — and so
# it never needs to be "promoted to main" before you can test an installer
# change.  Everything that evolves lives in the checkout it clones:
#
#   bootstrap.sh   (this file)  install git → clone → delegate
#   scripts/update.sh           the ONE orchestrator: stage → install →
#                               reconcile → swap → health-check → rollback
#   scripts/ota_install.sh      packages
#   scripts/reconcile.sh        host configuration
#
# Because it delegates to update.sh, a fresh install and an OTA update follow
# the SAME code path — so installing exercises staging, health-checking and
# rollback, and the install path cannot drift from the update path.
#
# USAGE (on a fresh Raspberry Pi OS Lite / Trixie image)
#
#   curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/main/scripts/bootstrap.sh | sudo bash
#
#   # or, with answers supplied up front (no prompts):
#   curl -fsSL .../bootstrap.sh | sudo bash -s -- --channel stable --wifi-country AU
#
#   # or, from a local checkout (development / testing without pushing):
#   sudo bash scripts/bootstrap.sh --local /path/to/checkout
#
# Options:
#   --channel stable|beta|dev   Release channel (default: prompt, else stable)
#   --wifi-country XX           ISO country code for the radio (default: prompt)
#   --repo URL                  Override the git remote
#   --local DIR                 Install from a local checkout instead of
#                               cloning (development; nothing is downloaded)
#   --dry-run                   Print the plan and exit without changing anything
#   --skip-boot-config          Do not run configure_boot.sh (no reboot needed)
#   -h, --help                  Show this help
#
# NOTE: the device is left with NO git checkout at the install root.  The code
# lives in releases/<version> and the root holds only data/, releases/, live and
# run/.  Future updates go through update.sh (the web UI or CLI), not git pull.

set -euo pipefail

DEFAULT_REPO="https://github.com/dennisadvani/metixel-photoframe.git"
INSTALL_ROOT="${METIXEL_INSTALL_ROOT:-/opt/metixel}"
DATA_DIR="${INSTALL_ROOT}/data"

CHANNEL=""
WIFI_COUNTRY=""
REPO_URL="${METIXEL_REPO_URL:-${DEFAULT_REPO}}"
LOCAL_DIR=""
DRY_RUN="no"
SKIP_BOOT_CONFIG="no"

usage() {
    sed -n '2,45p' "$0"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --channel)       [ $# -ge 2 ] || { echo "ERROR: --channel needs a value" >&2; exit 1; }
                         CHANNEL="$2"; shift ;;
        --channel=*)     CHANNEL="${1#*=}" ;;
        --wifi-country)  [ $# -ge 2 ] || { echo "ERROR: --wifi-country needs a value" >&2; exit 1; }
                         WIFI_COUNTRY="$2"; shift ;;
        --wifi-country=*) WIFI_COUNTRY="${1#*=}" ;;
        --repo)          [ $# -ge 2 ] || { echo "ERROR: --repo needs a value" >&2; exit 1; }
                         REPO_URL="$2"; shift ;;
        --repo=*)        REPO_URL="${1#*=}" ;;
        --local)         [ $# -ge 2 ] || { echo "ERROR: --local needs a path" >&2; exit 1; }
                         LOCAL_DIR="$2"; shift ;;
        --local=*)       LOCAL_DIR="${1#*=}" ;;
        --dry-run)       DRY_RUN="yes" ;;
        --skip-boot-config) SKIP_BOOT_CONFIG="yes" ;;
        -h|--help)       usage; exit 0 ;;
        *) echo "ERROR: unknown option: $1" >&2; echo "" >&2; usage >&2; exit 1 ;;
    esac
    shift
done

# ── Root check ─────────────────────────────────────────────────────────────
# Relaxed for --dry-run: inspecting the plan must not require root (and cannot
# be tested on a workstation otherwise).  Nothing is written in that mode.
if [ "${DRY_RUN}" = "no" ] && [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: this script must run as root (use sudo)." >&2
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     Metixel Photoframe — Bootstrap Installer                 ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "Install root: ${INSTALL_ROOT}"
if [ "${DRY_RUN}" = "yes" ]; then
    echo "Mode        : DRY RUN (nothing will be changed)"
fi
echo ""

if [ "${DRY_RUN}" = "no" ] && [ -L "${INSTALL_ROOT}/live" ] \
   && [ -d "$(readlink -f "${INSTALL_ROOT}/live" 2>/dev/null || true)" ]; then
    echo "ERROR: Metixel is already installed at ${INSTALL_ROOT}." >&2
    echo "       Use the web UI or 'sudo update.sh <ref>' to update it." >&2
    exit 1
fi

# ── Ask the two questions up front ─────────────────────────────────────────
if [ -z "${CHANNEL}" ]; then
    echo "Release channel:"
    echo "  stable = Latest stable release (recommended)"
    echo "  beta   = Latest pre-release"
    echo "  dev    = Development branch (latest commits, unstable)"
    read -r -p "  Channel [stable]: " CHANNEL
    CHANNEL="${CHANNEL:-stable}"
fi
case "${CHANNEL}" in
    stable|beta|dev) ;;
    *) echo "  Invalid channel '${CHANNEL}' — using stable."; CHANNEL="stable" ;;
esac
echo "  → Channel: ${CHANNEL}"

if [ -z "${WIFI_COUNTRY}" ]; then
    echo ""
    echo "WiFi country code (e.g. AU, US, GB, DE, NZ):"
    echo "  Sets the radio's regulatory domain so the correct channels are used."
    read -r -p "  Country code [AU]: " WIFI_COUNTRY
    WIFI_COUNTRY="${WIFI_COUNTRY:-AU}"
fi
WIFI_COUNTRY="$(printf '%s' "${WIFI_COUNTRY}" | tr '[:lower:]' '[:upper:]')"
echo "  → WiFi country: ${WIFI_COUNTRY}"
echo ""

# ── 1) Obtain a checkout ───────────────────────────────────────────────────
# The checkout is staged OUTSIDE the install root first, then handed to
# update.sh, which moves it into releases/<version> and performs the atomic
# swap.  This keeps update.sh the single owner of the install/swap logic.
STAGE_DIR="/tmp/metixel-bootstrap-$$"

echo "[1/3] Obtaining the Metixel checkout…"
if [ -n "${LOCAL_DIR}" ]; then
    [ -d "${LOCAL_DIR}" ] || { echo "ERROR: --local ${LOCAL_DIR} does not exist" >&2; exit 1; }
    [ -f "${LOCAL_DIR}/scripts/update.sh" ] \
        || { echo "ERROR: --local ${LOCAL_DIR} is not a Metixel checkout" >&2; exit 1; }
    echo "  Using local checkout: ${LOCAL_DIR}"
    REF="${CHANNEL}"
else
    # Full clone (not shallow) — the application needs a real .git on the
    # device for its update checks and for local debugging patches.
    command -v git >/dev/null 2>&1 || {
        echo "  Installing git…"
        if [ "${DRY_RUN}" = "yes" ]; then
            echo "  [dry-run] apt-get install -y git"
        else
            apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git
        fi
    }
    # Resolve the ref for the channel from the remote (no checkout needed):
    # stable/beta → newest matching tag, dev → the dev branch.
    if [ "${DRY_RUN}" = "yes" ]; then
        echo "  [dry-run] git ls-remote --tags ${REPO_URL} (resolve ${CHANNEL})"
        REF="<newest ${CHANNEL} ref>"
    else
        case "${CHANNEL}" in
            stable)
                REF="$(git ls-remote --tags --refs "${REPO_URL}" 'v*' \
                    | sed 's#.*refs/tags/##' \
                    | grep -Ev -- '-' \
                    | sort -V | tail -1)" ;;
            beta)
                REF="$(git ls-remote --tags --refs "${REPO_URL}" 'v*' \
                    | sed 's#.*refs/tags/##' \
                    | grep -E -- '-' \
                    | sort -V | tail -1)" ;;
            dev) REF="dev" ;;
        esac
        [ -n "${REF}" ] || { echo "ERROR: could not resolve a '${CHANNEL}' ref from ${REPO_URL}" >&2; exit 1; }
        echo "  Resolved ${CHANNEL} → ${REF}"
    fi
fi

# ── 2) Delegate to update.sh ───────────────────────────────────────────────
echo ""
echo "[2/3] Running the installer (update.sh)…"

if [ "${DRY_RUN}" = "yes" ]; then
    echo "  [dry-run] clone ${REF} → ${STAGE_DIR}"
    echo "  [dry-run] write ${DATA_DIR}/init.json (wifi_country=${WIFI_COUNTRY}, channel=${CHANNEL})"
    echo "  [dry-run] update.sh ${REF} --staged-dir ${STAGE_DIR}"
    [ "${SKIP_BOOT_CONFIG}" = "yes" ] || echo "  [dry-run] configure_boot.sh"
    echo ""
    echo "=== dry-run complete: no changes were made ==="
    exit 0
fi

if [ -n "${LOCAL_DIR}" ]; then
    # Copy rather than move — the user's working tree must survive.
    rm -rf "${STAGE_DIR}"
    mkdir -p "${STAGE_DIR}"
    cp -a "${LOCAL_DIR}/." "${STAGE_DIR}/"
else
    rm -rf "${STAGE_DIR}"
    git clone --branch "${REF}" "${REPO_URL}" "${STAGE_DIR}"
fi

# Write the installer's answers as a partial overlay BEFORE update.sh runs, so
# reconcile.sh can read them (it does not require the app to have started).
# The app merges and consumes this on first start.
mkdir -p "${DATA_DIR}"
python3 - "${DATA_DIR}/init.json" "${WIFI_COUNTRY}" "${CHANNEL}" <<'PY'
import json, sys
path, country, channel = sys.argv[1], sys.argv[2], sys.argv[3]
overlay = {
    "network": {"wifi_country": country},
    "update": {"channel": channel},
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(overlay, fh, indent=2)
print(f"  Wrote {path} (wifi_country={country}, channel={channel})")
PY

# Hand the staged checkout to update.sh, which performs the atomic swap +
# health-check + rollback.  It moves STAGE_DIR into releases/<name>.
bash "${STAGE_DIR}/scripts/update.sh" "${REF}" "${REPO_URL}" --staged-dir "${STAGE_DIR}"

# ── 3) Boot configuration ──────────────────────────────────────────────────
# Boot config is NOT part of reconciliation (config.txt is the device's own file
# and a change only takes effect on reboot), so it is applied explicitly here.
# configure_boot.sh reports REBOOT_REQUIRED when it changes something.
if [ "${SKIP_BOOT_CONFIG}" = "no" ] && [ -d /boot/firmware ]; then
    echo ""
    echo "[3/3] Applying boot configuration…"
    bash "${INSTALL_ROOT}/live/scripts/configure_boot.sh"
else
    echo ""
    echo "[3/3] Skipping boot configuration."
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     Installation Complete                                    ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "After a reboot Metixel will start automatically."
echo "Dashboard: http://<pi-ip-address>"
echo ""

if [ -t 0 ]; then
    read -r -p "Reboot now? [Y/n]: " REPLY
    case "${REPLY:-Y}" in
        [Nn]*) echo "Reboot skipped — reboot later to finish setup." ;;
        *) echo "Rebooting…"; reboot ;;
    esac
else
    echo "Reboot the device to finish setup."
fi
